from __future__ import annotations

import copy
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from .errors import (
    BusinessError,
    E_BAD_STAT,
    E_DB_DIGEST_MISMATCH,
    E_STAT_EFFECTIVE_ROLLBACK,
    E_STAT_VERSION_ROLLBACK,
    E_UNKNOWN_STAT_COLUMN,
    E_UNKNOWN_STAT_INDEX,
)
from .util import is_int, normalize_sqlite_value, quote_ident, sha256_json


@dataclass(frozen=True)
class Catalog:
    path: str
    digest: str
    tables: dict[str, dict[str, Any]]


def _read_only_connection(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise BusinessError(E_BAD_STAT, "sample database does not exist", {"path": path})
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise BusinessError(E_BAD_STAT, "cannot open sample database", {"path": path, "reason": str(exc)}) from exc


def read_catalog(path: str) -> Catalog:
    conn = _read_only_connection(path)
    try:
        conn.row_factory = sqlite3.Row
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        if len(names) != len(set(names)):
            raise BusinessError(E_BAD_STAT, "sample database contains duplicate table names", {"path": path})
        tables: dict[str, dict[str, Any]] = {}
        for table in names:
            columns = [
                {
                    "name": row[1],
                    "type": (row[2] or "").upper(),
                    "notnull": int(bool(row[3])),
                    "pk": int(row[5]),
                }
                for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})")
            ]
            if len({column["name"] for column in columns}) != len(columns):
                raise BusinessError(E_BAD_STAT, "sample database contains duplicate column names", {"table": table})
            indexes = []
            seen_indexes: set[str] = set()
            for index_row in conn.execute(f"PRAGMA index_list({quote_ident(table)})").fetchall():
                index_name = index_row[1]
                if index_name in seen_indexes:
                    raise BusinessError(
                        E_BAD_STAT,
                        "sample database contains duplicate index names",
                        {"table": table, "index": index_name},
                    )
                seen_indexes.add(index_name)
                index_columns = [
                    row[2]
                    for row in conn.execute(f"PRAGMA index_info({quote_ident(index_name)})").fetchall()
                ]
                indexes.append(
                    {
                        "name": index_name,
                        "unique": int(bool(index_row[2])),
                        "columns": index_columns,
                    }
                )
            indexes.sort(key=lambda item: item["name"])
            rows = [
                [normalize_sqlite_value(value) for value in row]
                for row in conn.execute(f"SELECT * FROM {quote_ident(table)} ORDER BY rowid")
            ]
            tables[table] = {"columns": columns, "indexes": indexes, "rows": rows}
        digest = sha256_json({"tables": tables})
        return Catalog(path=os.path.abspath(path), digest=digest, tables=tables)
    except sqlite3.Error as exc:
        raise BusinessError(E_BAD_STAT, "cannot inspect sample database", {"path": path, "reason": str(exc)}) from exc
    finally:
        conn.close()


def _bad_stat(message: str, details: dict[str, Any] | None = None) -> BusinessError:
    return BusinessError(E_BAD_STAT, message, details or {})


def _number(value: Any, field: str, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad_stat(f"{field} must be numeric", {"field": field, "value": value})
    numeric = float(value)
    if numeric < minimum or (maximum is not None and numeric > maximum):
        raise _bad_stat(f"{field} is out of range", {"field": field, "value": value})
    return numeric


def snapshot_content_digest(snapshot: dict[str, Any]) -> str:
    payload = {key: value for key, value in snapshot.items() if key != "content_digest"}
    return sha256_json(payload)


def validate_snapshot(snapshot: Any, catalog: Catalog, *, path: str | None = None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise _bad_stat("statistics snapshot must be an object", {"path": path})
    required = {"version", "collected_at_ms", "effective_at_ms", "database_digest", "content_digest", "tables"}
    missing = sorted(required - set(snapshot))
    if missing:
        raise _bad_stat("statistics snapshot is missing required fields", {"path": path, "missing": missing})
    version = snapshot["version"]
    if not is_int(version) or version <= 0:
        raise _bad_stat("statistics version must be a positive integer", {"path": path, "version": version})
    collected = snapshot["collected_at_ms"]
    effective = snapshot["effective_at_ms"]
    if not is_int(collected) or collected < 0:
        raise _bad_stat("collected_at_ms must be a non-negative integer", {"version": version, "value": collected})
    if not is_int(effective) or effective < 0:
        raise _bad_stat("effective_at_ms must be a non-negative integer", {"version": version, "value": effective})
    if effective < collected:
        raise _bad_stat(
            "effective_at_ms cannot precede collected_at_ms",
            {"version": version, "collected_at_ms": collected, "effective_at_ms": effective},
        )
    database_digest = snapshot["database_digest"]
    if not isinstance(database_digest, str) or len(database_digest) != 64:
        raise _bad_stat("database_digest must be a SHA-256 hex string", {"version": version})
    if database_digest != catalog.digest:
        raise BusinessError(
            E_DB_DIGEST_MISMATCH,
            "statistics snapshot database digest does not match the sample database",
            {
                "version": version,
                "declared": database_digest,
                "actual": catalog.digest,
                "sample_db": catalog.path,
            },
        )
    expected_digest = snapshot_content_digest(snapshot)
    if snapshot["content_digest"] != expected_digest:
        raise _bad_stat(
            "statistics content_digest does not match its content",
            {"version": version, "declared": snapshot["content_digest"], "actual": expected_digest},
        )
    tables = snapshot["tables"]
    if not isinstance(tables, dict):
        raise _bad_stat("tables must be an object", {"version": version})
    if set(tables) != set(catalog.tables):
        raise _bad_stat(
            "snapshot tables do not match the sample database",
            {
                "version": version,
                "missing": sorted(set(catalog.tables) - set(tables)),
                "unknown": sorted(set(tables) - set(catalog.tables)),
            },
        )
    for table_name in sorted(tables):
        table_stats = tables[table_name]
        actual_table = catalog.tables[table_name]
        if not isinstance(table_stats, dict):
            raise _bad_stat("table statistics must be an object", {"version": version, "table": table_name})
        row_count = table_stats.get("row_count")
        if not is_int(row_count) or row_count < 0:
            raise _bad_stat(
                "table row_count must be a non-negative integer",
                {"version": version, "table": table_name, "value": row_count},
            )
        columns = table_stats.get("columns")
        if not isinstance(columns, dict):
            raise _bad_stat("table columns must be an object", {"version": version, "table": table_name})
        actual_columns = {column["name"] for column in actual_table["columns"]}
        if set(columns) != actual_columns:
            raise _bad_stat(
                "snapshot columns do not match the sample database",
                {
                    "version": version,
                    "table": table_name,
                    "missing": sorted(actual_columns - set(columns)),
                    "unknown": sorted(set(columns) - actual_columns),
                },
            )
        for column_name in sorted(columns):
            column_stats = columns[column_name]
            if not isinstance(column_stats, dict):
                raise _bad_stat(
                    "column statistics must be an object",
                    {"version": version, "table": table_name, "column": column_name},
                )
            ndv = column_stats.get("ndv")
            if not is_int(ndv) or ndv <= 0:
                raise _bad_stat(
                    "column NDV must be a positive integer",
                    {"version": version, "table": table_name, "column": column_name, "value": ndv},
                )
            if ndv > row_count:
                raise _bad_stat(
                    "column NDV cannot exceed table row_count",
                    {"version": version, "table": table_name, "column": column_name, "ndv": ndv, "row_count": row_count},
                )
        indexes = table_stats.get("indexes")
        if not isinstance(indexes, dict):
            raise _bad_stat("table indexes must be an object", {"version": version, "table": table_name})
        actual_indexes = {index["name"]: index for index in actual_table["indexes"]}
        if set(indexes) != set(actual_indexes):
            raise _bad_stat(
                "snapshot indexes do not match the sample database",
                {
                    "version": version,
                    "table": table_name,
                    "missing": sorted(set(actual_indexes) - set(indexes)),
                    "unknown": sorted(set(indexes) - set(actual_indexes)),
                },
            )
        for index_name in sorted(indexes):
            index_stats = indexes[index_name]
            if not isinstance(index_stats, dict):
                raise _bad_stat(
                    "index statistics must be an object",
                    {"version": version, "table": table_name, "index": index_name},
                )
            if index_stats.get("columns") != actual_indexes[index_name]["columns"]:
                raise _bad_stat(
                    "index columns do not match the sample database",
                    {
                        "version": version,
                        "table": table_name,
                        "index": index_name,
                        "declared": index_stats.get("columns"),
                        "actual": actual_indexes[index_name]["columns"],
                    },
                )
            _number(
                index_stats.get("prefix_selectivity"),
                f"index {index_name} prefix_selectivity",
                0.0,
                1.0,
            )
    return snapshot


def flatten_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for table_name in sorted(snapshot["tables"]):
        table = snapshot["tables"][table_name]
        items.append(
            {
                "item_key": f"table:{table_name}:row_count",
                "kind": "table_row_count",
                "table": table_name,
                "column": None,
                "index": None,
                "value": table["row_count"],
            }
        )
        for column_name in sorted(table["columns"]):
            items.append(
                {
                    "item_key": f"column:{table_name}.{column_name}:ndv",
                    "kind": "column_ndv",
                    "table": table_name,
                    "column": column_name,
                    "index": None,
                    "value": table["columns"][column_name]["ndv"],
                }
            )
        for index_name in sorted(table["indexes"]):
            items.append(
                {
                    "item_key": f"index:{index_name}:prefix_selectivity",
                    "kind": "index_prefix_selectivity",
                    "table": table_name,
                    "column": None,
                    "index": index_name,
                    "value": table["indexes"][index_name]["prefix_selectivity"],
                }
            )
    items.sort(key=lambda item: item["item_key"])
    return items


def parse_stats_document(document: Any, catalog: Catalog, *, path: str | None = None) -> dict[str, Any]:
    return validate_snapshot(document, catalog, path=path)


def build_patched_snapshot(base: dict[str, Any], patch: Any, catalog: Catalog) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(patch, dict):
        raise _bad_stat("statistics patch must be an object")
    required = {"target_version", "base_version", "collected_at_ms", "effective_at_ms", "content_digest", "changes"}
    missing = sorted(required - set(patch))
    if missing:
        raise _bad_stat("statistics patch is missing required fields", {"missing": missing})
    target = patch["target_version"]
    base_version = patch["base_version"]
    if not is_int(target) or target <= 0:
        raise _bad_stat("patch target_version must be a positive integer", {"target_version": target})
    if not is_int(base_version) or base_version <= 0:
        raise _bad_stat("patch base_version must be a positive integer", {"base_version": base_version})
    if target < base_version:
        raise BusinessError(
            E_STAT_VERSION_ROLLBACK,
            "statistics patch target version would roll back the snapshot version",
            {"target_version": target, "base_version": base_version},
        )
    if target == base_version:
        raise BusinessError(
            E_STAT_VERSION_ROLLBACK,
            "statistics patch target version must advance the snapshot version",
            {"target_version": target, "base_version": base_version},
        )
    collected = patch["collected_at_ms"]
    effective = patch["effective_at_ms"]
    if not is_int(collected) or collected < 0:
        raise _bad_stat("patch collected_at_ms must be a non-negative integer", {"value": collected})
    if not is_int(effective) or effective < 0:
        raise _bad_stat("patch effective_at_ms must be a non-negative integer", {"value": effective})
    if effective < collected:
        raise _bad_stat(
            "patch effective_at_ms cannot precede collected_at_ms",
            {"collected_at_ms": collected, "effective_at_ms": effective},
        )
    if effective < base["effective_at_ms"]:
        raise BusinessError(
            E_STAT_EFFECTIVE_ROLLBACK,
            "statistics patch effective clock would move backwards",
            {"base_effective_at_ms": base["effective_at_ms"], "effective_at_ms": effective},
        )
    if collected < base["collected_at_ms"]:
        raise BusinessError(
            E_STAT_EFFECTIVE_ROLLBACK,
            "statistics patch collection clock would move backwards",
            {"base_collected_at_ms": base["collected_at_ms"], "collected_at_ms": collected},
        )
    changes = patch["changes"]
    if not isinstance(changes, list) or not changes:
        raise _bad_stat("patch changes must be a non-empty array")
    new_snapshot = copy.deepcopy(base)
    new_snapshot["version"] = target
    new_snapshot["collected_at_ms"] = collected
    new_snapshot["effective_at_ms"] = effective
    normalized_changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            raise _bad_stat("each patch change must be an object", {"change_index": index})
        kind = change.get("kind")
        table_name = change.get("table")
        if kind == "table_row_count":
            if not isinstance(table_name, str) or table_name not in catalog.tables:
                raise _bad_stat(
                    "table_row_count references an unknown table",
                    {"change_index": index, "table": table_name},
                )
            value = change.get("value")
            if not is_int(value) or value < 0:
                raise _bad_stat("table_row_count value must be non-negative", {"change_index": index, "value": value})
            path_key = f"table:{table_name}:row_count"
            new_snapshot["tables"][table_name]["row_count"] = value
            normalized = {
                "kind": kind,
                "table": table_name,
                "column": None,
                "index": None,
                "value": value,
            }
        elif kind == "column_ndv":
            column_name = change.get("column")
            if not isinstance(table_name, str) or table_name not in catalog.tables:
                raise _bad_stat("column_ndv references an unknown table", {"change_index": index, "table": table_name})
            actual_columns = {column["name"] for column in catalog.tables[table_name]["columns"]}
            if not isinstance(column_name, str) or column_name not in actual_columns:
                raise BusinessError(
                    E_UNKNOWN_STAT_COLUMN,
                    "statistics patch references an unknown column",
                    {"change_index": index, "table": table_name, "column": column_name},
                )
            value = change.get("value")
            if not is_int(value) or value <= 0:
                raise _bad_stat("column_ndv value must be a positive integer", {"change_index": index, "value": value})
            new_snapshot["tables"][table_name]["columns"][column_name]["ndv"] = value
            path_key = f"column:{table_name}.{column_name}:ndv"
            normalized = {
                "kind": kind,
                "table": table_name,
                "column": column_name,
                "index": None,
                "value": value,
            }
        elif kind == "index_prefix_selectivity":
            index_name = change.get("index")
            if not isinstance(table_name, str) or table_name not in catalog.tables or not isinstance(index_name, str):
                raise BusinessError(
                    E_UNKNOWN_STAT_INDEX,
                    "statistics patch references an unknown index",
                    {"change_index": index, "table": table_name, "index": index_name},
                )
            actual_indexes = {
                item["name"]: item for item in catalog.tables[table_name]["indexes"]
            }
            if index_name not in actual_indexes:
                raise BusinessError(
                    E_UNKNOWN_STAT_INDEX,
                    "statistics patch references an unknown index",
                    {"change_index": index, "table": table_name, "index": index_name},
                )
            value = _number(change.get("value"), "index_prefix_selectivity", 0.0, 1.0)
            new_snapshot["tables"][table_name]["indexes"][index_name]["prefix_selectivity"] = value
            path_key = f"index:{index_name}:prefix_selectivity"
            normalized = {
                "kind": kind,
                "table": table_name,
                "column": None,
                "index": index_name,
                "value": value,
            }
        else:
            raise _bad_stat("unknown statistics change kind", {"change_index": index, "kind": kind})
        if path_key in seen:
            raise _bad_stat("duplicate statistics change item", {"change_index": index, "item": path_key})
        seen.add(path_key)
        normalized_changes.append(normalized)
    new_snapshot.pop("content_digest", None)
    new_snapshot["content_digest"] = snapshot_content_digest(new_snapshot)
    validate_snapshot(new_snapshot, catalog)
    if patch["content_digest"] != new_snapshot["content_digest"]:
        raise _bad_stat(
            "patch content_digest does not match the resulting snapshot",
            {
                "declared": patch["content_digest"],
                "actual": new_snapshot["content_digest"],
                "target_version": target,
            },
        )
    old_items = {item["item_key"]: item for item in flatten_snapshot(base)}
    new_items = {item["item_key"]: item for item in flatten_snapshot(new_snapshot)}
    for normalized in normalized_changes:
        if normalized["kind"] == "table_row_count":
            key = f"table:{normalized['table']}:row_count"
        elif normalized["kind"] == "column_ndv":
            key = f"column:{normalized['table']}.{normalized['column']}:ndv"
        else:
            key = f"index:{normalized['index']}:prefix_selectivity"
        normalized["item_key"] = key
        normalized["old_value"] = old_items[key]["value"]
        normalized["new_value"] = new_items[key]["value"]
    return new_snapshot, {"changes": normalized_changes, "content_digest": new_snapshot["content_digest"]}
