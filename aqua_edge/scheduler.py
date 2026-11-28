"""Deterministic event scheduler for the multiplexed gateway uplink."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import AquaError
from .model import Frame, Project, Stream, parse_project


SNAPSHOT_VERSION = "aqua-edge-snapshot/1"
EPS_TIME = 1e-7
EPS_BYTES = 1e-7

BLOCK_BANDWIDTH = "BANDWIDTH"
BLOCK_WINDOW_RTT = "WINDOW_RTT"
BLOCK_CONSUMER_SLOW = "CONSUMER_SLOW"
BLOCK_OUTAGE = "OUTAGE"
BLOCK_NOT_READY = "NOT_READY"
BLOCK_WINDOW_CAPACITY = "WINDOW_CAPACITY"
BLOCK_NONE = "NONE"

BLOCK_DESCRIPTIONS = {
    BLOCK_BANDWIDTH: "同连接大块帧占用上行带宽或并发流槽位",
    BLOCK_WINDOW_RTT: "流窗口被未归还帧占满，等待 RTT 信用归还",
    BLOCK_CONSUMER_SLOW: "消费者读取速率下降，窗口信用归还排队累积",
    BLOCK_OUTAGE: "链路瞬断，等待恢复",
    BLOCK_NOT_READY: "输入帧 ready=false，未进入可发送状态",
    BLOCK_WINDOW_CAPACITY: "单帧长度大于该流窗口，窗口永远无法容纳",
    BLOCK_NONE: "无等待",
}


@dataclass
class ActiveFrame:
    frame: Frame
    start_ms: float
    remaining_bytes: float
    outage_pause_ms: float = 0.0


@dataclass
class Credit:
    credit_id: int
    frame_id: str
    stream_id: str
    bytes_count: int
    completed_at_ms: float
    consume_start_ms: float
    consume_end_ms: float
    return_time_ms: float
    returned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "credit_id": self.credit_id,
            "frame_id": self.frame_id,
            "stream_id": self.stream_id,
            "bytes": self.bytes_count,
            "completed_at_ms": self.completed_at_ms,
            "consume_start_ms": self.consume_start_ms,
            "consume_end_ms": self.consume_end_ms,
            "return_time_ms": self.return_time_ms,
            "returned": self.returned,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Credit":
        return cls(
            credit_id=int(raw["credit_id"]),
            frame_id=str(raw["frame_id"]),
            stream_id=str(raw["stream_id"]),
            bytes_count=int(raw["bytes"]),
            completed_at_ms=float(raw["completed_at_ms"]),
            consume_start_ms=float(raw["consume_start_ms"]),
            consume_end_ms=float(raw["consume_end_ms"]),
            return_time_ms=float(raw["return_time_ms"]),
            returned=bool(raw.get("returned", False)),
        )


@dataclass
class RuntimeStream:
    stream: Stream
    next_index: int = 0
    window_available: float = 0.0
    active: ActiveFrame | None = None

    def __post_init__(self) -> None:
        if self.window_available == 0.0:
            self.window_available = float(self.stream.window_bytes)


class Scheduler:
    """Event-driven scheduler.

    The uplink is shared equally by all currently transmitting streams. At most
    one frame per stream is in flight, and at most
    ``network.max_concurrent_streams`` streams transmit concurrently.
    """

    def __init__(self, project: Project) -> None:
        self.project = project
        self.streams = project.streams
        self.stream_by_id = project.stream_map()
        self.runtime: dict[str, RuntimeStream] = {
            stream.stream_id: RuntimeStream(stream=stream) for stream in self.streams
        }
        self.now_ms = 0.0
        self.completed_count = 0
        self.credits: list[Credit] = []
        self.consumer_rate_bytes_per_s = project.consumer.read_bytes_per_s
        self.consumer_busy_until_ms = 0.0
        self.rate_event_index = 0
        self.next_credit_id = 1
        self.trace_records: dict[str, dict[str, Any]] = {}
        self.wait_segments: dict[str, list[dict[str, Any]]] = {}
        self._finished = False

    # ------------------------------------------------------------------
    # Public scheduling entry points
    # ------------------------------------------------------------------
    def run(self, max_completed: int | None = None) -> "Scheduler":
        """Run until completion, or until at least ``max_completed`` frames finish.

        A snapshot cutoff is taken at a clean event boundary after slots have
        been refilled. If several frames finish at the same timestamp, all of
        them are included before the cutoff is evaluated.
        """

        if max_completed is not None and max_completed < 0:
            raise AquaError("E_ARGUMENT", "max_completed 不得小于 0")

        while True:
            self._normalize_now()
            self._fill_slots()

            if max_completed is not None and self.completed_count >= max_completed:
                return self

            next_time = self._next_event_time()
            if next_time is None:
                self.finalize_unsent()
                self._finished = True
                return self

            self._record_waiting(self.now_ms, next_time)
            self._advance(next_time - self.now_ms)
            self.now_ms = next_time

    def finalize_unsent(self) -> None:
        for runtime in self.runtime.values():
            for frame in runtime.stream.frames[runtime.next_index :]:
                if frame.frame_id in self.trace_records:
                    continue
                self._record_unsent(runtime, frame)

    def frame_traces(self) -> list[dict[str, Any]]:
        records = list(self.trace_records.values())
        stream_order = {stream.stream_id: index for index, stream in enumerate(self.streams)}
        frame_order = {
            frame.frame_id: (stream.stream_id, frame.sequence)
            for stream in self.streams
            for frame in stream.frames
        }
        return sorted(
            records,
            key=lambda record: (
                stream_order.get(record["stream_id"], 10**9),
                frame_order.get(record["frame_id"], ("", 10**9))[1],
            ),
        )

    # ------------------------------------------------------------------
    # Event loop internals
    # ------------------------------------------------------------------
    def _normalize_now(self) -> None:
        while self.rate_event_index < len(self.project.consumer.events):
            event = self.project.consumer.events[self.rate_event_index]
            if event.time_ms > self.now_ms + EPS_TIME:
                break
            self._set_consumer_rate(event.read_bytes_per_s, event.time_ms)
            self.rate_event_index += 1

        while True:
            changed = self._return_due_credits()
            completed = self._complete_due_active()
            if not changed and not completed:
                break

    def _return_due_credits(self) -> bool:
        changed = False
        for credit in self.credits:
            if credit.returned or credit.return_time_ms > self.now_ms + EPS_TIME:
                continue
            credit.returned = True
            runtime = self.runtime[credit.stream_id]
            runtime.window_available = min(
                float(runtime.stream.window_bytes),
                runtime.window_available + credit.bytes_count,
            )
            changed = True
        return changed

    def _complete_due_active(self) -> bool:
        due: list[tuple[RuntimeStream, ActiveFrame]] = [
            (runtime, runtime.active)
            for runtime in self.runtime.values()
            if runtime.active is not None and runtime.active.remaining_bytes <= EPS_BYTES
        ]
        if not due:
            return False
        due.sort(key=lambda item: (item[0].stream.stream_id, item[1].frame.frame_id))
        for runtime, active in due:
            self._complete_frame(runtime, active)
        return True

    def _fill_slots(self) -> None:
        if self._is_outage(self.now_ms):
            return
        active_count = sum(1 for runtime in self.runtime.values() if runtime.active is not None)
        free_slots = self.project.network.max_concurrent_streams - active_count
        if free_slots <= 0:
            return

        candidates: list[tuple[Stream, Frame]] = []
        for runtime in self.runtime.values():
            if runtime.active is not None or runtime.next_index >= len(runtime.stream.frames):
                continue
            frame = runtime.stream.frames[runtime.next_index]
            if frame.logical_time_ms > self.now_ms + EPS_TIME:
                continue
            if not frame.ready:
                continue
            if runtime.window_available + EPS_BYTES < frame.length_bytes:
                continue
            candidates.append((runtime.stream, frame))

        candidates.sort(
            key=lambda item: (
                -item[0].priority,
                item[0].stream_id,
                item[1].sequence,
                item[1].frame_id,
            )
        )
        for stream, frame in candidates[:free_slots]:
            self._start_frame(self.runtime[stream.stream_id], frame)

    def _next_event_time(self) -> float | None:
        candidates: list[float] = []
        if not self._is_outage(self.now_ms):
            active_count = sum(1 for runtime in self.runtime.values() if runtime.active is not None)
            if active_count:
                share_rate = self.project.network.uplink_bytes_per_ms / active_count
                for runtime in self.runtime.values():
                    if runtime.active is None:
                        continue
                    duration = runtime.active.remaining_bytes / share_rate
                    if duration < EPS_TIME:
                        duration = EPS_TIME
                    candidates.append(self.now_ms + duration)

        for credit in self.credits:
            if not credit.returned and credit.return_time_ms > self.now_ms + EPS_TIME:
                candidates.append(credit.return_time_ms)
            if not credit.returned and credit.consume_end_ms > self.now_ms + EPS_TIME:
                candidates.append(credit.consume_end_ms)
        while self.rate_event_index < len(self.project.consumer.events):
            event = self.project.consumer.events[self.rate_event_index]
            if event.time_ms > self.now_ms + EPS_TIME:
                candidates.append(event.time_ms)
                break
            self.rate_event_index += 1
        for outage in self.project.outages:
            if outage.start_ms > self.now_ms + EPS_TIME:
                candidates.append(outage.start_ms)
            if outage.end_ms > self.now_ms + EPS_TIME:
                candidates.append(outage.end_ms)
        for runtime in self.runtime.values():
            if runtime.next_index >= len(runtime.stream.frames):
                continue
            frame = runtime.stream.frames[runtime.next_index]
            if frame.logical_time_ms > self.now_ms + EPS_TIME:
                candidates.append(frame.logical_time_ms)

        positive = [value for value in candidates if value > self.now_ms + EPS_TIME]
        return min(positive) if positive else None

    def _record_waiting(self, start_ms: float, end_ms: float) -> None:
        if end_ms <= start_ms + EPS_TIME:
            return
        for runtime in self.runtime.values():
            if runtime.next_index >= len(runtime.stream.frames):
                continue
            frame = runtime.stream.frames[runtime.next_index]
            if frame.logical_time_ms > start_ms + EPS_TIME:
                continue
            if frame.frame_id in self.trace_records:
                continue
            blocker = self._blocker_for(runtime, frame)
            self.wait_segments.setdefault(frame.frame_id, []).append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "type": blocker["type"],
                    "stream_id": blocker["stream_id"],
                    "frame_id": blocker["frame_id"],
                }
            )

    def _advance(self, delta_ms: float) -> None:
        if delta_ms <= 0:
            return
        if self._is_outage(self.now_ms):
            for runtime in self.runtime.values():
                if runtime.active is not None:
                    runtime.active.outage_pause_ms += delta_ms
            return
        active = [runtime.active for runtime in self.runtime.values() if runtime.active is not None]
        if not active:
            return
        share_rate = self.project.network.uplink_bytes_per_ms / len(active)
        consumed = share_rate * delta_ms
        for frame in active:
            frame.remaining_bytes = max(0.0, frame.remaining_bytes - consumed)

    def _start_frame(self, runtime: RuntimeStream, frame: Frame) -> None:
        runtime.next_index += 1
        runtime.window_available = max(0.0, runtime.window_available - frame.length_bytes)
        runtime.active = ActiveFrame(
            frame=frame,
            start_ms=self.now_ms,
            remaining_bytes=float(frame.length_bytes),
        )

        blocked = self._summarize_waiting(frame.frame_id)
        outage_wait = sum(
            segment["end_ms"] - segment["start_ms"]
            for segment in self.wait_segments.get(frame.frame_id, [])
            if segment["type"] == BLOCK_OUTAGE
        )
        queue_delay = max(0.0, self.now_ms - frame.logical_time_ms)
        deadline_at = (
            frame.logical_time_ms + runtime.stream.deadline_ms
            if runtime.stream.deadline_ms is not None
            else None
        )
        self.trace_records[frame.frame_id] = {
            "frame_id": frame.frame_id,
            "stream_id": runtime.stream.stream_id,
            "stream_label": runtime.stream.label,
            "pond_id": runtime.stream.pond_id,
            "kind": runtime.stream.kind,
            "sensor_type": runtime.stream.sensor_type,
            "target_device": runtime.stream.target_device,
            "priority": runtime.stream.priority,
            "window_bytes": runtime.stream.window_bytes,
            "deadline_ms": runtime.stream.deadline_ms,
            "logical_time_ms": frame.logical_time_ms,
            "ready_time_ms": frame.ready_time_ms,
            "length_bytes": frame.length_bytes,
            "start_ms": self.now_ms,
            "end_ms": None,
            "queue_delay_ms": queue_delay,
            "transfer_ms": None,
            "delivery_delay_ms": None,
            "outage_wait_ms": outage_wait,
            "outage_pause_ms": 0.0,
            "outage_total_ms": outage_wait,
            "status": "SENDING",
            "blocking_type": blocked["type"],
            "blocking_stream_id": blocked["stream_id"],
            "blocking_frame_id": blocked["frame_id"],
            "blocked_by": blocked,
            "wait_breakdown": self._wait_breakdown(frame.frame_id),
            "deadline_at_ms": deadline_at,
            "on_time": None,
        }
        self.wait_segments.pop(frame.frame_id, None)

    def _complete_frame(self, runtime: RuntimeStream, active: ActiveFrame) -> None:
        frame = active.frame
        runtime.active = None
        record = self.trace_records[frame.frame_id]
        record["end_ms"] = self.now_ms
        record["transfer_ms"] = max(0.0, self.now_ms - active.start_ms)
        record["delivery_delay_ms"] = max(0.0, self.now_ms - frame.logical_time_ms)
        record["outage_pause_ms"] = active.outage_pause_ms
        record["outage_total_ms"] = record["outage_wait_ms"] + active.outage_pause_ms
        record["status"] = "SENT"
        if runtime.stream.deadline_ms is not None:
            deadline_at = frame.logical_time_ms + runtime.stream.deadline_ms
            record["on_time"] = self.now_ms <= deadline_at + EPS_TIME
        self.completed_count += 1
        self._enqueue_credit(runtime.stream, frame, self.now_ms)

    def _enqueue_credit(self, stream: Stream, frame: Frame, completed_at_ms: float) -> None:
        read_rate = self.consumer_rate_bytes_per_s / 1000.0
        consume_start = max(completed_at_ms, self.consumer_busy_until_ms)
        consume_end = consume_start + frame.length_bytes / read_rate
        return_time = consume_end + self.project.network.rtt_ms
        credit = Credit(
            credit_id=self.next_credit_id,
            frame_id=frame.frame_id,
            stream_id=stream.stream_id,
            bytes_count=frame.length_bytes,
            completed_at_ms=completed_at_ms,
            consume_start_ms=consume_start,
            consume_end_ms=consume_end,
            return_time_ms=return_time,
        )
        self.next_credit_id += 1
        self.credits.append(credit)
        self.consumer_busy_until_ms = max(self.consumer_busy_until_ms, consume_end)

    def _set_consumer_rate(self, new_rate_bytes_per_s: float, at_ms: float) -> None:
        self.consumer_rate_bytes_per_s = new_rate_bytes_per_s
        new_rate_per_ms = new_rate_bytes_per_s / 1000.0
        affected = [
            credit
            for credit in self.credits
            if not credit.returned and credit.consume_end_ms > at_ms + EPS_TIME
        ]
        affected.sort(key=lambda credit: (credit.completed_at_ms, credit.credit_id))
        fully_consumed = [
            credit.consume_end_ms
            for credit in self.credits
            if not credit.returned and credit.consume_end_ms <= at_ms + EPS_TIME
        ]
        cursor = max([at_ms, *fully_consumed])
        for credit in affected:
            if credit.consume_start_ms < at_ms < credit.consume_end_ms:
                old_duration = credit.consume_end_ms - credit.consume_start_ms
                remaining_fraction = (credit.consume_end_ms - at_ms) / old_duration
                remaining_bytes = credit.bytes_count * max(0.0, min(1.0, remaining_fraction))
                start = at_ms
            else:
                remaining_bytes = float(credit.bytes_count)
                start = max(at_ms, credit.completed_at_ms, cursor)
            end = start + remaining_bytes / new_rate_per_ms
            credit.consume_start_ms = start
            credit.consume_end_ms = end
            credit.return_time_ms = end + self.project.network.rtt_ms
            cursor = end
        self.consumer_busy_until_ms = cursor

    def _is_outage(self, time_ms: float) -> bool:
        for outage in self.project.outages:
            if outage.start_ms - EPS_TIME <= time_ms < outage.end_ms:
                return True
        return False

    # ------------------------------------------------------------------
    # Blocking attribution
    # ------------------------------------------------------------------
    def _blocker_for(self, runtime: RuntimeStream, frame: Frame) -> dict[str, Any]:
        if not frame.ready:
            return self._blocker(frame, BLOCK_NOT_READY, runtime.stream.stream_id, frame.frame_id)
        if frame.length_bytes > self.project.network.frame_size_limit_bytes:
            return self._blocker(
                frame,
                "FRAME_LIMIT",
                runtime.stream.stream_id,
                frame.frame_id,
            )
        if self._is_outage(self.now_ms):
            return self._blocker(frame, BLOCK_OUTAGE, None, None)
        if runtime.active is not None:
            return self._blocker(
                frame,
                BLOCK_BANDWIDTH,
                runtime.stream.stream_id,
                runtime.active.frame.frame_id,
            )
        if runtime.window_available + EPS_BYTES < frame.length_bytes:
            pending = [
                credit
                for credit in self.credits
                if credit.stream_id == runtime.stream.stream_id and not credit.returned
            ]
            if pending:
                source = max(
                    pending,
                    key=lambda credit: (
                        credit.return_time_ms,
                        credit.completed_at_ms,
                        credit.credit_id,
                    ),
                )
                block_type = (
                    BLOCK_CONSUMER_SLOW
                    if source.consume_end_ms > self.now_ms + EPS_TIME
                    else BLOCK_WINDOW_RTT
                )
                return self._blocker(
                    frame,
                    block_type,
                    source.stream_id,
                    source.frame_id,
                )
            return self._blocker(
                frame,
                BLOCK_WINDOW_CAPACITY,
                runtime.stream.stream_id,
                frame.frame_id,
            )
        active = [item.active for item in self.runtime.values() if item.active is not None]
        if len(active) >= self.project.network.max_concurrent_streams:
            source = sorted(
                active,
                key=lambda item: (
                    -item.remaining_bytes,
                    item.frame.stream_id,
                    item.frame.frame_id,
                ),
            )[0]
            return self._blocker(
                frame,
                BLOCK_BANDWIDTH,
                source.frame.stream_id,
                source.frame.frame_id,
            )
        return self._blocker(frame, BLOCK_NONE, None, None)

    def _blocker(
        self,
        frame: Frame,
        block_type: str,
        stream_id: str | None,
        source_frame_id: str | None,
    ) -> dict[str, Any]:
        return {
            "type": block_type,
            "stream_id": stream_id,
            "frame_id": source_frame_id,
            "description": BLOCK_DESCRIPTIONS.get(block_type, block_type),
        }

    def _summarize_waiting(self, frame_id: str) -> dict[str, Any]:
        segments = self.wait_segments.get(frame_id, [])
        if not segments:
            return {
                "type": BLOCK_NONE,
                "stream_id": None,
                "frame_id": None,
                "description": BLOCK_DESCRIPTIONS[BLOCK_NONE],
            }
        totals: dict[tuple[str, str | None, str | None], float] = {}
        for segment in segments:
            key = (segment["type"], segment["stream_id"], segment["frame_id"])
            totals[key] = totals.get(key, 0.0) + max(0.0, segment["end_ms"] - segment["start_ms"])
        winner = max(
            totals.items(),
            key=lambda item: (
                item[1],
                -list(totals).index(item[0]),
            ),
        )[0]
        return {
            "type": winner[0],
            "stream_id": winner[1],
            "frame_id": winner[2],
            "description": BLOCK_DESCRIPTIONS.get(winner[0], winner[0]),
        }

    def _wait_breakdown(self, frame_id: str) -> list[dict[str, Any]]:
        totals: dict[tuple[str, str | None, str | None], float] = {}
        for segment in self.wait_segments.get(frame_id, []):
            key = (segment["type"], segment["stream_id"], segment["frame_id"])
            totals[key] = totals.get(key, 0.0) + max(0.0, segment["end_ms"] - segment["start_ms"])
        result = [
            {
                "type": key[0],
                "stream_id": key[1],
                "frame_id": key[2],
                "duration_ms": duration,
            }
            for key, duration in totals.items()
            if duration > EPS_TIME
        ]
        result.sort(key=lambda item: (-item["duration_ms"], item["type"], item["stream_id"] or ""))
        return result

    def _record_unsent(self, runtime: RuntimeStream, frame: Frame) -> None:
        if frame.ready:
            blocker = self._blocker_for(runtime, frame)
            status = "UNSENT"
        else:
            blocker = self._blocker(frame, BLOCK_NOT_READY, runtime.stream.stream_id, frame.frame_id)
            status = "NOT_READY"
        self.trace_records[frame.frame_id] = {
            "frame_id": frame.frame_id,
            "stream_id": runtime.stream.stream_id,
            "stream_label": runtime.stream.label,
            "pond_id": runtime.stream.pond_id,
            "kind": runtime.stream.kind,
            "sensor_type": runtime.stream.sensor_type,
            "target_device": runtime.stream.target_device,
            "priority": runtime.stream.priority,
            "window_bytes": runtime.stream.window_bytes,
            "deadline_ms": runtime.stream.deadline_ms,
            "logical_time_ms": frame.logical_time_ms,
            "ready_time_ms": frame.ready_time_ms,
            "length_bytes": frame.length_bytes,
            "start_ms": None,
            "end_ms": None,
            "queue_delay_ms": None,
            "transfer_ms": None,
            "delivery_delay_ms": None,
            "outage_wait_ms": 0.0,
            "outage_pause_ms": 0.0,
            "outage_total_ms": 0.0,
            "status": status,
            "blocking_type": blocker["type"],
            "blocking_stream_id": blocker["stream_id"],
            "blocking_frame_id": blocker["frame_id"],
            "blocked_by": blocker,
            "wait_breakdown": self._wait_breakdown(frame.frame_id),
            "deadline_at_ms": (
                frame.logical_time_ms + runtime.stream.deadline_ms
                if runtime.stream.deadline_ms is not None
                else None
            ),
            "on_time": False if runtime.stream.deadline_ms is not None else None,
        }

    # ------------------------------------------------------------------
    # Snapshot support
    # ------------------------------------------------------------------
    def snapshot_document(self) -> dict[str, Any]:
        state_streams: dict[str, Any] = {}
        for stream in self.streams:
            runtime = self.runtime[stream.stream_id]
            active = None
            if runtime.active is not None:
                active = {
                    "frame_id": runtime.active.frame.frame_id,
                    "start_ms": runtime.active.start_ms,
                    "remaining_bytes": runtime.active.remaining_bytes,
                    "outage_pause_ms": runtime.active.outage_pause_ms,
                }
            state_streams[stream.stream_id] = {
                "next_index": runtime.next_index,
                "window_available": runtime.window_available,
                "active": active,
            }
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "tool": "aqua-edge",
            "project": self.project.to_dict(),
            "state": {
                "now_ms": self.now_ms,
                "completed_count": self.completed_count,
                "consumer_rate_bytes_per_s": self.consumer_rate_bytes_per_s,
                "consumer_busy_until_ms": self.consumer_busy_until_ms,
                "rate_event_index": self.rate_event_index,
                "next_credit_id": self.next_credit_id,
                "streams": state_streams,
                "credits": [credit.to_dict() for credit in self.credits if not credit.returned],
                "trace_records": [self.trace_records[frame_id] for frame_id in sorted(self.trace_records)],
                "wait_segments": self.wait_segments,
            },
        }

    @classmethod
    def from_snapshot(cls, raw_snapshot: Any) -> "Scheduler":
        try:
            if not isinstance(raw_snapshot, dict):
                raise KeyError("root")
            if raw_snapshot.get("snapshot_version") != SNAPSHOT_VERSION:
                raise AquaError(
                    "E_SNAPVERSION",
                    f"快照版本不兼容，期望 {SNAPSHOT_VERSION}",
                    {
                        "found": raw_snapshot.get("snapshot_version"),
                        "expected": SNAPSHOT_VERSION,
                    },
                )
            project = parse_project(raw_snapshot["project"])
            state = raw_snapshot["state"]
            if not isinstance(state, dict):
                raise KeyError("state")
            required_state = {
                "now_ms",
                "completed_count",
                "consumer_rate_bytes_per_s",
                "consumer_busy_until_ms",
                "rate_event_index",
                "next_credit_id",
                "streams",
                "credits",
                "trace_records",
                "wait_segments",
            }
            missing = sorted(required_state.difference(state))
            if missing:
                raise KeyError(missing[0])
            scheduler = cls(project)
            scheduler.now_ms = float(state["now_ms"])
            scheduler.completed_count = int(state["completed_count"])
            scheduler.consumer_rate_bytes_per_s = float(state["consumer_rate_bytes_per_s"])
            scheduler.consumer_busy_until_ms = float(state["consumer_busy_until_ms"])
            scheduler.rate_event_index = int(state["rate_event_index"])
            scheduler.next_credit_id = int(state["next_credit_id"])
            scheduler.credits = [Credit.from_dict(item) for item in state["credits"]]
            scheduler.trace_records = {
                str(record["frame_id"]): record for record in state["trace_records"]
            }
            scheduler.wait_segments = {
                str(frame_id): list(segments)
                for frame_id, segments in state["wait_segments"].items()
            }
            stream_states = state["streams"]
            if not isinstance(stream_states, dict):
                raise KeyError("streams")
            for stream in project.streams:
                raw_runtime = stream_states[stream.stream_id]
                runtime = scheduler.runtime[stream.stream_id]
                runtime.next_index = int(raw_runtime["next_index"])
                runtime.window_available = float(raw_runtime["window_available"])
                active = raw_runtime.get("active")
                if active is not None:
                    frame_id = str(active["frame_id"])
                    frame = next(
                        (item for item in stream.frames if item.frame_id == frame_id),
                        None,
                    )
                    if frame is None:
                        raise KeyError(f"active frame {frame_id}")
                    runtime.active = ActiveFrame(
                        frame=frame,
                        start_ms=float(active["start_ms"]),
                        remaining_bytes=float(active["remaining_bytes"]),
                        outage_pause_ms=float(active.get("outage_pause_ms", 0.0)),
                    )
            scheduler._finished = not any(
                runtime.active is not None or runtime.next_index < len(runtime.stream.frames)
                for runtime in scheduler.runtime.values()
            )
            return scheduler
        except AquaError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise AquaError(
                "E_SNAPVERSION",
                f"快照字段缺失或损坏，期望版本 {SNAPSHOT_VERSION}",
                {"reason": str(exc), "expected": SNAPSHOT_VERSION},
            ) from exc

    def resume(self) -> "Scheduler":
        return self.run()
