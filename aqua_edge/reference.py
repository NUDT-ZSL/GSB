"""A separate, deliberately small reference scheduler.

This implementation is intentionally independent from ``scheduler.Scheduler``
and supports only full-run scheduling. It is used by ``aqua-edge verify`` to
check start/end times and queueing delays without sharing scheduling code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import Project


EPS = 1e-7


@dataclass
class _Active:
    stream_id: str
    frame_id: str
    sequence: int
    start: float
    remaining: float


@dataclass
class _Credit:
    stream_id: str
    frame_id: str
    size: int
    ready_at: float
    read_from: float
    read_to: float
    return_at: float
    returned: bool = False


class ReferenceScheduler:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.next_index = {stream.stream_id: 0 for stream in project.streams}
        self.windows = {
            stream.stream_id: float(stream.window_bytes) for stream in project.streams
        }
        self.active: dict[str, _Active] = {}
        self.credits: list[_Credit] = []
        self.now = 0.0
        self.rate_index = 0
        self.read_rate = project.consumer.read_bytes_per_s
        self.read_busy = 0.0
        self.finished: list[dict[str, Any]] = []
        self.completed = 0

    def run(self) -> list[dict[str, Any]]:
        while True:
            self._apply_rate_events()
            self._return_credits()
            self._finish_due()
            self._admit()
            nxt = self._next_time()
            if nxt is None:
                break
            dt = nxt - self.now
            if dt > 0 and not self._outage():
                if self.active:
                    per_ms = self.project.network.uplink_bytes_per_ms / len(self.active)
                    used = per_ms * dt
                    for item in self.active.values():
                        item.remaining = max(0.0, item.remaining - used)
            self.now = nxt
        return self.finished

    def _apply_rate_events(self) -> None:
        events = self.project.consumer.events
        while self.rate_index < len(events) and events[self.rate_index].time_ms <= self.now + EPS:
            event = events[self.rate_index]
            self.read_rate = event.read_bytes_per_s
            pending = [
                credit
                for credit in self.credits
                if not credit.returned and credit.read_to > event.time_ms + EPS
            ]
            pending.sort(key=lambda credit: (credit.ready_at, credit.frame_id))
            already = [
                credit.read_to
                for credit in self.credits
                if not credit.returned and credit.read_to <= event.time_ms + EPS
            ]
            cursor = max([event.time_ms, *already])
            rate = self.read_rate / 1000.0
            for credit in pending:
                if credit.read_from < event.time_ms < credit.read_to:
                    fraction = (credit.read_to - event.time_ms) / (
                        credit.read_to - credit.read_from
                    )
                    left = credit.size * max(0.0, min(1.0, fraction))
                    start = event.time_ms
                else:
                    left = float(credit.size)
                    start = max(event.time_ms, credit.ready_at, cursor)
                credit.read_from = start
                credit.read_to = start + left / rate
                credit.return_at = credit.read_to + self.project.network.rtt_ms
                cursor = credit.read_to
            self.read_busy = cursor
            self.rate_index += 1

    def _return_credits(self) -> None:
        for credit in self.credits:
            if credit.returned or credit.return_at > self.now + EPS:
                continue
            credit.returned = True
            self.windows[credit.stream_id] = min(
                float(self.project.stream_map()[credit.stream_id].window_bytes),
                self.windows[credit.stream_id] + credit.size,
            )

    def _finish_due(self) -> None:
        due = [
            (stream_id, item)
            for stream_id, item in self.active.items()
            if item.remaining <= EPS
        ]
        for stream_id, item in sorted(due, key=lambda pair: pair[0]):
            stream = self.project.stream_map()[stream_id]
            frame = stream.frames[item.sequence]
            del self.active[stream_id]
            self.finished.append(
                {
                    "frame_id": frame.frame_id,
                    "stream_id": stream_id,
                    "logical_time_ms": frame.logical_time_ms,
                    "start_ms": item.start,
                    "end_ms": self.now,
                    "queue_delay_ms": max(0.0, item.start - frame.logical_time_ms),
                    "status": "SENT",
                }
            )
            self.completed += 1
            self._new_credit(stream_id, frame.frame_id, frame.length_bytes)

    def _new_credit(self, stream_id: str, frame_id: str, size: int) -> None:
        rate = self.read_rate / 1000.0
        read_from = max(self.now, self.read_busy)
        read_to = read_from + size / rate
        self.credits.append(
            _Credit(
                stream_id=stream_id,
                frame_id=frame_id,
                size=size,
                ready_at=self.now,
                read_from=read_from,
                read_to=read_to,
                return_at=read_to + self.project.network.rtt_ms,
            )
        )
        self.read_busy = max(self.read_busy, read_to)

    def _admit(self) -> None:
        if self._outage():
            return
        free = self.project.network.max_concurrent_streams - len(self.active)
        if free <= 0:
            return
        candidates: list[tuple[Any, Any]] = []
        for stream in self.project.streams:
            index = self.next_index[stream.stream_id]
            if stream.stream_id in self.active or index >= len(stream.frames):
                continue
            frame = stream.frames[index]
            if frame.logical_time_ms > self.now + EPS or not frame.ready:
                continue
            if self.windows[stream.stream_id] + EPS < frame.length_bytes:
                continue
            candidates.append((stream, frame))
        candidates.sort(key=lambda pair: (-pair[0].priority, pair[0].stream_id, pair[1].sequence))
        for stream, frame in candidates[:free]:
            self.next_index[stream.stream_id] += 1
            self.windows[stream.stream_id] = max(
                0.0, self.windows[stream.stream_id] - frame.length_bytes
            )
            self.active[stream.stream_id] = _Active(
                stream_id=stream.stream_id,
                frame_id=frame.frame_id,
                sequence=frame.sequence,
                start=self.now,
                remaining=float(frame.length_bytes),
            )

    def _next_time(self) -> float | None:
        times: list[float] = []
        if not self._outage() and self.active:
            per_ms = self.project.network.uplink_bytes_per_ms / len(self.active)
            for item in self.active.values():
                times.append(self.now + max(EPS, item.remaining / per_ms))
        for credit in self.credits:
            if not credit.returned and credit.return_at > self.now + EPS:
                times.append(credit.return_at)
            if not credit.returned and credit.read_to > self.now + EPS:
                times.append(credit.read_to)
        events = self.project.consumer.events
        if self.rate_index < len(events):
            event_time = events[self.rate_index].time_ms
            if event_time > self.now + EPS:
                times.append(event_time)
        for outage in self.project.outages:
            if outage.start_ms > self.now + EPS:
                times.append(outage.start_ms)
            if outage.end_ms > self.now + EPS:
                times.append(outage.end_ms)
        for stream in self.project.streams:
            index = self.next_index[stream.stream_id]
            if index >= len(stream.frames):
                continue
            frame_time = stream.frames[index].logical_time_ms
            if frame_time > self.now + EPS:
                times.append(frame_time)
        future = [time for time in times if time > self.now + EPS]
        return min(future) if future else None

    def _outage(self) -> bool:
        return any(
            outage.start_ms - EPS <= self.now < outage.end_ms
            for outage in self.project.outages
        )

