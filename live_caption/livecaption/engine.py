"""分块重排字幕引擎。

处理模型（全部确定性，决策只依赖事件自带 arrival_ms 虚拟时钟）：
- 事件按到达顺序逐条摄入，按 start_ms // chunk_ms 归入分块；
- 块在水位线（收到属于更晚块的事件）越过且等待满所选等待时长后定稿；
  等待硬上限为 max_wait_ms：到点即使水位线未越过也定稿，
  此后到达的同块事件按迟到事件触发改写；
- 定稿后到达的同块事件为迟到事件，触发版本化改写；
- 可见视图 = 各块按块序拼接、块内按声明规则排序后的全局装配；
- 每次视图变化产生单调递增版本号与驻留行快照（受资源上限约束）；
- 流结束后做定稿校验与一次性全局重排收敛比对。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from .config import DisplayPolicy
from .consistency import check_view_consistency
from .errors import E_RESOURCE_LIMIT, BusinessError
from .events import WordEvent, sort_events
from .finalize import FinalResult, check_convergence, finalize_once
from .latency import LatencyController, ProcessTimer
from .lines import Line, assemble_lines, assemble_mixed, check_screen
from .rewrite import RewriteLog

DEGRADE_SNAPSHOT_EVICTED = "RESIDENT_SNAPSHOT_EVICTED"


@dataclass
class _Chunk:
    chunk_seq: int
    window_start_ms: int
    window_end_ms: int
    events: List[WordEvent] = field(default_factory=list)
    first_arrival_ms: int = -1
    finalized: bool = False
    finalized_ms: int = -1
    chosen_wait_ms: int = 0
    last_line_texts: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class LineSnapshot:
    version: int
    reason: str
    lines: Tuple[Line, ...]
    arrival_seq: int


class CaptionEngine:
    """单流字幕引擎；strict=True 时业务错误直接抛出，否则记录后继续。"""

    def __init__(self, policy: DisplayPolicy, stream_id: str,
                 strict: bool = True) -> None:
        self.policy = policy
        self.stream_id = stream_id
        self.strict = strict
        self.chunks: Dict[int, _Chunk] = {}
        self.received: List[WordEvent] = []
        self.rewrite_log = RewriteLog(policy)
        self.latency = LatencyController(policy)
        self.line_snapshots: Deque[LineSnapshot] = deque()
        self.temp_version = 0
        self.visible_lines: List[Line] = []
        self.errors: List[dict] = []
        self.degraded: List[str] = []
        self.finalized_chunk_seqs: List[int] = []
        self.finished = False
        self.final_result: Optional[FinalResult] = None
        self.converged: Optional[bool] = None

    def _error(self, err: BusinessError) -> None:
        if self.strict:
            raise err
        if self.errors and self.errors[-1]["message"] == err.message:
            return  # 连续相同错误去重，保留首次出现
        self.errors.append(err.to_dict())

    def _chunk_id(self, ev: WordEvent) -> int:
        return ev.start_ms // self.policy.chunk_ms

    def _ordered_events(self) -> List[WordEvent]:
        """分块重排核心：块按块序、块内按声明的同分打破规则排序。"""
        ordered: List[WordEvent] = []
        for seq in sorted(self.chunks):
            ordered.extend(sort_events(self.chunks[seq].events))
        return ordered

    def _final_flags(self, ordered: Sequence[WordEvent]) -> List[bool]:
        return [self.chunks[self._chunk_id(ev)].finalized for ev in ordered]

    def _reassemble(self) -> List[Line]:
        ordered = self._ordered_events()
        return assemble_mixed(ordered, self._final_flags(ordered),
                              self.policy, self.stream_id, chunk_seq=-1)

    def _push_snapshot(self, reason: str, arrival_seq: int) -> None:
        self.temp_version += 1
        self.line_snapshots.append(LineSnapshot(
            version=self.temp_version, reason=reason,
            lines=tuple(self.visible_lines), arrival_seq=arrival_seq))
        while len(self.line_snapshots) > self.policy.max_resident_snapshots:
            self.line_snapshots.popleft()
            if DEGRADE_SNAPSHOT_EVICTED not in self.degraded:
                self.degraded.append(DEGRADE_SNAPSHOT_EVICTED)

    def _visible_screen(self) -> List[Line]:
        n = self.policy.max_lines_per_screen
        return self.visible_lines[len(self.visible_lines) - n:]

    def _audit_view(self, checkpoint: str) -> None:
        """自检：可见视图与已接收事件完整重排口径一致。"""
        try:
            check_view_consistency(self._visible_screen(), self.received,
                                   self.policy, self.stream_id, checkpoint)
        except BusinessError as err:
            self._error(err)

    def ingest(self, ev: WordEvent) -> None:
        if len(self.received) >= self.policy.max_events_per_stream:
            self._error(BusinessError(
                E_RESOURCE_LIMIT, "event count exceeds limit",
                stream_id=self.stream_id,
                limit=self.policy.max_events_per_stream))
            return
        cid = self._chunk_id(ev)
        chunk = self.chunks.get(cid)
        if chunk is None:
            chunk = _Chunk(chunk_seq=cid,
                           window_start_ms=cid * self.policy.chunk_ms,
                           window_end_ms=(cid + 1) * self.policy.chunk_ms)
            self.chunks[cid] = chunk
            chunk.first_arrival_ms = ev.arrival_ms
            plan = self.latency.plan_chunk(cid, chunk.window_start_ms,
                                           chunk.window_end_ms)
            chunk.chosen_wait_ms = plan.chosen_wait_ms
        late = chunk.finalized
        if late:
            self.latency.observe_lateness(ev.arrival_ms - chunk.finalized_ms)
        chunk.events.append(ev)
        self.received.append(ev)
        if late:
            self._rewrite(chunk, ev)
        else:
            self._refresh_view("ingest", ev.arrival_seq)
        self._try_finalize(ev.arrival_ms)
        self._audit_view(checkpoint=f"arrival_seq={ev.arrival_seq}")

    def _refresh_view(self, reason: str, arrival_seq: int) -> None:
        try:
            new_lines = self._reassemble()
        except BusinessError as err:
            self._error(err)
            return
        if [ln.text for ln in new_lines] != [ln.text for ln in self.visible_lines]:
            self.visible_lines = new_lines
            self._push_snapshot(reason, arrival_seq)

    def _try_finalize(self, clock_ms: int) -> None:
        for seq in sorted(self.chunks):
            chunk = self.chunks[seq]
            if chunk.finalized:
                continue
            watermark_passed = any(
                e.start_ms >= chunk.window_end_ms for e in self.received)
            deadline = chunk.first_arrival_ms + chunk.chosen_wait_ms
            hard_cap = chunk.first_arrival_ms + self.policy.max_wait_ms
            ready = (watermark_passed and clock_ms >= deadline) \
                or clock_ms >= hard_cap
            if not ready:
                continue
            timer = ProcessTimer()
            ordered = sort_events(chunk.events)
            try:
                chunk_lines = assemble_lines(ordered, self.policy,
                                             self.stream_id,
                                             chunk_seq=seq, final=True)
                check_screen(chunk_lines, self.policy, self.stream_id, seq)
            except BusinessError as err:
                self._error(err)
                continue
            chunk.finalized = True
            chunk.finalized_ms = clock_ms
            chunk.last_line_texts = [ln.text for ln in chunk_lines]
            self.finalized_chunk_seqs.append(seq)
            try:
                self.latency.record_timing(
                    chunk_seq=seq, event_count=len(chunk.events),
                    window_start_ms=chunk.window_start_ms,
                    window_end_ms=chunk.window_end_ms,
                    first_arrival_ms=chunk.first_arrival_ms,
                    finalized_ms=clock_ms, process_us=timer.elapsed_us(),
                    chosen_wait_ms=chunk.chosen_wait_ms,
                    completed_chunks=len(self.finalized_chunk_seqs),
                    temp_version=self.temp_version)
            except BusinessError as err:
                self._error(err)
            self._refresh_view("finalize", arrival_seq=-1)

    def _rewrite(self, chunk: _Chunk, trigger: WordEvent) -> None:
        """迟到事件插入已显示区域：产生带单调递增版本号的新行快照。

        改写记录以块区域行为口径：被替换行 = 该块旧行文本，
        新版行 = 该块事件集合重排装配的新行（可独立重算）。
        """
        ordered = sort_events(chunk.events)
        try:
            region_lines = assemble_lines(ordered, self.policy, self.stream_id,
                                          chunk_seq=chunk.chunk_seq, final=True)
        except BusinessError as err:
            self._error(err)
            return
        replaced = list(chunk.last_line_texts)
        chunk.last_line_texts = [ln.text for ln in region_lines]
        try:
            self.rewrite_log.append(
                reason="late_event", trigger=trigger,
                replaced_lines=replaced, new_lines=region_lines,
                events=chunk.events, chunk_seq=chunk.chunk_seq)
        except BusinessError as err:
            self._error(err)
            return
        self._refresh_view("rewrite", trigger.arrival_seq)

    def finish(self) -> FinalResult:
        """收尾：定稿剩余块，校验并与一次性全局重排比对收敛。"""
        if self.received:
            last_clock = max(ev.arrival_ms for ev in self.received)
            for seq in sorted(self.chunks):
                chunk = self.chunks[seq]
                if chunk.finalized:
                    continue
                ordered = sort_events(chunk.events)
                try:
                    chunk_lines = assemble_lines(ordered, self.policy,
                                                 self.stream_id, chunk_seq=seq,
                                                 final=True)
                    check_screen(chunk_lines, self.policy, self.stream_id, seq)
                except BusinessError as err:
                    self._error(err)
                    continue
                chunk.finalized = True
                chunk.finalized_ms = last_clock
                chunk.last_line_texts = [ln.text for ln in chunk_lines]
                self.finalized_chunk_seqs.append(seq)
            try:
                self.visible_lines = self._reassemble()
            except BusinessError as err:
                self._error(err)
            self._push_snapshot("finish", arrival_seq=-1)
        try:
            oneshot = finalize_once(self.received, self.policy, self.stream_id,
                                    chunk_count=len(self.finalized_chunk_seqs),
                                    rewrite_total=self.rewrite_log.current_version)
        except BusinessError as err:
            self._error(err)
            self.finished = True
            self.converged = False
            self.final_result = FinalResult(
                lines=[], text="",
                chunk_count=len(self.finalized_chunk_seqs),
                rewrite_total=self.rewrite_log.current_version)
            return self.final_result
        incremental = assemble_lines(self._ordered_events(), self.policy,
                                     self.stream_id, chunk_seq=-1, final=True)
        incremental_text = "\n".join(ln.text for ln in incremental)
        self.converged = check_convergence(incremental_text, oneshot.text)
        self.final_result = FinalResult(
            lines=oneshot.lines, text=oneshot.text,
            chunk_count=len(self.finalized_chunk_seqs),
            rewrite_total=self.rewrite_log.current_version)
        self.finished = True
        return self.final_result

    def run(self, events: Sequence[WordEvent]) -> FinalResult:
        for ev in events:
            self.ingest(ev)
        return self.finish()

    def current_chunk_seq(self) -> int:
        open_chunks = [s for s, c in sorted(self.chunks.items()) if not c.finalized]
        if open_chunks:
            return open_chunks[0]
        return max(self.chunks) if self.chunks else -1

    def stats(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "received_events": len(self.received),
            "finalized_chunks": len(self.finalized_chunk_seqs),
            "total_chunks": len(self.chunks),
            "current_chunk_seq": self.current_chunk_seq(),
            "temp_version": self.temp_version,
            "rewrite_total": self.rewrite_log.current_version,
            "resident_snapshots": len(self.line_snapshots),
            "rewrite_history": len(self.rewrite_log.records),
            "evicted_rewrites": self.rewrite_log.evicted_count,
            "degraded": list(self.degraded)
            + list(self.rewrite_log.degraded_categories),
            "converged": self.converged,
            "finished": self.finished,
        }
