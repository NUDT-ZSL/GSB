"""Cross-version semantic merge with auditable arbitration evidence."""

from __future__ import annotations

from typing import Any

from .errors import LOGICAL_CLOCK_INVALID
from .util import require_nonnegative_int

V1_PRIORITY = 1
V2_PRIORITY = 2


def v1_candidate(
    entry: dict[str, Any],
    *,
    source_clock_ms: int,
    anchor_clock_ms: int,
    ms_per_second: int,
    rounding: str,
) -> dict[str, Any]:
    elapsed_ms = entry["relative_effective_seconds"] * ms_per_second
    absolute_ms = anchor_clock_ms + elapsed_ms
    discarded_ms = 0
    return {
        "message_key": entry["message_key"],
        "topic": entry.get("topic"),
        "delivery_alias": entry["delivery_alias"],
        "semantic_generation": entry["semantic_generation"],
        "logical_clock_ms": source_clock_ms,
        "source_version": "v1",
        "source_priority": V1_PRIORITY,
        "effective_ms": absolute_ms,
        "original_time": {
            "type": "relative_effective_seconds",
            "value": entry["relative_effective_seconds"],
            "snapshot_logical_clock_ms": source_clock_ms,
        },
        "conversion": {
            "kind": "relative-seconds-to-absolute-ms",
            "anchor_logical_clock_ms": anchor_clock_ms,
            "scale_ms_per_second": ms_per_second,
            "rounding": rounding,
            "computed_elapsed_ms": elapsed_ms,
            "absolute_effective_ms": absolute_ms,
            "discarded_remainder_ms": discarded_ms,
        },
        "payload_digest": entry.get("payload_digest"),
        "dependency": None,
        "tombstone": None,
        "source_shard_id": None,
    }


def v2_candidate(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_key": entry["message_key"],
        "topic": entry.get("topic"),
        "delivery_alias": entry["delivery_alias"],
        "semantic_generation": entry["semantic_generation"],
        "logical_clock_ms": entry["logical_clock_ms"],
        "source_version": "v2",
        "source_priority": V2_PRIORITY,
        "effective_ms": entry["absolute_effective_ms"],
        "original_time": {
            "type": "absolute_effective_ms",
            "value": entry["absolute_effective_ms"],
            "logical_clock_ms": entry["logical_clock_ms"],
            "shard_id": entry["source_shard_id"],
        },
        "conversion": {
            "kind": "identity",
            "absolute_effective_ms": entry["absolute_effective_ms"],
            "rounding": "none",
            "discarded_remainder_ms": 0,
        },
        "payload_digest": entry.get("payload_digest"),
        "dependency": entry.get("dependency"),
        "tombstone": entry.get("tombstone"),
        "source_shard_id": entry["source_shard_id"],
    }


def arbitration_key(candidate: dict[str, Any]) -> tuple[int, int, int]:
    """Deterministic latest-version ordering.

    Logical time is primary, semantic generation breaks equal-time candidates,
    and source-format priority is the final tie-breaker (v2 over v1).
    """

    return (
        candidate["logical_clock_ms"],
        candidate["semantic_generation"],
        candidate["source_priority"],
    )


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_version": candidate["source_version"],
        "source_shard_id": candidate["source_shard_id"],
        "logical_clock_ms": candidate["logical_clock_ms"],
        "semantic_generation": candidate["semantic_generation"],
        "source_priority": candidate["source_priority"],
        "effective_ms": candidate["effective_ms"],
        "original_time": candidate["original_time"],
    }


def merge_snapshots(
    v1: dict[str, Any],
    v2: dict[str, Any],
    *,
    injected_logical_clock_ms: Any,
) -> dict[str, Any]:
    anchor_clock_ms = require_nonnegative_int(
        injected_logical_clock_ms,
        path="injected_logical_clock_ms",
        code=LOGICAL_CLOCK_INVALID,
    )
    candidates: list[dict[str, Any]] = []
    conversions: list[dict[str, Any]] = []
    for entry in v1["entries"]:
        candidate = v1_candidate(
            entry,
            source_clock_ms=v1["logical_clock_ms"],
            anchor_clock_ms=anchor_clock_ms,
            ms_per_second=v1["ms_per_second"],
            rounding=v1["rounding"],
        )
        candidates.append(candidate)
        conversions.append(
            {
                "message_key": candidate["message_key"],
                "original_time": candidate["original_time"],
                "conversion": candidate["conversion"],
                "logical_clock_ms": candidate["logical_clock_ms"],
                "semantic_generation": candidate["semantic_generation"],
            }
        )
    for entry in v2["entries"]:
        candidates.append(v2_candidate(entry))

    by_key: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_key.setdefault(candidate["message_key"], []).append(candidate)

    merged_entries: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for message_key in sorted(by_key):
        group = by_key[message_key]
        winner = max(group, key=arbitration_key)
        losers = sorted(group, key=arbitration_key, reverse=True)[1:]
        decision = {
            "message_key": message_key,
            "decision": "latest-version",
            "basis": "max(logical_clock_ms, semantic_generation, source_priority)",
            "winner": _candidate_summary(winner),
            "overridden": [_candidate_summary(candidate) for candidate in losers],
        }
        decisions.append(decision)
        merged_entries.append(
            {
                "message_key": message_key,
                "topic": winner["topic"],
                "delivery_alias": winner["delivery_alias"],
                "semantic_generation": winner["semantic_generation"],
                "effective_ms": winner["effective_ms"],
                "source_version": winner["source_version"],
                "source_shard_id": winner["source_shard_id"],
                "logical_clock_ms": winner["logical_clock_ms"],
                "original_time": winner["original_time"],
                "conversion": winner["conversion"],
                "payload_digest": winner["payload_digest"],
                "dependency": winner["dependency"],
                "tombstone": winner["tombstone"],
                "arbitration": decision,
                "source_origins": [
                    _candidate_summary(candidate)
                    for candidate in sorted(group, key=arbitration_key, reverse=True)
                ],
            }
        )

    decision_counts = {
        "v1_only": sum(
            1 for entry in merged_entries if entry["source_version"] == "v1"
        ),
        "v2_only": sum(
            1 for entry in merged_entries if entry["source_version"] == "v2"
        ),
        "conflicts": sum(
            1 for group in by_key.values() if len(group) > 1
        ),
    }
    return {
        "schema": "seqledger-unified-view/v1",
        "injected_logical_clock_ms": anchor_clock_ms,
        "priority": {
            "v1": V1_PRIORITY,
            "v2": V2_PRIORITY,
            "rule": "v2 wins only when logical clock and generation also tie",
        },
        "arbitration_basis": "max(logical_clock_ms, semantic_generation, source_priority)",
        "entries": merged_entries,
        "decisions": decisions,
        "conversions": conversions,
        "decision_counts": decision_counts,
        "v1_source": {
            "logical_clock_ms": v1["logical_clock_ms"],
            "ms_per_second": v1["ms_per_second"],
            "rounding": v1["rounding"],
            "entry_count": len(v1["entries"]),
        },
        "v2_source": {
            "current_generation": v2["current_generation"],
            "shards": v2["shards"],
            "entry_count": len(v2["entries"]),
            "manifest_source": v2["manifest_source"],
            "meta_path": v2["meta_path"],
        },
    }


def merged_entry_index(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["message_key"]: entry for entry in view.get("entries", [])}
