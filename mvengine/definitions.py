"""Parsers and validators for v1 JSON and v2 shard+SQLite definitions."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import EngineError, fail
from .expressions import FilterExpression, validate_columns
from .models import AggregateRule, SUPPORTED_AGGREGATES, ViewSpec
from .util import (
    canonical_json,
    digest_file,
    digest_json,
    read_json,
    require_dict,
    require_int,
    require_list,
    require_str,
    safe_child,
    seconds_floor_ms,
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ParsedV1:
    path: Path
    file_sha256: str
    logical_clock_ms: int
    base_schema: dict[str, dict[str, Any]]
    views: dict[str, ViewSpec]
    raw: dict[str, Any]


@dataclass(frozen=True)
class V2Revision:
    sequence: int
    view_name: str
    generation: int
    logical_clock_ms: int
    absolute_refresh_ms: int
    status: str
    base_tables: tuple[str, ...]
    aggregate_rules: tuple[AggregateRule, ...]
    upstream_views: tuple[str, ...]
    shard_file: str | None
    spec_digest: str
    tombstone_reason: str | None
    tombstoned_at_ms: int | None


@dataclass(frozen=True)
class ParsedV2:
    path: Path
    manifest_sha256: str
    manifest_version: int
    logical_clock_ms: int
    view_generation_floor: int
    base_schema: dict[str, dict[str, Any]]
    views: dict[str, ViewSpec]
    revisions: dict[str, tuple[V2Revision, ...]]
    shard_hashes: dict[str, str]
    raw_manifest: dict[str, Any]


def _read_v1_view(
    raw_view: dict[str, Any],
    *,
    base_schema: dict[str, dict[str, Any]],
    logical_clock_ms: int,
) -> ViewSpec:
    name = require_str(raw_view.get("name"), "views[].name", code="E_V1_SCHEMA_INVALID")
    base_table = require_str(
        raw_view.get("base_table"),
        f"view {name}.base_table",
        code="E_V1_UNKNOWN_BASE_TABLE",
    )
    if base_table not in base_schema:
        raise fail(
            "E_V1_UNKNOWN_BASE_TABLE",
            "v1 view references an unknown base table",
            view=name,
            base_table=base_table,
            known_tables=sorted(base_schema),
        )
    columns = set(base_schema[base_table]["columns"])
    filter_text = require_str(
        raw_view.get("filter", "true"),
        f"view {name}.filter",
        allow_empty=False,
        code="E_V1_BAD_FILTER",
    )
    try:
        parsed_filter = FilterExpression(filter_text, error_code="E_V1_BAD_FILTER")
    except EngineError:
        raise
    validate_columns(parsed_filter.columns, columns, field=f"view {name}.filter")

    aggregate = require_dict(
        raw_view.get("aggregate"),
        f"view {name}.aggregate",
        code="E_V1_BAD_AGGREGATE",
    )
    column = require_str(
        aggregate.get("column"),
        f"view {name}.aggregate.column",
        code="E_V1_BAD_AGGREGATE",
    )
    functions_raw = require_list(
        aggregate.get("functions"),
        f"view {name}.aggregate.functions",
        code="E_V1_BAD_AGGREGATE",
    )
    functions = [
        require_str(
            function,
            f"view {name}.aggregate.functions[]",
            code="E_V1_BAD_AGGREGATE",
        )
        for function in functions_raw
    ]
    if sorted(functions) != sorted(SUPPORTED_AGGREGATES):
        raise fail(
            "E_V1_BAD_AGGREGATE",
            "v1 aggregate functions must contain count, sum, and sum_sq exactly once",
            view=name,
            functions=functions,
        )
    if column not in columns:
        raise fail(
            "E_V1_BAD_AGGREGATE",
            "v1 aggregate expression references an unknown column",
            view=name,
            column=column,
            columns=sorted(columns),
        )
    rules = (
        AggregateRule(function="count", column="*"),
        AggregateRule(function="sum", column=column),
        AggregateRule(function="sum_sq", column=column),
    )
    refresh_seconds = require_int(
        raw_view.get("refresh_seconds"),
        f"view {name}.refresh_seconds",
        minimum=0,
        code="E_V1_BAD_REFRESH",
    )
    anchor_ms = seconds_floor_ms(logical_clock_ms)
    absolute_refresh_ms = anchor_ms + refresh_seconds * 1000
    migration = {
        "aggregate": {
            "original": {"column": column, "functions": functions},
            "migrated_rules": [rule.as_dict() for rule in rules],
        },
        "format": {"from": "v1", "to": "v2"},
        "generation": {"migrated": 1, "original": None},
        "time": {
            "absolute_refresh_ms": absolute_refresh_ms,
            "anchor_ms": anchor_ms,
            "formula": "floor(logical_clock_ms / 1000) * 1000 + refresh_seconds * 1000",
            "relative_seconds": refresh_seconds,
            "rounding": "floor",
            "scale_ms": 1000,
        },
        "upstream_views": {"migrated": [], "original": []},
        "view_name": name,
    }
    return ViewSpec(
        name=name,
        base_tables=(base_table,),
        filter_text=filter_text,
        aggregate_rules=rules,
        upstream_views=(),
        generation=1,
        logical_clock_ms=logical_clock_ms,
        absolute_refresh_ms=absolute_refresh_ms,
        refresh_origin={
            "anchor_ms": anchor_ms,
            "relative_seconds": refresh_seconds,
            "representation": "relative_seconds",
            "rounding": "floor",
            "scale_ms": 1000,
        },
        status="active",
        source_format="v1",
        source_ref="v1:views.json",
        migration=migration,
    )


def parse_v1(path: Path) -> ParsedV1:
    raw = read_json(path)
    raw = require_dict(raw, "v1 definition", code="E_V1_SCHEMA_INVALID")
    if raw.get("format") != "v1":
        raise fail("E_V1_SCHEMA_INVALID", "v1 definition must declare format=v1", path=str(path))
    logical_clock_ms = require_int(
        raw.get("logical_clock_ms"),
        "logical_clock_ms",
        minimum=0,
        code="E_V1_SCHEMA_INVALID",
    )
    base_schema = _parse_base_schema(raw.get("base_schema"), code="E_V1_SCHEMA_INVALID")
    raw_views = require_list(raw.get("views"), "views", code="E_V1_SCHEMA_INVALID")
    views: dict[str, ViewSpec] = {}
    for index, raw_view in enumerate(raw_views):
        view_obj = require_dict(raw_view, f"views[{index}]", code="E_V1_SCHEMA_INVALID")
        name = require_str(view_obj.get("name"), "views[].name", code="E_V1_SCHEMA_INVALID")
        if name in views:
            raise fail(
                "E_V1_DUPLICATE_VIEW",
                "v1 view names must be unique",
                view=name,
            )
        view = _read_v1_view(
            view_obj,
            base_schema=base_schema,
            logical_clock_ms=logical_clock_ms,
        )
        views[name] = view

    mapping = require_dict(
        raw.get("base_view_map"),
        "base_view_map",
        code="E_V1_MAPPING_MISMATCH",
    )
    expected: dict[str, set[str]] = {}
    for view in views.values():
        expected.setdefault(view.base_tables[0], set()).add(view.name)
    actual: dict[str, set[str]] = {}
    for table, names in mapping.items():
        table_name = require_str(table, "base_view_map key", code="E_V1_MAPPING_MISMATCH")
        name_list = require_list(names, f"base_view_map[{table_name}]", code="E_V1_MAPPING_MISMATCH")
        actual[table_name] = {
            require_str(item, f"base_view_map[{table_name}][]", code="E_V1_MAPPING_MISMATCH")
            for item in name_list
        }
        if len(actual[table_name]) != len(name_list):
            raise fail(
                "E_V1_MAPPING_MISMATCH",
                "base_view_map contains duplicate view names",
                table=table_name,
            )
    if actual != expected:
        raise fail(
            "E_V1_MAPPING_MISMATCH",
            "flat base-to-view mapping disagrees with view declarations",
            actual={key: sorted(value) for key, value in sorted(actual.items())},
            expected={key: sorted(value) for key, value in sorted(expected.items())},
        )
    return ParsedV1(
        path=path,
        file_sha256=digest_file(path),
        logical_clock_ms=logical_clock_ms,
        base_schema=base_schema,
        views=views,
        raw=raw,
    )


def _parse_base_schema(value: Any, *, code: str) -> dict[str, dict[str, Any]]:
    schema = require_dict(value, "base_schema", code=code)
    result: dict[str, dict[str, Any]] = {}
    for table, spec in schema.items():
        table_name = require_str(table, "base_schema table", code=code)
        table_spec = require_dict(spec, f"base_schema[{table_name}]", code=code)
        columns_raw = require_list(table_spec.get("columns"), f"base_schema[{table_name}].columns", code=code)
        columns = [
            require_str(column, f"base_schema[{table_name}].columns[]", code=code)
            for column in columns_raw
        ]
        if not columns or len(set(columns)) != len(columns):
            raise fail(
                code,
                "base table columns must be non-empty and unique",
                table=table_name,
            )
        normalized: dict[str, Any] = {"columns": columns}
        if "types" in table_spec:
            types = require_dict(table_spec["types"], f"base_schema[{table_name}].types", code=code)
            normalized["types"] = {
                require_str(key, "type column", code=code): require_str(val, "type", code=code)
                for key, val in types.items()
            }
        result[table_name] = normalized
    if not result:
        raise fail(code, "base_schema must declare at least one table")
    return result


def _validate_aggregate_and_filter(
    *,
    name: str,
    base_tables: tuple[str, ...],
    filter_text: str,
    rules: tuple[AggregateRule, ...],
    base_schema: dict[str, dict[str, Any]],
    code_prefix: str,
) -> None:
    for table in base_tables:
        if table not in base_schema:
            raise fail(
                f"{code_prefix}_UNKNOWN_BASE_TABLE",
                "view references an unknown base table",
                view=name,
                base_table=table,
            )
    try:
        parsed_filter = FilterExpression(filter_text, error_code=f"{code_prefix}_BAD_FILTER")
    except EngineError:
        raise
    for table in base_tables:
        validate_columns(
            parsed_filter.columns,
            set(base_schema[table]["columns"]),
            field=f"view {name}.filter",
            code=f"{code_prefix}_BAD_FILTER",
        )
    if sorted(rule.function for rule in rules) != sorted(SUPPORTED_AGGREGATES):
        raise fail(
            f"{code_prefix}_BAD_AGGREGATE",
            "aggregate rules must contain count, sum, and sum_sq exactly once",
            view=name,
        )
    for rule in rules:
        if rule.column == "*":
            if rule.function != "count":
                raise fail(
                    f"{code_prefix}_BAD_AGGREGATE",
                    "only count may aggregate the wildcard column",
                    view=name,
                    rule=rule.as_dict(),
                )
            continue
        for table in base_tables:
            if rule.column not in base_schema[table]["columns"]:
                raise fail(
                    f"{code_prefix}_BAD_AGGREGATE",
                    "aggregate rule references an unknown column",
                    view=name,
                    table=table,
                    column=rule.column,
                )


def _read_v2_shards(
    root: Path,
    manifest: dict[str, Any],
    base_schema: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, tuple[AggregateRule, ...]]]:
    shards = require_dict(manifest.get("shards"), "v2 manifest shards", code="E_V2_BAD_MANIFEST")
    shard_hashes: dict[str, str] = {}
    shard_views: dict[str, dict[str, Any]] = {}
    shard_rules: dict[str, tuple[AggregateRule, ...]] = {}
    for relative, expected_hash in shards.items():
        relative_path = require_str(relative, "shard path", code="E_V2_BAD_MANIFEST")
        expected = require_str(expected_hash, f"shard hash for {relative_path}", code="E_V2_BAD_MANIFEST")
        if not _HASH_RE.fullmatch(expected):
            raise fail(
                "E_V2_BAD_MANIFEST",
                "shard hash must be a lowercase SHA-256 hex digest",
                shard=relative_path,
            )
        shard_path = safe_child(root, relative_path)
        if not shard_path.is_file():
            raise fail("E_INPUT_NOT_FOUND", "v2 shard file does not exist", shard=relative_path)
        actual = digest_file(shard_path)
        if actual != expected:
            raise fail(
                "E_SHARD_HASH_MISMATCH",
                "v2 shard SHA-256 does not match the manifest",
                expected=expected,
                actual=actual,
                shard=relative_path,
            )
        shard_hashes[relative_path] = actual
        raw = read_json(shard_path, code="E_SHARD_INVALID")
        shard = require_dict(raw, "v2 shard", code="E_SHARD_INVALID")
        if shard.get("format") != "v2-shard":
            raise fail("E_SHARD_INVALID", "shard must declare format=v2-shard", shard=relative_path)
        shard_id = require_str(shard.get("shard_id"), "shard_id", code="E_SHARD_INVALID")
        views = require_list(shard.get("views"), "shard views", code="E_SHARD_INVALID")
        for index, raw_view in enumerate(views):
            view = require_dict(raw_view, f"{shard_id}.views[{index}]", code="E_SHARD_INVALID")
            name = require_str(view.get("name"), "shard view name", code="E_SHARD_INVALID")
            if name in shard_views:
                raise fail(
                    "E_SHARD_INVALID",
                    "view name appears in more than one shard",
                    view=name,
                    shards=[shard_views[name]["_shard_file"], relative_path],
                )
            base_tables_raw = require_list(view.get("base_tables"), f"view {name}.base_tables", code="E_SHARD_INVALID")
            base_tables = tuple(
                require_str(item, f"view {name}.base_tables[]", code="E_SHARD_INVALID")
                for item in base_tables_raw
            )
            filter_text = require_str(
                view.get("filter"),
                f"view {name}.filter",
                allow_empty=False,
                code="E_SHARD_INVALID",
            )
            rules_raw = require_list(
                view.get("aggregate_rules"),
                f"view {name}.aggregate_rules",
                code="E_SHARD_INVALID",
            )
            rules = tuple(
                AggregateRule.from_dict(
                    require_dict(rule, f"view {name}.aggregate_rules[]", code="E_SHARD_INVALID"),
                    code="E_SHARD_INVALID",
                )
                for rule in rules_raw
            )
            upstream_raw = require_list(
                view.get("upstream_views"),
                f"view {name}.upstream_views",
                code="E_SHARD_INVALID",
            )
            upstream = tuple(
                require_str(item, f"view {name}.upstream_views[]", code="E_SHARD_INVALID")
                for item in upstream_raw
            )
            _validate_aggregate_and_filter(
                name=name,
                base_tables=base_tables,
                filter_text=filter_text,
                rules=rules,
                base_schema=base_schema,
                code_prefix="E_V2",
            )
            normalized = {
                "_shard_file": relative_path,
                "aggregate_rules": [rule.as_dict() for rule in rules],
                "base_tables": list(base_tables),
                "filter": filter_text,
                "name": name,
                "upstream_views": list(upstream),
            }
            shard_views[name] = normalized
            shard_rules[name] = rules
    return shard_views, shard_hashes, shard_rules


def parse_v2(path: Path) -> ParsedV2:
    manifest_path = path / "manifest.json" if path.is_dir() else path
    root = manifest_path.parent
    manifest = read_json(manifest_path, code="E_V2_BAD_MANIFEST")
    manifest = require_dict(manifest, "v2 manifest", code="E_V2_BAD_MANIFEST")
    if manifest.get("format") != "v2":
        raise fail("E_V2_BAD_MANIFEST", "v2 manifest must declare format=v2", path=str(manifest_path))
    manifest_version = require_int(
        manifest.get("manifest_version"),
        "manifest_version",
        minimum=0,
        code="E_V2_BAD_MANIFEST",
    )
    logical_clock_ms = require_int(
        manifest.get("logical_clock_ms"),
        "v2 logical_clock_ms",
        minimum=0,
        code="E_V2_BAD_MANIFEST",
    )
    generation_floor = require_int(
        manifest.get("view_generation_floor"),
        "view_generation_floor",
        minimum=0,
        code="E_V2_BAD_MANIFEST",
    )
    base_schema = _parse_base_schema(manifest.get("base_schema"), code="E_V2_BAD_MANIFEST")
    shard_views, shard_hashes, shard_rule_map = _read_v2_shards(root, manifest, base_schema)

    metadata_rel = require_str(
        manifest.get("metadata_db", "metadata.sqlite"),
        "metadata_db",
        code="E_V2_BAD_MANIFEST",
    )
    metadata_path = safe_child(root, metadata_rel)
    if not metadata_path.is_file():
        raise fail("E_INPUT_NOT_FOUND", "v2 SQLite metadata does not exist", path=str(metadata_path))
    revisions = _read_v2_sqlite(metadata_path, generation_floor=generation_floor)

    current_names = set(revisions)
    if set(shard_views) != {
        name for name, items in revisions.items() if items[-1].status == "active"
    }:
        raise fail(
            "E_METADATA_MISMATCH",
            "active SQLite metadata rows and shard views do not form the same set",
            shard_views=sorted(shard_views),
            active_metadata=sorted(
                name for name, items in revisions.items() if items[-1].status == "active"
            ),
        )

    for name, items in revisions.items():
        current = items[-1]
        if current.status == "tombstoned":
            if name in shard_views:
                raise fail(
                    "E_TOMBSTONE_INCONSISTENT",
                    "tombstoned view must not have an active shard definition",
                    view=name,
                )
            continue
        raw_view = shard_views[name]
        if current.shard_file != raw_view["_shard_file"]:
            raise fail(
                "E_METADATA_MISMATCH",
                "current SQLite revision points to a different shard",
                view=name,
                metadata_shard=current.shard_file,
                actual_shard=raw_view["_shard_file"],
            )
        normalized = {
            key: raw_view[key]
            for key in ("aggregate_rules", "base_tables", "filter", "name", "upstream_views")
        }
        if current.spec_digest != digest_json(normalized):
            raise fail(
                "E_METADATA_MISMATCH",
                "current SQLite spec_digest does not match the shard definition",
                view=name,
                metadata=current.spec_digest,
                shard=digest_json(normalized),
            )
        expected_rules = tuple(
            AggregateRule.from_dict(rule) for rule in raw_view["aggregate_rules"]
        )
        if current.base_tables != tuple(raw_view["base_tables"]):
            raise fail("E_METADATA_MISMATCH", "base tables differ between SQLite and shard", view=name)
        if current.aggregate_rules != expected_rules:
            raise fail("E_METADATA_MISMATCH", "aggregate rules differ between SQLite and shard", view=name)
        if current.upstream_views != tuple(raw_view["upstream_views"]):
            raise fail("E_METADATA_MISMATCH", "upstream dependencies differ between SQLite and shard", view=name)

    current_specs: dict[str, ViewSpec] = {}
    for name in sorted(revisions):
        current = revisions[name][-1]
        if current.status == "active":
            raw_view = shard_views[name]
            current_specs[name] = ViewSpec(
                name=name,
                base_tables=current.base_tables,
                filter_text=raw_view["filter"],
                aggregate_rules=current.aggregate_rules,
                upstream_views=current.upstream_views,
                generation=current.generation,
                logical_clock_ms=current.logical_clock_ms,
                absolute_refresh_ms=current.absolute_refresh_ms,
                refresh_origin={
                    "absolute_ms": current.absolute_refresh_ms,
                    "representation": "absolute_ms",
                },
                status="active",
                source_format="v2",
                source_ref=f"v2:{manifest_path.name}:{current.shard_file}#{name}",
            )
        else:
            current_specs[name] = ViewSpec(
                name=name,
                base_tables=current.base_tables,
                filter_text="true" if not current.base_tables else "true",
                aggregate_rules=current.aggregate_rules,
                upstream_views=current.upstream_views,
                generation=current.generation,
                logical_clock_ms=current.logical_clock_ms,
                absolute_refresh_ms=current.absolute_refresh_ms,
                refresh_origin={
                    "absolute_ms": current.absolute_refresh_ms,
                    "representation": "absolute_ms",
                },
                status="tombstoned",
                source_format="v2",
                source_ref=f"v2:{manifest_path.name}:tombstone#{name}",
                tombstone_reason=current.tombstone_reason,
                tombstoned_at_ms=current.tombstoned_at_ms,
            )

    _validate_v2_dependencies(current_specs)
    return ParsedV2(
        path=manifest_path,
        manifest_sha256=digest_file(manifest_path),
        manifest_version=manifest_version,
        logical_clock_ms=logical_clock_ms,
        view_generation_floor=generation_floor,
        base_schema=base_schema,
        views=current_specs,
        revisions=revisions,
        shard_hashes=shard_hashes,
        raw_manifest=manifest,
    )


def _read_v2_sqlite(path: Path, *, generation_floor: int) -> dict[str, tuple[V2Revision, ...]]:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise fail("E_METADATA_INVALID", "cannot open v2 SQLite metadata", detail=str(exc))
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "view_revisions" not in tables:
            raise fail("E_METADATA_INVALID", "v2 metadata is missing view_revisions table")
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(view_revisions)")
        }
        required = {
            "seq",
            "view_name",
            "generation",
            "logical_clock_ms",
            "absolute_refresh_ms",
            "status",
            "base_tables_json",
            "aggregate_rules_json",
            "upstream_views_json",
            "shard_file",
            "spec_digest",
            "tombstone_reason",
            "tombstoned_at_ms",
        }
        missing = sorted(required - columns)
        if missing:
            raise fail(
                "E_METADATA_INVALID",
                "view_revisions table is missing required columns",
                missing=missing,
            )
        rows = list(
            connection.execute(
                """
                SELECT seq, view_name, generation, logical_clock_ms,
                       absolute_refresh_ms, status, base_tables_json,
                       aggregate_rules_json, upstream_views_json, shard_file,
                       spec_digest, tombstone_reason, tombstoned_at_ms
                FROM view_revisions
                ORDER BY seq
                """
            )
        )
    except EngineError:
        raise
    except sqlite3.Error as exc:
        raise fail(
            "E_METADATA_INVALID",
            "cannot read v2 SQLite metadata",
            detail=str(exc),
        )
    finally:
        connection.close()

    grouped: dict[str, list[V2Revision]] = {}
    seen_pairs: set[tuple[str, int]] = set()
    for row in rows:
        sequence = require_int(row["seq"], "view_revisions.seq", minimum=1, code="E_METADATA_INVALID")
        name = require_str(row["view_name"], "view_revisions.view_name", code="E_METADATA_INVALID")
        generation = require_int(
            row["generation"],
            f"view {name}.generation",
            minimum=0,
            code="E_GENERATION_NOT_MONOTONIC",
        )
        pair = (name, generation)
        if pair in seen_pairs:
            raise fail(
                "E_GENERATION_NOT_MONOTONIC",
                "duplicate view generation in metadata history",
                view=name,
                generation=generation,
            )
        seen_pairs.add(pair)
        logical_clock_ms = require_int(
            row["logical_clock_ms"],
            f"view {name}.logical_clock_ms",
            minimum=0,
            code="E_METADATA_INVALID",
        )
        absolute_refresh_ms = require_int(
            row["absolute_refresh_ms"],
            f"view {name}.absolute_refresh_ms",
            minimum=0,
            code="E_BAD_REFRESH_TIME",
        )
        status = require_str(row["status"], f"view {name}.status", code="E_TOMBSTONE_INCONSISTENT")
        if status not in {"active", "tombstoned"}:
            raise fail(
                "E_TOMBSTONE_INCONSISTENT",
                "view status must be active or tombstoned",
                view=name,
                status=status,
            )
        try:
            base_tables = tuple(
                require_str(item, f"view {name}.base_tables[]", code="E_METADATA_INVALID")
                for item in require_list(
                    json.loads(row["base_tables_json"]),
                    f"view {name}.base_tables",
                    code="E_METADATA_INVALID",
                )
            )
            rule_dicts = json.loads(row["aggregate_rules_json"])
            require_list(
                rule_dicts,
                f"view {name}.aggregate_rules",
                code="E_METADATA_INVALID",
            )
            rules = tuple(
                AggregateRule.from_dict(
                    require_dict(item, "aggregate rule"),
                    code="E_METADATA_INVALID",
                )
                for item in rule_dicts
            )
            upstream = tuple(
                require_str(item, f"view {name}.upstream_views[]", code="E_METADATA_INVALID")
                for item in require_list(
                    json.loads(row["upstream_views_json"]),
                    f"view {name}.upstream_views",
                    code="E_METADATA_INVALID",
                )
            )
        except (json.JSONDecodeError, TypeError, EngineError) as exc:
            if isinstance(exc, EngineError):
                raise
            raise fail(
                "E_METADATA_INVALID",
                "SQLite metadata contains invalid JSON",
                view=name,
                detail=str(exc),
            )
        spec_digest = require_str(row["spec_digest"], f"view {name}.spec_digest", code="E_METADATA_INVALID")
        if not _HASH_RE.fullmatch(spec_digest):
            raise fail(
                "E_METADATA_INVALID",
                "spec_digest must be a lowercase SHA-256 digest",
                view=name,
            )
        reason = row["tombstone_reason"]
        tombstoned_at = row["tombstoned_at_ms"]
        if status == "tombstoned":
            if not isinstance(reason, str) or not reason:
                raise fail(
                    "E_TOMBSTONE_INCONSISTENT",
                    "tombstoned view requires a non-empty reason",
                    view=name,
                )
            tombstoned_at = require_int(
                tombstoned_at,
                f"view {name}.tombstoned_at_ms",
                minimum=0,
                code="E_TOMBSTONE_INCONSISTENT",
            )
            if row["shard_file"] is not None:
                raise fail(
                    "E_TOMBSTONE_INCONSISTENT",
                    "tombstoned revision must not point at an active shard",
                    view=name,
                )
        else:
            if reason is not None or tombstoned_at is not None:
                raise fail(
                    "E_TOMBSTONE_INCONSISTENT",
                    "active revision carries tombstone fields",
                    view=name,
                )
            if not isinstance(row["shard_file"], str) or not row["shard_file"]:
                raise fail(
                    "E_METADATA_MISMATCH",
                    "active revision requires a shard file",
                    view=name,
                )
        if status == "active" and (
            not base_tables
            or sorted(rule.function for rule in rules) != sorted(SUPPORTED_AGGREGATES)
        ):
            raise fail(
                "E_METADATA_INVALID",
                "metadata revision does not contain a complete unified view spec",
                view=name,
                sequence=sequence,
            )
        grouped.setdefault(name, []).append(
            V2Revision(
                sequence=sequence,
                view_name=name,
                generation=generation,
                logical_clock_ms=logical_clock_ms,
                absolute_refresh_ms=absolute_refresh_ms,
                status=status,
                base_tables=base_tables,
                aggregate_rules=rules,
                upstream_views=upstream,
                shard_file=row["shard_file"],
                spec_digest=spec_digest,
                tombstone_reason=reason,
                tombstoned_at_ms=tombstoned_at,
            )
        )

    if not grouped:
        raise fail("E_METADATA_INVALID", "v2 metadata contains no view revisions")
    result: dict[str, tuple[V2Revision, ...]] = {}
    for name, items in grouped.items():
        for previous, current in zip(items, items[1:]):
            if current.generation <= previous.generation:
                raise fail(
                    "E_GENERATION_NOT_MONOTONIC",
                    "view generation history is not strictly increasing",
                    view=name,
                    previous=previous.generation,
                    current=current.generation,
                )
        if items[-1].generation < generation_floor:
            raise fail(
                "E_GENERATION_NOT_MONOTONIC",
                "current view generation is below the declared floor",
                view=name,
                generation=items[-1].generation,
                floor=generation_floor,
            )
        result[name] = tuple(items)
    return result


def _validate_v2_dependencies(views: dict[str, ViewSpec]) -> None:
    names = set(views)
    for name, spec in views.items():
        for upstream in spec.upstream_views:
            if upstream not in names:
                raise fail(
                    "E_DEPENDENCY_UNDEFINED",
                    "upstream view dependency is not closed",
                    view=name,
                    upstream=upstream,
                )
            if spec.status == "active" and views[upstream].status != "active":
                raise fail(
                    "E_DEPENDENCY_TOMBSTONED",
                    "active view depends on a tombstoned view",
                    view=name,
                    upstream=upstream,
                )
    color: dict[str, int] = {}

    def visit(name: str, path: list[str]) -> None:
        state = color.get(name, 0)
        if state == 1:
            cycle_start = path.index(name)
            raise fail(
                "E_DEPENDENCY_CYCLE",
                "upstream view dependency graph contains a cycle",
                cycle=path[cycle_start:] + [name],
            )
        if state == 2:
            return
        color[name] = 1
        for upstream in views[name].upstream_views:
            visit(upstream, path + [name])
        color[name] = 2

    for name in sorted(views):
        visit(name, [])


def source_digest(parsed: ParsedV1 | ParsedV2) -> str:
    if isinstance(parsed, ParsedV1):
        return parsed.file_sha256
    return parsed.manifest_sha256

