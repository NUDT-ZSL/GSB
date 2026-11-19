"""Deterministic cross-version merge and unified index construction."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .models import Candidate, SourceInfo, Tombstone
from .util import canonical_json, sha256_text


_RANK_FIELDS = (
    "source_logical_clock_ms",
    "unified_cache_generation",
    "absolute_written_ms",
    "source_format_priority",
    "source_manifest_version",
    "source_id",
    "digests",
    "size_bytes",
    "content_path",
)


def _candidate_rank_values(candidate: Candidate) -> list[Any]:
    return [
        candidate.source_logical_clock_ms,
        candidate.unified_cache_generation,
        candidate.absolute_written_ms,
        2 if candidate.source_version == 2 else 1,
        candidate.source_manifest_version,
        candidate.source_id,
        list(candidate.digests),
        candidate.size_bytes,
        candidate.content_path,
    ]


def _decisive_field(winner: Candidate, loser: Candidate) -> str:
    winner_values = _candidate_rank_values(winner)
    loser_values = _candidate_rank_values(loser)
    for field, winner_value, loser_value in zip(_RANK_FIELDS, winner_values, loser_values):
        if winner_value != loser_value:
            return field
    return "stable_identity"


def candidate_from_entry(entry: dict[str, Any]) -> Candidate:
    source_root = str(entry.get("source_root", "."))
    content_path = str(entry["content_path"])
    return Candidate(
        task_key=str(entry["task_key"]),
        artifact=str(entry["artifact"]),
        original_digests=tuple(str(value) for value in entry.get("original_digests", entry["digests"])),
        size_bytes=int(entry["size_bytes"]),
        absolute_written_ms=int(entry["absolute_written_ms"]),
        source_version=int(entry["source_version"]),
        source_id=str(entry["source_id"]),
        source_manifest_id=str(entry["source_manifest_id"]),
        source_manifest_version=int(entry["source_manifest_version"]),
        source_logical_clock_ms=int(entry["source_logical_clock_ms"]),
        source_cache_generation=entry.get("source_cache_generation"),
        source_hit_tag=entry.get("source_hit_tag"),
        original_time=dict(entry["original_time"]),
        content_path=content_path,
        source_root=source_root,
        resolved_content_path=Path(source_root) / content_path,
    )


def tombstone_from_dict(value: dict[str, Any]) -> Tombstone:
    return Tombstone(
        task_key=str(value["task_key"]),
        artifact=str(value["artifact"]),
        digest=str(value["digest"]),
        invalidated_at_ms=int(value["invalidated_at_ms"]),
        cache_generation=int(value["cache_generation"]),
        source_id=str(value["source_id"]),
    )


def _source_dict(source: SourceInfo) -> dict[str, Any]:
    return source.to_dict()


def _dedupe_sources(sources: Iterable[SourceInfo]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for source in sources:
        by_id[source.source_id] = _source_dict(source)
    return [by_id[source_id] for source_id in sorted(by_id)]


def _dedupe_tombstones(tombstones: Iterable[Tombstone]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for tombstone in tombstones:
        key = (tombstone.task_key, tombstone.artifact, tombstone.digest, tombstone.source_id)
        by_key[key] = tombstone.to_dict()
    return [
        by_key[key]
        for key in sorted(by_key)
    ]


def _migration_decision(
    candidate: Candidate,
    *,
    selected: bool,
) -> dict[str, Any]:
    if candidate.source_version == 1:
        kind = "v1_relative_to_v2_semantics"
        mapping = {
            "from": {
                "time": candidate.original_time,
                "cache_generation": None,
                "hit_tag": None,
            },
            "to": {
                "absolute_written_ms": candidate.absolute_written_ms,
                "unified_cache_generation": candidate.unified_cache_generation,
                "unified_hit_tag": candidate.unified_hit_tag,
            },
            "conversion": {
                "reference_logical_clock_ms": candidate.original_time[
                    "reference_logical_clock_ms"
                ],
                "manifest_logical_clock_ms": candidate.source_logical_clock_ms,
                "scale_ms_per_second": 1000,
                "rounding": "floor",
                "direction": "subtract_from_logical_clock",
            },
        }
    else:
        kind = "v2_absolute_time_preserved"
        mapping = {
            "from": {
                "time": candidate.original_time,
                "cache_generation": candidate.source_cache_generation,
                "hit_tag": candidate.source_hit_tag,
            },
            "to": {
                "absolute_written_ms": candidate.absolute_written_ms,
                "unified_cache_generation": candidate.unified_cache_generation,
                "unified_hit_tag": candidate.unified_hit_tag,
            },
            "conversion": {
                "scale_ms_per_millisecond": 1,
                "rounding": "none",
                "direction": "preserve",
            },
        }
    identity = {
        "kind": kind,
        "source_id": candidate.source_id,
        "task_key": candidate.task_key,
        "artifact": candidate.artifact,
    }
    return {
        "decision_id": "migration:" + sha256_text(canonical_json(identity))[:24],
        **identity,
        **mapping,
        "selected": selected,
        "validity_equivalence": {
            "ttl_basis": "absolute_written_ms",
            "formula": "absolute_written_ms + ttl_seconds * 1000",
            "changed": False,
        },
    }


def _entry_from_candidate(
    candidate: Candidate,
    *,
    matching_tombstones: list[dict[str, Any]],
) -> dict[str, Any]:
    tombstoned = bool(matching_tombstones)
    return {
        "task_key": candidate.task_key,
        "artifact": candidate.artifact,
        "digests": list(candidate.digests),
        "original_digests": list(candidate.original_digests),
        "primary_digest": candidate.digests[0],
        "size_bytes": candidate.size_bytes,
        "absolute_written_ms": candidate.absolute_written_ms,
        "source_version": candidate.source_version,
        "source_id": candidate.source_id,
        "source_manifest_id": candidate.source_manifest_id,
        "source_manifest_version": candidate.source_manifest_version,
        "source_logical_clock_ms": candidate.source_logical_clock_ms,
        "source_cache_generation": candidate.source_cache_generation,
        "unified_cache_generation": candidate.unified_cache_generation,
        "source_hit_tag": candidate.source_hit_tag,
        "unified_hit_tag": candidate.unified_hit_tag,
        "original_time": candidate.original_time,
        "precision_tag": candidate.precision_tag,
        "content_path": candidate.content_path,
        "source_root": candidate.source_root,
        "tombstoned": tombstoned,
        "invalidated_at_ms": (
            min(item["invalidated_at_ms"] for item in matching_tombstones)
            if tombstoned
            else None
        ),
        "matching_tombstones": matching_tombstones,
    }


def _snapshot_body(
    *,
    index_version: int,
    previous_index_version: int | None,
    now_ms: int,
    input_hash: str,
    sources: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    tombstones: list[dict[str, Any]],
    merge_decisions: list[dict[str, Any]],
    migration_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest_version = max((source["manifest_version"] for source in sources), default=0)
    v2_sources = [source for source in sources if source["source_version"] == 2]
    floor_values = [
        source["generation_min"]
        for source in v2_sources
        if source["generation_min"] is not None
    ]
    ceiling_values = [
        source["generation_max"]
        for source in v2_sources
        if source["generation_max"] is not None
    ]
    tombstone_count = sum(1 for entry in entries if entry["tombstoned"])
    body = {
        "format": "cacheman-index-v1",
        "index_version": index_version,
        "previous_index_version": previous_index_version,
        "created_at_ms": now_ms,
        "logical_clock_ms": now_ms,
        "input_hash": input_hash,
        "manifest_version": manifest_version,
        "cache_generation_floor": min(floor_values) if floor_values else 0,
        "cache_generation_ceiling": max(ceiling_values) if ceiling_values else 0,
        "sources": sources,
        "entries": entries,
        "tombstones": tombstones,
        "merge_decisions": merge_decisions,
        "migration_decisions": migration_decisions,
        "summary": {
            "artifact_count": len(entries),
            "source_count": len(sources),
            "merge_conflict_count": len(merge_decisions),
            "migration_decision_count": len(migration_decisions),
            "tombstone_count": len(tombstones),
            "invalid_artifact_count": tombstone_count,
        },
    }
    return body


def build_snapshot(
    *,
    candidates: Iterable[Candidate],
    tombstones: Iterable[Tombstone],
    sources: Iterable[SourceInfo],
    now_ms: int,
    index_version: int,
    previous_index_version: int | None,
    input_hash: str,
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.task_key, candidate.artifact)].append(candidate)

    normalized_tombstones = _dedupe_tombstones(tombstones)
    tombstone_index: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for tombstone in normalized_tombstones:
        tombstone_index[
            (tombstone["task_key"], tombstone["artifact"], tombstone["digest"])
        ].append(tombstone)

    entries: list[dict[str, Any]] = []
    merge_decisions: list[dict[str, Any]] = []
    selected_candidates: dict[tuple[str, str], Candidate] = {}
    for key in sorted(grouped):
        group = sorted(grouped[key], key=lambda candidate: candidate.rank(), reverse=True)
        winner = group[0]
        selected_candidates[key] = winner
        if len(group) > 1:
            loser = group[1]
            candidate_records = [
                candidate.to_report_dict()
                for candidate in group
            ]
            identity = {
                "task_key": key[0],
                "artifact": key[1],
                "candidates": [
                    {
                        "source_id": candidate.source_id,
                        "rank_key": list(candidate.rank()),
                    }
                    for candidate in group
                ],
            }
            merge_decisions.append(
                {
                    "decision_id": "merge:" + sha256_text(canonical_json(identity))[:24],
                    "task_key": key[0],
                    "artifact": key[1],
                    "rule": "logical_clock,generation,write_time,source_priority,manifest_version,source_id",
                    "decisive_field": _decisive_field(winner, loser),
                    "winner_source_id": winner.source_id,
                    "winner_source_version": winner.source_version,
                    "loser_source_ids": sorted(
                        {candidate.source_id for candidate in group[1:]}
                    ),
                    "candidates": candidate_records,
                }
            )
        matching: list[dict[str, Any]] = []
        for digest in winner.digests:
            matching.extend(tombstone_index.get((winner.task_key, winner.artifact, digest), ()))
        matching.sort(
            key=lambda item: (
                item["invalidated_at_ms"],
                item["source_id"],
                item["digest"],
            )
        )
        entries.append(_entry_from_candidate(winner, matching_tombstones=matching))

    migration_decisions: list[dict[str, Any]] = []
    for key in sorted(grouped):
        for candidate in sorted(grouped[key], key=lambda item: (item.source_id, item.rank())):
            migration_decisions.append(
                _migration_decision(
                    candidate,
                    selected=candidate == selected_candidates[key],
                )
            )
    migration_decisions.sort(key=lambda item: item["decision_id"])
    merge_decisions.sort(key=lambda item: (item["task_key"], item["artifact"]))
    source_records = _dedupe_sources(sources)
    body = _snapshot_body(
        index_version=index_version,
        previous_index_version=previous_index_version,
        now_ms=now_ms,
        input_hash=input_hash,
        sources=source_records,
        entries=entries,
        tombstones=normalized_tombstones,
        merge_decisions=merge_decisions,
        migration_decisions=migration_decisions,
    )
    body["data_digest"] = "sha256:" + sha256_text(canonical_json(body))
    return body


def recompute_data_digest(snapshot: dict[str, Any]) -> str:
    body = dict(snapshot)
    body.pop("data_digest", None)
    return "sha256:" + sha256_text(canonical_json(body))
