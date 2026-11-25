from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .model import TraceRecord, Window


@dataclass
class RequestRange:
    instance_id: str
    first_seq: int
    last_seq: int
    count: int
    first_source_line: int
    last_source_line: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "first_request_seq": self.first_seq,
            "last_request_seq": self.last_seq,
            "request_count": self.count,
            "first_source_line": self.first_source_line,
            "last_source_line": self.last_source_line,
        }


@dataclass
class WindowAggregate:
    window: Window
    request_count: int = 0
    success_count: int = 0
    dropped_count: int = 0
    total_duration_ms: Decimal = Decimal(0)
    durations_ms: list[Decimal] = field(default_factory=list)
    ranges: dict[str, RequestRange] = field(default_factory=dict)
    max_event_clock_ms: int = 0
    assignment_keys: set[tuple[str, int]] = field(default_factory=set)

    def add(self, record: TraceRecord) -> None:
        assignment_key = (record.instance_id, record.request_seq)
        if assignment_key in self.assignment_keys:
            raise AssertionError(f"request assigned twice: {assignment_key}")
        self.assignment_keys.add(assignment_key)
        self.request_count += 1
        if record.success:
            self.success_count += 1
        if record.dropped:
            self.dropped_count += 1
        self.total_duration_ms += record.duration_ms
        self.durations_ms.append(record.duration_ms)
        self.max_event_clock_ms = max(self.max_event_clock_ms, record.logical_clock_ms)

        existing = self.ranges.get(record.instance_id)
        if existing is None:
            self.ranges[record.instance_id] = RequestRange(
                instance_id=record.instance_id,
                first_seq=record.request_seq,
                last_seq=record.request_seq,
                count=1,
                first_source_line=record.source_line,
                last_source_line=record.source_line,
            )
        else:
            existing.last_seq = record.request_seq
            existing.last_source_line = record.source_line
            existing.count += 1

    def summary(self) -> dict[str, Any]:
        return {
            "scope_type": self.window.scope_type,
            "scope_id": self.window.scope_id,
            "window_index": self.window.window_index,
            "start_ms": self.window.start_ms,
            "end_ms": self.window.end_ms,
            "request_count": self.request_count,
            "success_count": self.success_count,
            "dropped_count": self.dropped_count,
            "total_duration_ms": format(self.total_duration_ms, "f"),
            "max_event_clock_ms": self.max_event_clock_ms,
            "request_ranges": [item.to_dict() for item in sorted(self.ranges.values(), key=lambda x: x.instance_id)],
        }


def window_index_for_clock(clock_ms: int, origin_ms: int, step_ms: int) -> int:
    return (clock_ms - origin_ms) // step_ms


def make_window(
    scope_type: str,
    scope_id: str,
    index: int,
    origin_ms: int,
    length_ms: int,
    step_ms: int,
) -> Window:
    start = origin_ms + index * step_ms
    return Window(
        scope_type=scope_type,
        scope_id=scope_id,
        window_index=index,
        start_ms=start,
        end_ms=start + length_ms,
        step_ms=step_ms,
    )


def aggregate_trace(
    records: list[TraceRecord],
    *,
    origin_ms: int,
    length_ms: int,
    step_ms: int,
    expected_window_count: int | None = None,
    service_filter: str | None = None,
) -> dict[str, dict[int, WindowAggregate]]:
    """Group records into unique aligned ownership windows.

    A request is owned by ``floor((clock-origin)/step)``.  When length equals
    step (the delivered deterministic sample), windows are ordinary contiguous
    tumbling windows.  For length > step the nominal interval is retained, but
    the ownership key still prevents double counting.
    """
    aggregates: dict[str, dict[int, WindowAggregate]] = defaultdict(dict)
    global_indexes: set[int] = set(range(expected_window_count or 0))
    assigned: dict[tuple[str, int], tuple[str, int]] = {}

    scoped_records = [
        record
        for record in records
        if service_filter is None or record.service_name == service_filter
    ]

    for record in scoped_records:
        index = window_index_for_clock(record.logical_clock_ms, origin_ms, step_ms)
        global_indexes.add(index)
        ownership = (record.instance_id, record.request_seq)
        previous = assigned.get(ownership)
        if previous is not None and previous != (record.service_name, index):
            raise AssertionError(f"request window assignment conflict: {ownership}")
        assigned[ownership] = (record.service_name, index)

        scopes = [
            ("instance", record.instance_id),
            ("service", record.service_name),
        ]
        for scope_type, scope_id in scopes:
            key = f"{scope_type}:{scope_id}"
            bucket = aggregates[key]
            aggregate = bucket.get(index)
            if aggregate is None:
                aggregate = WindowAggregate(
                    make_window(
                        scope_type,
                        scope_id,
                        index,
                        origin_ms,
                        length_ms,
                        step_ms,
                    )
                )
                bucket[index] = aggregate
            aggregate.add(record)

    if not global_indexes:
        return {}

    min_index = min(global_indexes)
    max_index = max(global_indexes)
    if expected_window_count is not None:
        max_index = max(max_index, expected_window_count - 1)

    # Ensure contiguous windows for every scope touched by this service.
    scope_ids: set[tuple[str, str]] = set()
    for record in scoped_records:
        scope_ids.add(("instance", record.instance_id))
        scope_ids.add(("service", record.service_name))

    for scope_type, scope_id in scope_ids:
        key = f"{scope_type}:{scope_id}"
        bucket = aggregates[key]
        for index in range(min_index, max_index + 1):
            if index not in bucket:
                bucket[index] = WindowAggregate(
                    make_window(
                        scope_type,
                        scope_id,
                        index,
                        origin_ms,
                        length_ms,
                        step_ms,
                    )
                )

    return dict(aggregates)
