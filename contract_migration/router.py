"""Inclusive range routing plus an intentionally simple reference router."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from .errors import ContractMigrationError
from .model import RouteSegment, contract_id, contract_index


def _segments(
    shards: list[tuple[str, str, str]],
) -> list[RouteSegment]:
    return [RouteSegment(*entry) for entry in shards]


def normalize_segments(segments: list[RouteSegment]) -> list[RouteSegment]:
    ordered = sorted(segments, key=lambda item: item.start)
    normalized: list[RouteSegment] = []
    for segment in ordered:
        if segment.start > segment.end:
            raise ContractMigrationError(
                "OVERLAPPING_ROUTE",
                f"invalid range {segment.start}..{segment.end}",
            )
        if normalized and segment.start <= normalized[-1].end:
            raise ContractMigrationError(
                "OVERLAPPING_ROUTE",
                f"route segment {segment.start} overlaps {normalized[-1].end}",
            )
        if normalized and normalized[-1].shard_id == segment.shard_id:
            previous = normalized[-1]
            if contract_index(segment.start) != contract_index(previous.end) + 1:
                raise ContractMigrationError(
                    "ROUTE_GAP",
                    f"gap between {previous.end} and {segment.start}",
                )
            normalized[-1] = RouteSegment(
                previous.start, segment.end, segment.shard_id
            )
        else:
            normalized.append(segment)
    return normalized


class RangeRouter:
    """Binary-search range router with atomic table replacement."""

    def __init__(self, segments: list[RouteSegment]):
        self._segments = normalize_segments(segments)
        self._starts = [segment.start for segment in self._segments]
        self.replacement_count = 0

    @classmethod
    def from_shards(cls, shards: list) -> "RangeRouter":
        return cls(
            [
                RouteSegment(shard.start_key, shard.end_key, shard.shard_id)
                for shard in shards
            ]
        )

    @property
    def segments(self) -> list[RouteSegment]:
        return list(self._segments)

    def route(self, key: str) -> str:
        position = bisect_right(self._starts, key) - 1
        if position < 0:
            raise ContractMigrationError("ROUTE_GAP", f"no route for {key}")
        segment = self._segments[position]
        if key > segment.end:
            raise ContractMigrationError("ROUTE_GAP", f"no route for {key}")
        return segment.shard_id

    def replace_range(self, start: str, end: str, shard_id: str) -> dict:
        if start > end:
            raise ContractMigrationError(
                "OVERLAPPING_ROUTE", f"invalid replacement {start}..{end}"
            )
        before = self.segments
        replacement: list[RouteSegment] = []
        covered_start: str | None = None
        covered_end: str | None = None
        for segment in self._segments:
            if segment.end < start or segment.start > end:
                replacement.append(segment)
                continue
            if segment.start < start:
                replacement.append(RouteSegment(segment.start, _previous(start), segment.shard_id))
            if covered_start is None:
                covered_start = max(segment.start, start)
            covered_end = min(segment.end, end)
            replacement.append(
                RouteSegment(max(segment.start, start), min(segment.end, end), shard_id)
            )
            if segment.end > end:
                replacement.append(RouteSegment(_next(end), segment.end, segment.shard_id))
        if covered_start is None or covered_end is None:
            raise ContractMigrationError(
                "ROUTE_GAP", f"replacement range {start}..{end} is not fully routed"
            )
        if covered_start > start or covered_end < end:
            raise ContractMigrationError(
                "ROUTE_GAP", f"replacement range {start}..{end} crosses a route gap"
            )
        new_segments = normalize_segments(replacement)
        self._segments = new_segments
        self._starts = [segment.start for segment in new_segments]
        self.replacement_count += 1
        return {
            "before": [segment.as_dict() for segment in before],
            "after": [segment.as_dict() for segment in new_segments],
            "replaced_range": {"start": start, "end": end, "target": shard_id},
        }


class LinearReferenceRouter:
    """Independent linear scan reference; never used to make routing decisions."""

    def __init__(self, segments: list[RouteSegment]):
        self.segments = list(segments)

    def route(self, key: str) -> str:
        matches = [
            segment.shard_id
            for segment in self.segments
            if segment.start <= key <= segment.end
        ]
        if len(matches) != 1:
            raise ContractMigrationError(
                "ROUTE_GAP", f"reference router expected one match for {key}"
            )
        return matches[0]


def _previous(key: str) -> str:
    return contract_id(contract_index(key) - 1)


def _next(key: str) -> str:
    return contract_id(contract_index(key) + 1)


def split_range_by_route(
    start: str, end: str, segments: list[RouteSegment]
) -> list[dict]:
    """Split an inclusive hot range into one slice per existing route segment."""
    slices: list[dict] = []
    for segment in normalize_segments(segments):
        lower = max(start, segment.start)
        upper = min(end, segment.end)
        if lower <= upper:
            slices.append(
                {
                    "start": lower,
                    "end": upper,
                    "source_shard": segment.shard_id,
                }
            )
    if not slices:
        raise ContractMigrationError(
            "ROUTE_GAP", f"hot range {start}..{end} does not intersect a route"
        )
    return slices


def expand_route_table(
    segments: list[RouteSegment], all_contracts: list[str]
) -> dict[str, str]:
    reference = LinearReferenceRouter(segments)
    return {key: reference.route(key) for key in all_contracts}


@dataclass(frozen=True)
class RouteDiff:
    before: list[dict[str, str]]
    after: list[dict[str, str]]
    changed_ranges: list[dict[str, str]]

    def as_dict(self) -> dict:
        return {
            "before": self.before,
            "after": self.after,
            "changed_ranges": self.changed_ranges,
        }
