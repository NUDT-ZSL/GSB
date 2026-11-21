"""Cross-version view merging with an auditable, deterministic decision log."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .definitions import ParsedV1, ParsedV2
from .errors import fail
from .models import UnifiedIndex, ViewSpec, build_index_payload
from .util import canonical_json, require_dict


def _merged_base_schema(
    v1: ParsedV1,
    v2: ParsedV2,
) -> dict[str, dict[str, Any]]:
    merged = {
        table: {"columns": list(spec["columns"]), **({"types": dict(spec["types"])} if "types" in spec else {})}
        for table, spec in v1.base_schema.items()
    }
    for table, spec in v2.base_schema.items():
        if table in merged:
            if sorted(merged[table]["columns"]) != sorted(spec["columns"]):
                raise fail(
                    "E_DEFINITION_SCHEMA_CONFLICT",
                    "the two definition generations disagree on base table columns",
                    table=table,
                    v1_columns=merged[table]["columns"],
                    v2_columns=spec["columns"],
                )
            if "types" in merged[table] and "types" in spec:
                for column, data_type in spec["types"].items():
                    prior = merged[table]["types"].get(column)
                    if prior is not None and prior != data_type:
                        raise fail(
                            "E_DEFINITION_SCHEMA_CONFLICT",
                            "the two definition generations disagree on a column type",
                            table=table,
                            column=column,
                            v1_type=prior,
                            v2_type=data_type,
                        )
                merged[table]["types"] = {
                    **merged[table]["types"],
                    **spec["types"],
                }
            elif "types" in spec:
                merged[table]["types"] = dict(spec["types"])
        else:
            merged[table] = {
                "columns": list(spec["columns"]),
                **({"types": dict(spec["types"])} if "types" in spec else {}),
            }
    return {name: merged[name] for name in sorted(merged)}


def _candidate(
    *,
    spec: ViewSpec,
    source_priority: int,
) -> dict[str, Any]:
    return {
        "absolute_refresh_ms": spec.absolute_refresh_ms,
        "generation": spec.generation,
        "logical_clock_ms": spec.logical_clock_ms,
        "source_format": spec.source_format,
        "source_priority": source_priority,
        "source_ref": spec.source_ref,
        "status": spec.status,
    }


def _decision_key(candidate: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(candidate["logical_clock_ms"]),
        int(candidate["generation"]),
        int(candidate["source_priority"]),
    )


def _reason(winner: dict[str, Any], losers: list[dict[str, Any]]) -> str:
    if not losers:
        return "single_definition"
    max_clock = max(item["logical_clock_ms"] for item in [winner, *losers])
    clock_competitors = [
        item for item in [winner, *losers] if item["logical_clock_ms"] == max_clock
    ]
    if len(clock_competitors) == 1:
        return "higher_logical_clock"
    max_generation = max(item["generation"] for item in clock_competitors)
    generation_competitors = [
        item for item in clock_competitors if item["generation"] == max_generation
    ]
    if len(generation_competitors) == 1:
        return "higher_view_generation"
    return "same_clock_v2_format_priority"


def merge_definitions(v1: ParsedV1, v2: ParsedV2) -> UnifiedIndex:
    base_schema = _merged_base_schema(v1, v2)
    candidates: dict[str, list[tuple[ViewSpec, int]]] = {}
    for spec in v1.views.values():
        migrated_generation = max(1, v2.view_generation_floor)
        assert spec.migration is not None
        migration = {
            **spec.migration,
            "generation": {
                "migrated": migrated_generation,
                "original": None,
            },
        }
        migrated_spec = replace(
            spec,
            generation=migrated_generation,
            migration=migration,
        )
        candidates.setdefault(migrated_spec.name, []).append((migrated_spec, 1))
    for spec in v2.views.values():
        candidates.setdefault(spec.name, []).append((spec, 2))

    winners: dict[str, ViewSpec] = {}
    decisions: list[dict[str, Any]] = []
    migrations: list[dict[str, Any]] = []
    for name in sorted(candidates):
        options = sorted(
            candidates[name],
            key=lambda pair: (
                pair[0].logical_clock_ms,
                pair[0].generation,
                pair[1],
            ),
            reverse=True,
        )
        winning_spec, winning_priority = options[0]
        winners[name] = winning_spec
        ordered_candidates = [
            _candidate(spec=spec, source_priority=priority)
            for spec, priority in options
        ]
        winner_candidate = ordered_candidates[0]
        loser_candidates = ordered_candidates[1:]
        decisions.append(
            {
                "alternatives": sorted(
                    loser_candidates,
                    key=lambda item: (
                        item["source_format"],
                        item["logical_clock_ms"],
                        item["generation"],
                    ),
                ),
                "reason": _reason(winner_candidate, loser_candidates),
                "selected": winner_candidate,
                "view_name": name,
            }
        )
        for spec, priority in candidates[name]:
            if spec.source_format != "v1":
                continue
            assert spec.migration is not None
            migration = dict(spec.migration)
            migration["used_in_index"] = (
                winning_priority == priority and winning_spec is spec
            )
            migrations.append(migration)

    index = build_index_payload(
        views=winners,
        base_schema=base_schema,
        decisions=decisions,
        migrations=migrations,
        source_digests={
            "v1": v1.file_sha256,
            "v2": v2.manifest_sha256,
        },
        logical_clock_ms=max(v1.logical_clock_ms, v2.logical_clock_ms),
    )
    return index


def parse_index_payload(value: dict[str, Any]) -> UnifiedIndex:
    """Parse and validate a serialized unified index, requiring stable digests."""

    value = require_dict(value, "unified index", code="E_INDEX_INVALID")
    required = {
        "base_schema",
        "decisions",
        "definition_digest",
        "index_digest",
        "logical_clock_ms",
        "migrations",
        "source_digests",
        "views",
    }
    missing = sorted(required - set(value))
    if missing:
        raise fail("E_INDEX_INVALID", "unified index is missing fields", missing=missing)
    payload_without_index = dict(value)
    expected_index = payload_without_index.pop("index_digest")
    definition_without_digest = dict(payload_without_index)
    expected_definition = definition_without_digest.pop("definition_digest")
    from .util import digest_json

    actual_definition = digest_json(definition_without_digest)
    if actual_definition != expected_definition:
        raise fail(
            "E_INDEX_INVALID",
            "unified index definition digest does not match its content",
            expected=expected_definition,
            actual=actual_definition,
        )
    actual_index = digest_json(payload_without_index)
    if actual_index != expected_index:
        raise fail(
            "E_INDEX_INVALID",
            "unified index digest does not match its content",
            expected=expected_index,
            actual=actual_index,
        )
    index = UnifiedIndex(payload=value)
    index.views
    index.topo_order()
    return index


def index_is_identical(first: UnifiedIndex, second: UnifiedIndex) -> bool:
    return canonical_json(first.payload) == canonical_json(second.payload)

