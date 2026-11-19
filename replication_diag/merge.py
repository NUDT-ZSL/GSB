"""Cross-version position arbitration and migration diagnostics."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .config import Config
from .errors import DiagnosticsError
from .models import PositionRecord


def arbitration_key(record: PositionRecord) -> tuple[int, int, int]:
    """Higher tuples win.

    The logical clock is authoritative, then the replay generation, then the
    source-format priority (v2 over v1).  The final content digest is not part of
    the business arbitration key: equal-key records with different content are
    rejected instead of being selected by load order.
    """

    return (record.logical_clock, record.replay_generation, record.source_priority)


def merge_records(
    v1_records: Iterable[PositionRecord],
    v2_records: Iterable[PositionRecord],
    config: Config,
) -> list[PositionRecord]:
    groups: dict[int, list[PositionRecord]] = defaultdict(list)
    for record in list(v1_records) + list(v2_records):
        groups[record.tx_num].append(record)

    merged: list[PositionRecord] = []
    for tx_num in sorted(groups):
        candidates = groups[tx_num]
        by_source: dict[str, list[PositionRecord]] = defaultdict(list)
        for candidate in candidates:
            by_source[candidate.source_version].append(candidate)
        for source, source_candidates in by_source.items():
            if len(source_candidates) > 1:
                raise DiagnosticsError(
                    "E_DUPLICATE_TX",
                    location=source_candidates[1].location,
                    details={"tx_id": source_candidates[1].tx_id, "source_version": source},
                )

        ordered = sorted(
            candidates,
            key=lambda item: (
                -item.logical_clock,
                -item.replay_generation,
                -item.source_priority,
                item.content_digest,
            ),
        )
        top = ordered[0]
        top_key = arbitration_key(top)
        tied = [item for item in ordered if arbitration_key(item) == top_key]
        if len(tied) > 1:
            digests = {item.content_digest for item in tied}
            if len(digests) > 1:
                raise DiagnosticsError(
                    "E_AMBIGUOUS_TX",
                    location=top.location,
                    details={
                        "tx_id": top.tx_id,
                        "arbitration_key": {
                            "logical_clock": top.logical_clock,
                            "replay_generation": top.replay_generation,
                            "source_priority": top.source_priority,
                        },
                        "content_digests": sorted(digests),
                    },
                )
        merged.append(top)

    merged.sort(key=lambda item: item.tx_num)
    if config.require_contiguous and len(merged) > 1:
        for left, right in zip(merged, merged[1:]):
            if right.tx_num != left.tx_num + 1:
                raise DiagnosticsError(
                    "E_POSITION_GAP",
                    location=right.location,
                    details={
                        "previous_tx_id": left.tx_id,
                        "current_tx_id": right.tx_id,
                        "missing_count": right.tx_num - left.tx_num - 1,
                    },
                )

    if config.strict_clock_order and len(merged) > 1:
        for left, right in zip(merged, merged[1:]):
            if right.logical_clock < left.logical_clock:
                raise DiagnosticsError(
                    "E_CLOCK_ROLLBACK",
                    location=right.location,
                    details={
                        "previous_tx_id": left.tx_id,
                        "current_tx_id": right.tx_id,
                        "previous_logical_clock": left.logical_clock,
                        "current_logical_clock": right.logical_clock,
                    },
                )

    return merged


def selected_migrations(merged: list[PositionRecord]) -> list[dict]:
    decisions = [
        {"tx_id": record.tx_id, **dict(record.migration or {})}
        for record in merged
        if record.migration is not None
    ]
    decisions.sort(key=lambda item: item["tx_id"])
    return decisions
