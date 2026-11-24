"""Boundary evaluation and canonical result normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from .codes import (
    E_ACCESS_SEQ_DUPLICATE,
    E_ACCESS_SEQ_GAP,
    E_LIFETIME_ANCESTOR,
    E_LIFETIME_SELF,
    E_PARENT_CYCLE,
    E_PARENT_MISSING,
    OK,
    OOB_ANCESTOR_CONTAINED,
    OOB_CROSS_ANCESTOR,
    OOB_ENDPOINT_TOUCH,
    OOB_OUTSIDE_ANCESTORS,
    OOB_ZERO_LENGTH,
)
from .models import Region, Rejection, TraceRecord, Verdict


@dataclass(frozen=True)
class ChainError:
    business_code: str
    region_id: str
    ancestor_path: tuple[str, ...]
    cycle_region_ids: tuple[str, ...] = ()
    missing_parent_region_id: Optional[str] = None


def _resolve_chain(
    region_id: str, regions: Mapping[str, Region]
) -> tuple[tuple[Region, ...], Optional[ChainError]]:
    first = regions.get(region_id)
    if first is None:
        return (), ChainError("E_REGION_UNKNOWN", region_id, ())

    path: list[Region] = [first]
    positions = {region_id: 0}
    current = first
    while current.parent_region_id is not None:
        parent_id = current.parent_region_id
        parent = regions.get(parent_id)
        if parent is None:
            return tuple(path), ChainError(
                E_PARENT_MISSING,
                region_id,
                tuple(item.region_id for item in path),
                missing_parent_region_id=parent_id,
            )
        if parent_id in positions:
            cycle = tuple(
                sorted(item.region_id for item in path[positions[parent_id] :])
            )
            return tuple(path), ChainError(
                E_PARENT_CYCLE,
                region_id,
                tuple(item.region_id for item in path),
                cycle_region_ids=cycle,
            )
        positions[parent_id] = len(path)
        path.append(parent)
        current = parent
    return tuple(path), None


def region_topology_diagnostics(regions: Mapping[str, Region]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for region_id in sorted(regions):
        _, error = _resolve_chain(region_id, regions)
        if error is None:
            continue
        key = (
            error.business_code,
            error.ancestor_path,
            error.cycle_region_ids,
            error.missing_parent_region_id,
        )
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(
            {
                "region_id": region_id,
                "business_code": error.business_code,
                "ancestor_path": list(error.ancestor_path),
                "cycle_region_ids": list(error.cycle_region_ids),
                "missing_parent_region_id": error.missing_parent_region_id,
            }
        )
    diagnostics.sort(
        key=lambda item: (
            item["region_id"],
            item["business_code"],
            tuple(item["ancestor_path"]),
        )
    )
    return diagnostics


def _active(region: Region, timestamp_ms: int) -> bool:
    return region.lifetime_start_ms <= timestamp_ms < region.lifetime_end_ms


def _intersection(
    left: tuple[int, int], right: tuple[int, int]
) -> Optional[tuple[int, int]]:
    start = max(left[0], right[0])
    end = min(left[1], right[1])
    if start > end:
        return None
    return (start, end)


def _oob_ranges(
    access: tuple[int, int], region: tuple[int, int]
) -> list[dict[str, Any]]:
    access_start, access_end = access
    region_start, region_end = region
    ranges: list[dict[str, Any]] = []
    if access_start < region_start:
        ranges.append(
            {
                "side": "before_region",
                "start": access_start,
                "end": min(access_end, region_start),
            }
        )
    if access_end > region_end:
        ranges.append(
            {
                "side": "after_region",
                "start": max(access_start, region_end),
                "end": access_end,
            }
        )
    return ranges


def _base_evidence(record: TraceRecord, region: Region) -> dict[str, Any]:
    access = record.access_interval
    allowed = _intersection(access, region.interval)
    ranges = _oob_ranges(access, region.interval)
    if record.access_size == 0:
        first_oob = record.base_offset
    elif ranges:
        first_oob = int(ranges[0]["start"])
    else:
        first_oob = None
    return {
        "access_seq": record.access_seq,
        "trace_id": record.trace_id,
        "region_id": record.region_id,
        "access_kind": record.access_kind,
        "timestamp_ms": record.timestamp_ms,
        "access_interval": [access[0], access[1]],
        "region_interval": [region.start_offset, region.end_offset],
        "allowed_intersection": None if allowed is None else [allowed[0], allowed[1]],
        "oob_ranges": ranges,
        "first_oob_offset": first_oob,
        "low_oob_bytes": max(0, region.start_offset - access[0]),
        "high_oob_bytes": max(0, access[1] - region.end_offset),
        "ancestor_path": [],
        "ancestor_region_id": None,
        "overlapping_ancestor_region_ids": [],
        "cycle_region_ids": [],
        "missing_parent_region_id": None,
        "zero_length_offset": record.base_offset if record.access_size == 0 else None,
    }


def evaluate_access(record: TraceRecord, regions: Mapping[str, Region]) -> Verdict:
    region = regions[record.region_id]
    chain, chain_error = _resolve_chain(record.region_id, regions)
    evidence = _base_evidence(record, region)

    if chain_error is not None:
        evidence["ancestor_path"] = list(chain_error.ancestor_path)
        evidence["cycle_region_ids"] = list(chain_error.cycle_region_ids)
        evidence["missing_parent_region_id"] = chain_error.missing_parent_region_id
        return Verdict(
            trace_id=record.trace_id,
            access_seq=record.access_seq,
            region_id=record.region_id,
            arena_id=region.arena_id,
            access_kind=record.access_kind,
            business_code=chain_error.business_code,
            status="error",
            evidence=evidence,
            access_end=record.access_interval[1],
            source_file=record.source_file,
            line_number=record.line_number,
        )

    ancestor_path = [item.region_id for item in chain]
    evidence["ancestor_path"] = ancestor_path

    if not _active(region, record.timestamp_ms):
        return Verdict(
            trace_id=record.trace_id,
            access_seq=record.access_seq,
            region_id=record.region_id,
            arena_id=region.arena_id,
            access_kind=record.access_kind,
            business_code=E_LIFETIME_SELF,
            status="error",
            evidence=evidence,
            access_end=record.access_interval[1],
            source_file=record.source_file,
            line_number=record.line_number,
        )

    for ancestor in chain[1:]:
        if not _active(ancestor, record.timestamp_ms):
            evidence["ancestor_region_id"] = ancestor.region_id
            return Verdict(
                trace_id=record.trace_id,
                access_seq=record.access_seq,
                region_id=record.region_id,
                arena_id=region.arena_id,
                access_kind=record.access_kind,
                business_code=E_LIFETIME_ANCESTOR,
                status="error",
                evidence=evidence,
                access_end=record.access_interval[1],
                source_file=record.source_file,
                line_number=record.line_number,
            )

    access_start, access_end = record.access_interval
    region_start, region_end = region.interval
    code: str
    status = "ok"

    if record.access_size == 0:
        code = OOB_ZERO_LENGTH
        status = "out_of_bounds"
    elif access_end == region_start or access_start == region_end:
        code = OOB_ENDPOINT_TOUCH
        status = "out_of_bounds"
    elif access_start >= region_start and access_end <= region_end:
        code = OK
    else:
        fully_containing = [
            item
            for item in chain[1:]
            if access_start >= item.start_offset and access_end <= item.end_offset
        ]
        if fully_containing:
            ancestor = fully_containing[0]
            code = OOB_ANCESTOR_CONTAINED
            status = "out_of_bounds"
            evidence["ancestor_region_id"] = ancestor.region_id
        else:
            overlapping = [
                item
                for item in chain[1:]
                if _intersection(record.access_interval, item.interval) is not None
            ]
            if overlapping:
                code = OOB_CROSS_ANCESTOR
                status = "out_of_bounds"
                evidence["ancestor_region_id"] = overlapping[0].region_id
                evidence["overlapping_ancestor_region_ids"] = [
                    item.region_id for item in overlapping
                ]
            else:
                code = OOB_OUTSIDE_ANCESTORS
                status = "out_of_bounds"

    return Verdict(
        trace_id=record.trace_id,
        access_seq=record.access_seq,
        region_id=record.region_id,
        arena_id=region.arena_id,
        access_kind=record.access_kind,
        business_code=code,
        status=status,
        evidence=evidence,
        access_end=record.access_interval[1],
        source_file=record.source_file,
        line_number=record.line_number,
    )


def select_unique_records(
    records: Iterable[TraceRecord],
) -> tuple[list[TraceRecord], list[Rejection], list[dict[str, Any]]]:
    grouped: dict[int, list[TraceRecord]] = {}
    for record in records:
        grouped.setdefault(record.access_seq, []).append(record)

    unique: list[TraceRecord] = []
    rejections: list[Rejection] = []
    duplicate_counts: dict[int, int] = {}
    for access_seq in sorted(grouped):
        candidates = sorted(grouped[access_seq], key=lambda item: item.stable_input_key)
        chosen = candidates[0]
        unique.append(chosen)
        if len(candidates) > 1:
            duplicate_counts[access_seq] = len(candidates) - 1
            for duplicate in candidates[1:]:
                rejections.append(
                    Rejection(
                        source_file=duplicate.source_file,
                        line_number=duplicate.line_number,
                        business_code=E_ACCESS_SEQ_DUPLICATE,
                        message="duplicate access_seq ignored; first stable occurrence is authoritative",
                        detail={
                            "access_seq": access_seq,
                            "authoritative_source_file": chosen.source_file,
                            "authoritative_line_number": chosen.line_number,
                            "duplicate_trace_id": duplicate.trace_id,
                            "duplicates_for_seq": len(candidates) - 1,
                        },
                    )
                )

    unique.sort(key=lambda item: (item.access_seq, item.stable_input_key))
    gaps = _sequence_gaps([item.access_seq for item in unique])
    gap_diagnostics: list[dict[str, Any]] = []
    if gaps:
        ranges = [{"start": first, "end": last} for first, last in gaps]
        gap_diagnostics.append(
            {
                "business_code": E_ACCESS_SEQ_GAP,
                "missing_ranges": ranges,
                "missing_count": sum(last - first + 1 for first, last in gaps),
            }
        )

    duplicate_diagnostics = [
        {
            "business_code": E_ACCESS_SEQ_DUPLICATE,
            "access_seq": seq,
            "duplicate_count": count,
        }
        for seq, count in sorted(duplicate_counts.items())
    ]
    return unique, rejections, gap_diagnostics + duplicate_diagnostics


def _sequence_gaps(values: Sequence[int]) -> list[tuple[int, int]]:
    if not values:
        return []
    first_expected = 0 if values[0] == 0 else 1
    gaps: list[tuple[int, int]] = []
    expected = first_expected
    for value in values:
        if value > expected:
            candidate_end = value - 1
            if gaps and gaps[-1][1] + 1 == expected:
                gaps[-1] = (gaps[-1][0], candidate_end)
            else:
                gaps.append((expected, candidate_end))
        expected = max(expected, value + 1)
    return gaps


def verdict_counts(verdicts: Sequence[Verdict]) -> dict[str, int]:
    counts = {"total": 0, "ok": 0, "out_of_bounds": 0, "error": 0}
    for verdict in verdicts:
        counts["total"] += 1
        counts[verdict.status] = counts.get(verdict.status, 0) + 1
    return counts
