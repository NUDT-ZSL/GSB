from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .errors import fail
from .models import LogicalTable, MergedRule, RoutingRule, RuleCandidate
from .util import (
    SHA256_RE,
    digest,
    ensure_relative_child,
    read_json,
    require_dict,
    require_int,
    require_list,
    require_str,
)


def _diagnostic_path(path: Path) -> str:
    parts = path.resolve().parts
    for index, part in enumerate(parts):
        if part == "rules":
            return Path(*parts[index:]).as_posix()
    return path.name


def _routing_from_spec(
    spec: dict[str, Any],
    *,
    table: LogicalTable,
    label: str,
) -> RoutingRule:
    rule_type = str(
        spec.get("type", spec.get("rule_type", spec.get("shard_rule_type", table.rule_type)))
    ).lower()
    if rule_type not in {"modulo", "range"}:
        fail(
            "E_RULE_TYPE_INVALID",
            "routing rule type must be modulo or range",
            label=label,
            actual=rule_type,
        )
    if rule_type != table.rule_type:
        fail(
            "E_RULE_TYPE_INVALID",
            "routing rule type must match the logical table description",
            table=table.name,
            expected=table.rule_type,
            actual=rule_type,
        )
    if rule_type == "modulo":
        raw_count = spec.get(
            "shard_count",
            spec.get("modulo_shards", spec.get("modulus", spec.get("modulo"))),
        )
        count = require_int(raw_count, label=f"{label}.modulo_shards", minimum=1)
        if count <= 0:
            fail("E_MODULUS_INVALID", "modulo shard count must be positive", table=table.name)
        if count != table.shard_count:
            fail(
                "E_MODULUS_INVALID",
                "modulo shard count must equal the table shard count",
                table=table.name,
                expected=table.shard_count,
                actual=count,
            )
        return RoutingRule(rule_type="modulo", shard_count=count)

    raw_boundaries = spec.get(
        "boundaries",
        spec.get("range_boundaries", table.range_boundaries),
    )
    if raw_boundaries is None:
        fail(
            "E_BOUNDARIES_INVALID",
            "range routing requires a boundary array",
            table=table.name,
        )
    boundaries = require_list(raw_boundaries, label=f"{label}.boundaries")
    if len(boundaries) != table.shard_count + 1:
        fail(
            "E_BOUNDARIES_INVALID",
            "range boundaries must contain shard_count + 1 values",
            table=table.name,
            expected=table.shard_count + 1,
            actual=len(boundaries),
        )
    parsed = tuple(
        require_int(value, label=f"{label}.boundaries[{index}]")
        for index, value in enumerate(boundaries)
    )
    if any(left >= right for left, right in zip(parsed, parsed[1:])):
        fail(
            "E_BOUNDARIES_INVALID",
            "range boundaries must be strictly increasing",
            table=table.name,
        )
    if table.key_min is not None and parsed[0] < table.key_min:
        fail(
            "E_BOUNDARIES_INVALID",
            "range boundaries exceed the table shard-key domain",
            table=table.name,
            minimum=table.key_min,
            actual=parsed[0],
        )
    if table.key_max is not None and parsed[-1] > table.key_max:
        fail(
            "E_BOUNDARIES_INVALID",
            "range boundaries exceed the table shard-key domain",
            table=table.name,
            maximum=table.key_max,
            actual=parsed[-1],
        )
    return RoutingRule(rule_type="range", shard_count=table.shard_count, boundaries=parsed)


def _parse_routing(
    obj: dict[str, Any],
    *,
    tables: dict[str, LogicalTable],
    defaults: dict[str, Any],
    label: str,
) -> dict[str, RoutingRule]:
    explicit = obj.get("routing", obj.get("routes"))
    if explicit is not None:
        explicit = require_dict(explicit, label=f"{label}.routing")
        unknown = sorted(set(explicit) - set(tables))
        if unknown:
            fail(
                "E_RULE_ROUTING_MISSING",
                "routing references unknown logical tables",
                label=label,
                tables=unknown,
            )
    else:
        explicit = {}
    result: dict[str, RoutingRule] = {}
    for table_name, table in tables.items():
        if table_name in explicit:
            raw_spec = explicit[table_name]
            if isinstance(raw_spec, int) and not isinstance(raw_spec, bool):
                raw_spec = {"shard_count": raw_spec}
            if isinstance(raw_spec, list):
                raw_spec = {"boundaries": raw_spec}
            spec = require_dict(raw_spec, label=f"{label}.routing.{table_name}")
        else:
            spec = dict(defaults)
            if table.rule_type == "modulo" and "modulo_shards" not in spec:
                spec["modulo_shards"] = table.shard_count
            if table.rule_type == "range" and "range_boundaries" not in spec:
                if table.range_boundaries is not None:
                    spec["range_boundaries"] = list(table.range_boundaries)
        result[table_name] = _routing_from_spec(
            spec,
            table=table,
            label=f"{label}.routing.{table_name}",
        )
    return result


def load_v1_rules(
    path: Path,
    *,
    tables: dict[str, LogicalTable],
    base_clock_override_ms: int | None = None,
) -> list[RuleCandidate]:
    root = read_json(path, label="v1 rule snapshot")
    root_obj = require_dict(root, label="v1 rule snapshot")
    base_clock = root_obj.get(
        "logical_clock_base_ms",
        root_obj.get("base_clock_ms", root_obj.get("clock_base_ms", 0)),
    )
    base_clock = require_int(base_clock, label="v1.logical_clock_base_ms", minimum=0)
    if base_clock_override_ms is not None:
        base_clock = require_int(
            base_clock_override_ms,
            label="v1 base clock override",
            minimum=0,
        )
    scale_ms = require_int(
        root_obj.get("clock_scale_ms", 1000),
        label="v1.clock_scale_ms",
        minimum=1,
    )
    rounding = str(root_obj.get("rounding", "floor")).lower()
    if rounding not in {"floor", "truncate"}:
        fail(
            "E_V1_EFFECT_INVALID",
            "v1 rounding must be floor or truncate for non-negative seconds",
            actual=rounding,
        )
    default_priority = require_int(
        root_obj.get("source_priority", 10),
        label="v1.source_priority",
        minimum=0,
    )
    top_defaults = {
        key: root_obj[key]
        for key in ("modulo_shards", "range_boundaries", "type")
        if key in root_obj
    }
    raw_rules = root_obj.get("rules", root_obj.get("snapshots", root_obj.get("versions")))
    if raw_rules is None:
        if "version" not in root_obj:
            fail("E_VALIDATION", "v1 snapshot has neither rules nor a version")
        raw_rules = [root_obj]
    raw_rules = require_list(raw_rules, label="v1.rules")
    if not raw_rules:
        fail("E_VALIDATION", "v1 snapshot contains no rules")

    candidates: list[RuleCandidate] = []
    for index, raw in enumerate(raw_rules):
        obj = require_dict(raw, label=f"v1.rules[{index}]")
        version = require_str(
            obj.get("version", root_obj.get("version")),
            label=f"v1.rules[{index}].version",
        )
        relative_raw = obj.get(
            "relative_effective_seconds",
            obj.get(
                "relative_effect_seconds",
                obj.get("effective_after_seconds", obj.get("relative_seconds")),
            ),
        )
        if relative_raw is None:
            fail(
                "E_V1_EFFECT_INVALID",
                "v1 rule requires a non-negative relative_effective_seconds",
                version=version,
            )
        relative_seconds = require_int(
            relative_raw,
            label=f"v1.rules[{index}].relative_effective_seconds",
            minimum=0,
        )
        if relative_seconds < 0:
            fail("E_V1_EFFECT_INVALID", "v1 relative effect seconds must be non-negative")
        effective_clock = base_clock + relative_seconds * scale_ms
        priority = require_int(
            obj.get("source_priority", default_priority),
            label=f"v1.rules[{index}].source_priority",
            minimum=0,
        )
        generation = require_int(
            obj.get("generation", index + 1),
            label=f"v1.rules[{index}].generation",
            minimum=0,
        )
        defaults = dict(top_defaults)
        for key in ("modulo_shards", "range_boundaries", "type"):
            if key in obj:
                defaults[key] = obj[key]
        routing = _parse_routing(
            obj,
            tables=tables,
            defaults=defaults,
            label=f"v1.rules[{index}]",
        )
        candidates.append(
            RuleCandidate(
                version=version,
                source="v1",
                source_priority=priority,
                effective_clock_ms=effective_clock,
                generation=generation,
                digest=digest(
                    {
                        "version": version,
                        "routing": {
                            table: rule.to_dict()
                            for table, rule in sorted(routing.items())
                        },
                    }
                ),
                rollback_target=(
                    str(obj["rollback_target"]) if obj.get("rollback_target") else None
                ),
                routing=routing,
                source_file=_diagnostic_path(path),
                conversion={
                    "source": "v1",
                    "base_clock_ms": base_clock,
                    "relative_effective_seconds": relative_seconds,
                    "clock_scale_ms": scale_ms,
                    "rounding": rounding,
                    "effective_clock_ms": effective_clock,
                },
                raw=obj,
            )
        )
    return candidates


def _find_v2_meta_database(directory: Path) -> Path:
    for name in ("meta.sqlite", "meta.db", "rules.sqlite"):
        candidate = directory / name
        if candidate.exists():
            return candidate
    fail(
        "E_INPUT_MISSING",
        "v2 metadata SQLite database was not found",
        directory=str(directory),
    )


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {str(row[1]) for row in rows}


def _pick_column(columns: set[str], *aliases: str) -> str | None:
    for alias in aliases:
        if alias in columns:
            return alias
    return None


def load_v2_rules(directory: Path, *, tables: dict[str, LogicalTable]) -> list[RuleCandidate]:
    directory = directory.resolve()
    if not directory.exists() or not directory.is_dir():
        fail(
            "E_INPUT_MISSING",
            "v2 rule directory does not exist",
            directory=str(directory),
        )
    meta_path = _find_v2_meta_database(directory)
    try:
        connection = sqlite3.connect(
            f"file:{meta_path.as_posix()}?mode=ro", uri=True, timeout=5
        )
    except sqlite3.Error as exc:
        fail(
            "E_SHARD_DB_UNOPENABLE",
            "v2 metadata SQLite database cannot be opened",
            path=str(meta_path),
            error=str(exc),
        )
    try:
        connection.row_factory = sqlite3.Row
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        table_name = "v2_rules" if "v2_rules" in names else (names[0] if names else "")
        if not table_name:
            fail(
                "E_V2_CLOCK_INVALID",
                "v2 metadata database contains no rule table",
                path=str(meta_path),
            )
        columns = _table_columns(connection, table_name)
        version_col = _pick_column(columns, "version", "rule_version")
        digest_col = _pick_column(columns, "content_digest", "digest", "shard_hash")
        clock_col = _pick_column(columns, "effective_clock_ms", "effective_clock", "clock_ms")
        generation_col = _pick_column(columns, "generation", "shard_generation")
        file_col = _pick_column(columns, "shard_file", "json_file", "file")
        priority_col = _pick_column(columns, "source_priority", "priority")
        rollback_col = _pick_column(columns, "rollback_target", "rollback_to")
        previous_col = _pick_column(columns, "previous_digest", "prev_digest")
        routing_col = _pick_column(columns, "routing_json")
        required = {
            "version": version_col,
            "content_digest": digest_col,
            "effective_clock_ms": clock_col,
            "generation": generation_col,
            "shard_file": file_col,
        }
        missing = sorted(key for key, value in required.items() if value is None)
        if missing:
            fail(
                "E_V2_CLOCK_INVALID",
                "v2 metadata table is missing required columns",
                table=table_name,
                columns=missing,
            )
        rows = connection.execute(f'SELECT * FROM "{table_name}"').fetchall()
    except sqlite3.Error as exc:
        fail(
            "E_V2_CLOCK_INVALID",
            "cannot read v2 metadata",
            path=str(meta_path),
            error=str(exc),
        )
    finally:
        connection.close()

    if not rows:
        fail("E_RULE_NOT_FOUND", "v2 metadata contains no rule versions")

    parsed_rows: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(row)
        version = require_str(
            raw[version_col], label="v2.version"
        )
        content_digest = require_str(
            raw[digest_col], label=f"v2.{version}.content_digest"
        ).lower()
        if not SHA256_RE.fullmatch(content_digest):
            fail(
                "E_V2_DIGEST_MISMATCH",
                "v2 content digest must be a lowercase SHA-256 hex string",
                version=version,
                actual=content_digest,
            )
        effective_clock = require_int(
            raw[clock_col],
            label=f"v2.{version}.effective_clock_ms",
        )
        generation = require_int(
            raw[generation_col],
            label=f"v2.{version}.generation",
            minimum=1,
        )
        priority = (
            require_int(
                raw[priority_col],
                label=f"v2.{version}.source_priority",
                minimum=0,
            )
            if priority_col
            else 20
        )
        rollback_target = (
            require_str(raw[rollback_col], label=f"v2.{version}.rollback_target")
            if rollback_col and raw[rollback_col] is not None
            else None
        )
        previous_digest = (
            require_str(raw[previous_col], label=f"v2.{version}.previous_digest")
            if previous_col and raw[previous_col] is not None
            else None
        )
        if previous_digest is not None:
            previous_digest = previous_digest.lower()
            if not SHA256_RE.fullmatch(previous_digest):
                fail(
                    "E_V2_DIGEST_MISMATCH",
                    "v2 previous digest must be a lowercase SHA-256 hex string",
                    version=version,
                )
        shard_file = require_str(
            raw[file_col], label=f"v2.{version}.shard_file"
        )
        shard_path = ensure_relative_child(
            directory,
            shard_file,
            label=f"v2.{version}.shard_file",
        )
        shard_doc = read_json(shard_path, label=f"v2 shard {version}")
        shard_obj = require_dict(shard_doc, label=f"v2 shard {version}")
        actual_digest = digest(shard_obj)
        if actual_digest != content_digest:
            fail(
                "E_V2_DIGEST_MISMATCH",
                "v2 shard content digest does not match metadata",
                version=version,
                expected=content_digest,
                actual=actual_digest,
                shard_file=shard_file,
            )
        if shard_obj.get("version") != version:
            fail(
                "E_VERSION_CONFLICT",
                "v2 shard file version does not match metadata",
                expected=version,
                actual=shard_obj.get("version"),
            )
        if shard_obj.get("effective_clock_ms") not in (None, effective_clock):
            fail(
                "E_V2_CLOCK_INVALID",
                "v2 shard file clock does not match metadata",
                version=version,
            )
        if shard_obj.get("generation") not in (None, generation):
            fail(
                "E_V2_GENERATION_NOT_MONOTONIC",
                "v2 shard file generation does not match metadata",
                version=version,
            )
        routing_doc = shard_obj.get("routing")
        if routing_doc is None and routing_col and raw[routing_col]:
            try:
                routing_doc = json.loads(raw[routing_col])
            except json.JSONDecodeError as exc:
                fail(
                    "E_V2_CLOCK_INVALID",
                    "v2 routing_json is invalid JSON",
                    version=version,
                    error=str(exc),
                )
        explicit = require_dict(routing_doc, label=f"v2.{version}.routing")
        routing = _parse_routing(
            {"routing": explicit},
            tables=tables,
            defaults={},
            label=f"v2.{version}",
        )
        parsed_rows.append(
            {
                "version": version,
                "digest": content_digest,
                "effective_clock_ms": effective_clock,
                "generation": generation,
                "priority": priority,
                "rollback_target": rollback_target,
                "previous_digest": previous_digest,
                "source_file": _diagnostic_path(shard_path),
                "routing": routing,
                "raw": shard_obj,
            }
        )

    ordered = sorted(
        parsed_rows,
        key=lambda item: (
            item["effective_clock_ms"],
            item["generation"],
            item["version"],
        ),
    )
    previous_generation: int | None = None
    previous_digest: str | None = None
    chain_declared = previous_col is not None and any(
        item["previous_digest"] is not None for item in ordered
    )
    for index, item in enumerate(ordered):
        if previous_generation is not None and item["generation"] <= previous_generation:
            fail(
                "E_V2_GENERATION_NOT_MONOTONIC",
                "v2 shard generations must be strictly increasing by logical clock",
                version=item["version"],
                previous_generation=previous_generation,
                actual=item["generation"],
            )
        if not chain_declared:
            pass
        elif index == 0:
            if item["previous_digest"] is not None:
                fail(
                    "E_V2_DIGEST_MISMATCH",
                    "first v2 shard must not declare a previous digest",
                    version=item["version"],
                )
        elif previous_col is not None and item["previous_digest"] != previous_digest:
            fail(
                "E_V2_DIGEST_MISMATCH",
                "v2 shard digest chain is broken",
                version=item["version"],
                expected_previous=previous_digest,
                actual_previous=item["previous_digest"],
            )
        previous_generation = item["generation"]
        previous_digest = item["digest"]

    return [
        RuleCandidate(
            version=item["version"],
            source="v2",
            source_priority=item["priority"],
            effective_clock_ms=item["effective_clock_ms"],
            generation=item["generation"],
            digest=item["digest"],
            rollback_target=item["rollback_target"],
            routing=item["routing"],
            source_file=item["source_file"],
            conversion={
                "source": "v2",
                "absolute_clock_ms": item["effective_clock_ms"],
                "generation": item["generation"],
                "digest_chain_verified": True,
            },
            raw=item["raw"],
        )
        for item in ordered
    ]


def _to_merged_rule(candidate: RuleCandidate, decision: dict[str, Any]) -> MergedRule:
    return MergedRule(
        version=candidate.version,
        effective_clock_ms=candidate.effective_clock_ms,
        generation=candidate.generation,
        digest=candidate.digest,
        source=candidate.source,
        source_priority=candidate.source_priority,
        rollback_target=candidate.rollback_target,
        routing=candidate.routing,
        decision=decision,
    )


def merge_candidates(
    candidates: Iterable[RuleCandidate],
    *,
    tables: dict[str, LogicalTable],
) -> list[MergedRule]:
    by_version: dict[str, list[RuleCandidate]] = {}
    for candidate in candidates:
        by_version.setdefault(candidate.version, []).append(candidate)
    result: list[MergedRule] = []
    for version in sorted(by_version):
        versions = by_version[version]
        winner = max(versions, key=lambda item: item.sort_key())
        evidence = [
            {
                "source": item.source,
                "effective_clock_ms": item.effective_clock_ms,
                "generation": item.generation,
                "source_priority": item.source_priority,
                "digest": item.digest,
                "source_file": item.source_file,
                "conversion": item.conversion,
            }
            for item in sorted(versions, key=lambda item: item.sort_key())
        ]
        decision = {
            "basis": [
                "effective_clock_ms",
                "generation",
                "source_priority",
            ],
            "winner_source": winner.source,
            "winner_effective_clock_ms": winner.effective_clock_ms,
            "winner_generation": winner.generation,
            "winner_source_priority": winner.source_priority,
            "reason": (
                "same version selected by latest logical clock, then shard "
                "generation, then source priority"
            ),
            "candidates": evidence,
        }
        result.append(_to_merged_rule(winner, decision))
    result.sort(key=lambda item: item.sort_key())
    return result


def build_rule_models(
    v1_candidates: list[RuleCandidate],
    v2_candidates: list[RuleCandidate],
    *,
    tables: dict[str, LogicalTable],
) -> tuple[list[MergedRule], list[MergedRule], list[MergedRule]]:
    merged = merge_candidates([*v1_candidates, *v2_candidates], tables=tables)
    v1_models = merge_candidates(v1_candidates, tables=tables)
    v2_models = merge_candidates(v2_candidates, tables=tables)
    if not merged:
        return [], [], []
    for rule in merged:
        if set(rule.routing) != set(tables):
            missing = sorted(set(tables) - set(rule.routing))
            fail(
                "E_RULE_ROUTING_MISSING",
                "every rule version must route every logical table",
                version=rule.version,
                missing_tables=missing,
            )
    return merged, v1_models, v2_models


def rule_for_clock(rules: Iterable[MergedRule], clock_ms: int) -> MergedRule:
    eligible = [rule for rule in rules if rule.effective_clock_ms <= clock_ms]
    if not eligible:
        fail(
            "E_RULE_NOT_FOUND",
            "no rule version is effective at the requested logical clock",
            clock_ms=clock_ms,
            available=[
                {
                    "version": rule.version,
                    "effective_clock_ms": rule.effective_clock_ms,
                }
                for rule in sorted(rules, key=lambda item: item.sort_key())
            ],
        )
    return max(eligible, key=lambda item: item.sort_key())


def select_snapshot_rule(
    rules: list[MergedRule],
    *,
    version: str | None,
) -> MergedRule:
    if version is not None:
        matches = [rule for rule in rules if rule.version == version]
        if not matches:
            fail(
                "E_RULE_NOT_FOUND",
                "requested rule version is not present in the snapshot",
                version=version,
            )
        return max(matches, key=lambda item: item.sort_key())
    if not rules:
        fail("E_RULE_NOT_FOUND", "snapshot contains no rules")
    return max(rules, key=lambda item: item.sort_key())


def merged_rule_from_dict(value: dict[str, Any]) -> MergedRule:
    routing: dict[str, RoutingRule] = {}
    for table_name, raw in value.get("routing", {}).items():
        rule_type = str(raw.get("type", ""))
        if rule_type not in {"modulo", "range"}:
            fail(
                "E_STATE_CORRUPT",
                "persisted routing rule has an invalid type",
                table=table_name,
            )
        routing[table_name] = RoutingRule(
            rule_type=rule_type,
            shard_count=require_int(
                raw.get("shard_count"), label=f"rule.{table_name}.shard_count", minimum=1
            ),
            boundaries=(
                tuple(
                    require_int(
                        item,
                        label=f"rule.{table_name}.boundaries",
                    )
                    for item in raw["boundaries"]
                )
                if raw.get("boundaries") is not None
                else None
            ),
        )
    return MergedRule(
        version=require_str(value.get("version"), label="rule.version"),
        effective_clock_ms=require_int(
            value.get("effective_clock_ms"), label="rule.effective_clock_ms"
        ),
        generation=require_int(value.get("generation"), label="rule.generation"),
        digest=require_str(value.get("digest"), label="rule.digest"),
        source=require_str(value.get("source"), label="rule.source"),
        source_priority=require_int(
            value.get("source_priority"), label="rule.source_priority", minimum=0
        ),
        rollback_target=value.get("rollback_target"),
        routing=routing,
        decision=value.get("decision", {}),
    )
