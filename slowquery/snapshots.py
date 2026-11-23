"""Statistics snapshot normalization, patching, and logical-clock binding."""

from __future__ import annotations

import copy
from typing import Any

from .canonical import (
    canonical_json,
    digest,
    require_int,
    require_list,
    require_mapping,
    require_nonempty_str,
    require_number,
    sorted_unique,
    stable_round,
)
from .errors import EngineError


def _normalize_columns(
    value: Any, path: str, *, partial: bool
) -> dict[str, dict[str, int]]:
    columns: dict[str, dict[str, int]] = {}
    if isinstance(value, list):
        iterable = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            entry = require_mapping(item, "ERR_SNAPSHOT_SCHEMA", item_path)
            name = require_nonempty_str(
                entry.get("name"), "ERR_SNAPSHOT_SCHEMA", f"{item_path}.name"
            ).lower()
            iterable.append((name, entry, item_path))
    elif isinstance(value, dict):
        iterable = []
        for name, entry in value.items():
            item_path = f"{path}.{name}"
            normalized_name = require_nonempty_str(
                name, "ERR_SNAPSHOT_SCHEMA", item_path
            ).lower()
            if isinstance(entry, int) and not isinstance(entry, bool):
                entry = {"ndv": entry}
            iterable.append(
                (
                    normalized_name,
                    require_mapping(entry, "ERR_SNAPSHOT_SCHEMA", item_path),
                    item_path,
                )
            )
    else:
        raise EngineError(
            "ERR_SNAPSHOT_SCHEMA", f"{path} must be an object or array", {"path": path}
        )
    for name, entry, item_path in iterable:
        if name in columns:
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA",
                "duplicate column statistic",
                {"path": path, "column": name},
            )
        if "ndv" not in entry and not partial:
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA",
                "column statistic requires ndv",
                {"path": item_path},
            )
        column: dict[str, int] = {}
        if "ndv" in entry:
            column["ndv"] = require_int(
                entry["ndv"],
                "ERR_SNAPSHOT_SCHEMA",
                f"{item_path}.ndv",
                minimum=0,
            )
        columns[name] = column
    return columns


def _normalize_selectivity_map(value: Any, path: str) -> dict[str, float]:
    mapping = require_mapping(value, "ERR_SNAPSHOT_SCHEMA", path)
    result: dict[str, float] = {}
    for key, raw_value in mapping.items():
        normalized_key = ",".join(
            part.strip().lower() for part in require_nonempty_str(
                key, "ERR_SNAPSHOT_SCHEMA", f"{path}.key"
            ).split(",") if part.strip()
        )
        if not normalized_key:
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA", "empty selectivity prefix", {"path": path}
            )
        result[normalized_key] = stable_round(
            require_number(
                raw_value,
                "ERR_SNAPSHOT_SCHEMA",
                f"{path}.{key}",
                minimum=0.0,
                maximum=1.0,
            )
        )
    return dict(sorted(result.items()))


def _normalize_indexes(
    value: Any, path: str, *, partial: bool
) -> dict[str, dict[str, Any]]:
    indexes: dict[str, dict[str, Any]] = {}
    if isinstance(value, list):
        iterable = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            entry = require_mapping(item, "ERR_SNAPSHOT_SCHEMA", item_path)
            name = require_nonempty_str(
                entry.get("name"), "ERR_SNAPSHOT_SCHEMA", f"{item_path}.name"
            ).lower()
            iterable.append((name, entry, item_path))
    elif isinstance(value, dict):
        iterable = []
        for name, entry in value.items():
            item_path = f"{path}.{name}"
            normalized_name = require_nonempty_str(
                name, "ERR_SNAPSHOT_SCHEMA", item_path
            ).lower()
            iterable.append(
                (
                    normalized_name,
                    require_mapping(entry, "ERR_SNAPSHOT_SCHEMA", item_path),
                    item_path,
                )
            )
    else:
        raise EngineError(
            "ERR_SNAPSHOT_SCHEMA", f"{path} must be an object or array", {"path": path}
        )
    for name, entry, item_path in iterable:
        if name in indexes:
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA",
                "duplicate index statistic",
                {"path": path, "index": name},
            )
        index: dict[str, Any] = {}
        if "columns" in entry:
            raw_columns = require_list(
                entry["columns"], "ERR_SNAPSHOT_SCHEMA", f"{item_path}.columns"
            )
            if not raw_columns:
                raise EngineError(
                    "ERR_SNAPSHOT_SCHEMA",
                    "index must contain at least one column",
                    {"path": item_path},
                )
            columns = [
                require_nonempty_str(
                    column,
                    "ERR_SNAPSHOT_SCHEMA",
                    f"{item_path}.columns[{index}]",
                ).lower()
                for index, column in enumerate(raw_columns)
            ]
            if len(columns) != len(set(columns)):
                raise EngineError(
                    "ERR_SNAPSHOT_SCHEMA",
                    "index columns must be unique",
                    {"path": item_path},
                )
            index["columns"] = columns
        elif not partial:
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA",
                "index statistic requires columns",
                {"path": item_path},
            )
        if "unique" in entry:
            if not isinstance(entry["unique"], bool):
                raise EngineError(
                    "ERR_SNAPSHOT_SCHEMA",
                    "index unique must be boolean",
                    {"path": f"{item_path}.unique"},
                )
            index["unique"] = entry["unique"]
        if "prefix_selectivity" in entry:
            index["prefix_selectivity"] = _normalize_selectivity_map(
                entry["prefix_selectivity"], f"{item_path}.prefix_selectivity"
            )
        if "selectivity" in entry:
            index["selectivity"] = stable_round(
                require_number(
                    entry["selectivity"],
                    "ERR_SNAPSHOT_SCHEMA",
                    f"{item_path}.selectivity",
                    minimum=0.0,
                    maximum=1.0,
                )
            )
        indexes[name] = index
    return dict(sorted(indexes.items()))


def _normalize_tables(
    value: Any, path: str, *, partial: bool
) -> dict[str, dict[str, Any]]:
    tables: dict[str, dict[str, Any]] = {}
    if isinstance(value, list):
        iterable = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            entry = require_mapping(item, "ERR_SNAPSHOT_SCHEMA", item_path)
            name = require_nonempty_str(
                entry.get("name"), "ERR_SNAPSHOT_SCHEMA", f"{item_path}.name"
            ).lower()
            iterable.append((name, entry, item_path))
    elif isinstance(value, dict):
        iterable = []
        for name, entry in value.items():
            item_path = f"{path}.{name}"
            normalized_name = require_nonempty_str(
                name, "ERR_SNAPSHOT_SCHEMA", item_path
            ).lower()
            iterable.append(
                (
                    normalized_name,
                    require_mapping(entry, "ERR_SNAPSHOT_SCHEMA", item_path),
                    item_path,
                )
            )
    else:
        raise EngineError(
            "ERR_SNAPSHOT_SCHEMA", f"{path} must be an object or array", {"path": path}
        )
    for name, entry, item_path in iterable:
        if name in tables:
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA",
                "duplicate table statistic",
                {"path": path, "table": name},
            )
        table: dict[str, Any] = {}
        if "row_count" in entry:
            table["row_count"] = require_int(
                entry["row_count"],
                "ERR_SNAPSHOT_SCHEMA",
                f"{item_path}.row_count",
                minimum=0,
            )
        elif not partial:
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA",
                "table statistic requires row_count",
                {"path": item_path},
            )
        if "columns" in entry:
            table["columns"] = _normalize_columns(
                entry["columns"], f"{item_path}.columns", partial=partial
            )
        elif not partial:
            table["columns"] = {}
        if "indexes" in entry:
            table["indexes"] = _normalize_indexes(
                entry["indexes"], f"{item_path}.indexes", partial=partial
            )
        elif not partial:
            table["indexes"] = {}
        tables[name] = table
    return dict(sorted(tables.items()))


def normalize_statistics(
    document: Any,
    *,
    partial: bool = False,
    path: str = "snapshot",
) -> dict[str, Any]:
    mapping = require_mapping(document, "ERR_SNAPSHOT_SCHEMA", path)
    if "version" not in mapping:
        raise EngineError(
            "ERR_SNAPSHOT_SCHEMA", "snapshot version is required", {"path": path}
        )
    version = require_int(
        mapping["version"],
        "ERR_SNAPSHOT_SCHEMA",
        f"{path}.version",
        minimum=1,
    )
    effective_name = (
        "effective_at_ms" if "effective_at_ms" in mapping else "effective_clock_ms"
    )
    effective = require_int(
        mapping.get(effective_name),
        "ERR_SNAPSHOT_SCHEMA",
        f"{path}.{effective_name}",
        minimum=0,
    )
    raw_tables = mapping.get("tables", mapping.get("stats", {}).get("tables"))
    if raw_tables is None:
        raise EngineError(
            "ERR_SNAPSHOT_SCHEMA", "snapshot tables are required", {"path": path}
        )
    tables = _normalize_tables(raw_tables, f"{path}.tables", partial=partial)
    if not tables:
        raise EngineError(
            "ERR_SNAPSHOT_SCHEMA", "snapshot must contain at least one table"
        )
    if "covered_tables" in mapping:
        raw_covered = require_list(
            mapping["covered_tables"],
            "ERR_SNAPSHOT_SCHEMA",
            f"{path}.covered_tables",
        )
        covered = sorted_unique(
            require_nonempty_str(
                item, "ERR_SNAPSHOT_SCHEMA", f"{path}.covered_tables[{index}]"
            ).lower()
            for index, item in enumerate(raw_covered)
        )
        missing = [table for table in covered if table not in tables]
        if missing and not partial:
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA",
                "covered_tables contains tables absent from statistics",
                {"missing": missing},
            )
    else:
        covered = sorted(tables)
    mode = mapping.get("mode", "replace" if not partial else None)
    if mode is not None and mode not in {"replace", "merge"}:
        raise EngineError(
            "ERR_SNAPSHOT_SCHEMA",
            "snapshot mode must be replace or merge",
            {"path": f"{path}.mode"},
        )
    normalized = {
        "version": version,
        "effective_at_ms": effective,
        "tables": tables,
        "covered_tables": covered,
    }
    if partial:
        if mode is not None:
            normalized["mode"] = mode
        declared_covered = mapping.get("covered_tables")
        if declared_covered is not None:
            normalized["covered_tables"] = sorted_unique(
                item.lower() for item in declared_covered
            )
    return normalized


def statistics_digest(tables: dict[str, Any]) -> str:
    return digest({"statistics": tables})


def finalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    result = {
        "version": snapshot["version"],
        "effective_at_ms": snapshot["effective_at_ms"],
        "tables": snapshot["tables"],
        "covered_tables": sorted(set(snapshot["covered_tables"])),
    }
    result["digest"] = statistics_digest(result["tables"])
    return result


def merge_patch(
    current: dict[str, Any] | None,
    patch: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    if current is None:
        if patch.get("mode") == "merge":
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA",
                "an incremental patch requires an existing snapshot",
            )
        classification = "complete"
        return finalize_snapshot(
            {
                "version": patch["version"],
                "effective_at_ms": patch["effective_at_ms"],
                "tables": patch["tables"],
                "covered_tables": patch["covered_tables"],
            }
        ), classification

    current_version = current["version"]
    if patch["version"] < current_version:
        raise EngineError(
            "ERR_SNAPSHOT_ROLLBACK",
            "snapshot version cannot move backwards",
            {"current_version": current_version, "incoming_version": patch["version"]},
        )
    if patch["version"] == current_version:
        if _patch_matches_current(current, patch):
            return copy.deepcopy(current), "no_op"
        raise EngineError(
            "ERR_SNAPSHOT_CONFLICT",
            "snapshot version already exists with different content",
            {"version": patch["version"]},
        )
    mode = patch.get("mode")
    all_tables = set(current["covered_tables"]) | set(patch["covered_tables"])
    if mode == "replace" or (
        mode is None and set(patch["covered_tables"]) >= set(current["covered_tables"])
    ):
        classification = "complete"
        tables = copy.deepcopy(patch["tables"])
        covered = sorted(set(patch["covered_tables"]))
    else:
        classification = "incremental"
        tables = copy.deepcopy(current["tables"])
        for table_name, table_patch in patch["tables"].items():
            base_table = tables.setdefault(
                table_name, {"row_count": 0, "columns": {}, "indexes": {}}
            )
            if "row_count" in table_patch:
                base_table["row_count"] = table_patch["row_count"]
            if "columns" in table_patch:
                base_table.setdefault("columns", {}).update(table_patch["columns"])
            if "indexes" in table_patch:
                base_indexes = base_table.setdefault("indexes", {})
                for index_name, index_patch in table_patch["indexes"].items():
                    merged_index = base_indexes.setdefault(index_name, {})
                    if "prefix_selectivity" in index_patch:
                        merged_prefixes = merged_index.setdefault(
                            "prefix_selectivity", {}
                        )
                        merged_prefixes.update(index_patch["prefix_selectivity"])
                        index_patch = {
                            key: value
                            for key, value in index_patch.items()
                            if key != "prefix_selectivity"
                        }
                    merged_index.update(index_patch)
        covered = sorted(all_tables)

    for table_name, table in tables.items():
        if "row_count" not in table:
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA",
                "merged snapshot table is missing row_count",
                {"table": table_name},
            )
        table.setdefault("columns", {})
        table.setdefault("indexes", {})
        for index_name, index in table["indexes"].items():
            if "columns" not in index:
                raise EngineError(
                    "ERR_SNAPSHOT_SCHEMA",
                    "merged snapshot index is missing columns",
                    {"table": table_name, "index": index_name},
                )
    final = finalize_snapshot(
        {
            "version": patch["version"],
            "effective_at_ms": patch["effective_at_ms"],
            "tables": tables,
            "covered_tables": covered,
        }
    )
    return final, classification


def _patch_matches_current(
    current: dict[str, Any], patch: dict[str, Any]
) -> bool:
    if patch["effective_at_ms"] != current["effective_at_ms"]:
        return False
    if not set(patch["covered_tables"]).issubset(set(current["covered_tables"])):
        return False
    for table_name, table_patch in patch["tables"].items():
        current_table = current["tables"].get(table_name)
        if current_table is None:
            return False
        if (
            "row_count" in table_patch
            and table_patch["row_count"] != current_table.get("row_count")
        ):
            return False
        for column_name, column_patch in table_patch.get("columns", {}).items():
            current_column = current_table.get("columns", {}).get(column_name)
            if current_column is None:
                return False
            if (
                "ndv" in column_patch
                and column_patch["ndv"] != current_column.get("ndv")
            ):
                return False
        for index_name, index_patch in table_patch.get("indexes", {}).items():
            current_index = current_table.get("indexes", {}).get(index_name)
            if current_index is None:
                return False
            if (
                "columns" in index_patch
                and index_patch["columns"] != current_index.get("columns")
            ):
                return False
            if (
                "unique" in index_patch
                and index_patch["unique"] != current_index.get("unique", False)
            ):
                return False
            if (
                "selectivity" in index_patch
                and index_patch["selectivity"] != current_index.get("selectivity")
            ):
                return False
            for prefix, value in index_patch.get(
                "prefix_selectivity", {}
            ).items():
                if (
                    current_index.get("prefix_selectivity", {}).get(prefix)
                    != value
                ):
                    return False
    return True


def snapshot_for_clock(
    snapshots: list[dict[str, Any]], logical_clock_ms: int
) -> dict[str, Any] | None:
    eligible = [
        snapshot
        for snapshot in snapshots
        if snapshot["effective_at_ms"] <= logical_clock_ms
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda snapshot: (snapshot["effective_at_ms"], snapshot["version"]))


def snapshot_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": snapshot["version"],
        "effective_at_ms": snapshot["effective_at_ms"],
        "digest": snapshot["digest"],
        "covered_tables": snapshot["covered_tables"],
    }


def snapshot_json(snapshot: dict[str, Any]) -> str:
    return canonical_json(snapshot)
