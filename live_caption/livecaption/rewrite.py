"""临时字幕的版本化改写与回滚。

- 每次迟到事件触发已显示区域重排时，产生一个带单调递增版本号的行快照；
- 每个版本记录：被替换行、替换原因、触发事件、事件集合指纹；
- 任一改写版本的行内容可由 (该版本事件集合, 策略) 独立重算一致；
- 同一行的改写深度超过 rollback_steps -> E_REWRITE_DEPTH；
- 改写历史条数超过 max_rewrite_history 时按策略淘汰最旧版本并标记降级，
  或返回 E_RESOURCE_LIMIT。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .config import DisplayPolicy
from .errors import E_RESOURCE_LIMIT, E_REWRITE_DEPTH, BusinessError
from .events import WordEvent, sort_events
from .lines import Line, assemble_lines

DEGRADE_EVICTED = "REWRITE_HISTORY_EVICTED"


def events_fingerprint(events: Sequence[WordEvent]) -> str:
    """事件集合指纹：排序后的 (word_index,start_ms,end_ms,word) 摘要。"""
    ordered = sort_events(events)
    payload = ";".join(
        f"{e.word_index}:{e.start_ms}:{e.end_ms}:{e.word}" for e in ordered)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RewriteRecord:
    version: int                     # 单调递增改写版本号
    reason: str                      # 替换原因
    trigger_event: WordEvent         # 触发事件
    replaced_lines: Tuple[str, ...]  # 被替换行的旧文本
    new_lines: Tuple[Line, ...]      # 新版本行
    event_fingerprint: str           # 该版本事件集合指纹
    word_indices: Tuple[int, ...]    # 该版本事件集合的 word_index（排序）
    chunk_seq: int


class RewriteLog:
    """改写历史：版本单调递增，单块改写深度受限，总量受限可降级淘汰。"""

    def __init__(self, policy: DisplayPolicy) -> None:
        self.policy = policy
        self.records: List[RewriteRecord] = []
        self._chunk_depth: Dict[int, int] = {}
        self._next_version = 1
        self.degraded_categories: List[str] = []
        self.evicted_count = 0

    @property
    def current_version(self) -> int:
        return self._next_version - 1

    def _check_depth(self, chunk_seq: int) -> None:
        depth = self._chunk_depth.get(chunk_seq, 0) + 1
        if depth > self.policy.rollback_steps:
            raise BusinessError(
                E_REWRITE_DEPTH,
                f"chunk {chunk_seq} rewrite depth {depth} exceeds "
                f"rollback_steps {self.policy.rollback_steps}",
                chunk_seq=chunk_seq, depth=depth,
                rollback_steps=self.policy.rollback_steps,
            )

    def append(self, reason: str, trigger: WordEvent,
               replaced_lines: Sequence[str], new_lines: Sequence[Line],
               events: Sequence[WordEvent], chunk_seq: int) -> RewriteRecord:
        self._check_depth(chunk_seq)
        record = RewriteRecord(
            version=self._next_version,
            reason=reason,
            trigger_event=trigger,
            replaced_lines=tuple(replaced_lines),
            new_lines=tuple(new_lines),
            event_fingerprint=events_fingerprint(events),
            word_indices=tuple(e.word_index for e in sort_events(events)),
            chunk_seq=chunk_seq,
        )
        self._next_version += 1
        self._chunk_depth[chunk_seq] = self._chunk_depth.get(chunk_seq, 0) + 1
        self.records.append(record)
        if len(self.records) > self.policy.max_rewrite_history:
            if self.policy.evict_on_history_overflow:
                overflow = len(self.records) - self.policy.max_rewrite_history
                del self.records[:overflow]
                self.evicted_count += overflow
                if DEGRADE_EVICTED not in self.degraded_categories:
                    self.degraded_categories.append(DEGRADE_EVICTED)
            else:
                raise BusinessError(
                    E_RESOURCE_LIMIT,
                    "rewrite history exceeds limit",
                    limit=self.policy.max_rewrite_history,
                )
        return record

    def recompute(self, record: RewriteRecord, events: Sequence[WordEvent],
                  final: bool = False) -> Tuple[Line, ...]:
        """由事件集合独立重算指定版本的行内容，用于一致性验证。"""
        if events_fingerprint(events) != record.event_fingerprint:
            raise ValueError("event set does not match record fingerprint")
        ordered = sort_events(events)
        lines = assemble_lines(
            ordered, self.policy,
            stream_id=ordered[0].stream_id if ordered else "",
            chunk_seq=record.chunk_seq, final=final)
        return tuple(lines)
