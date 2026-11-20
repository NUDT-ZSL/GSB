from __future__ import annotations

import copy
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import error
from .model import apply_operation, schema_for_digest
from .snapshot import (
    INTERNAL_LOG_TABLE,
    connect_snapshot,
    index_create_sql,
    ordered_views,
    table_create_sql,
    trigger_create_sql,
    view_create_sql,
)
from .typesys import parse_type, python_sql_value, sql_default_literal
from .util import canonical_json, canonical_value, decimal_text, quantize_decimal


def initialize_operation_log(stage_path: Path, version: int) -> None:
    conn = connect_snapshot(stage_path, read_only=False)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA journal_mode=DELETE")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {INTERNAL_LOG_TABLE} (
              op_id TEXT PRIMARY KEY,
              op_index INTEGER NOT NULL,
              payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute(f"PRAGMA user_version={int(version)}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_operation_log(stage_path: Path) -> List[Dict[str, Any]]:
    if not stage_path.exists():
        return []
    conn = sqlite3.connect(str(stage_path), timeout=30.0)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (INTERNAL_LOG_TABLE,),
        ).fetchone()
        if not exists:
            return []
        rows = conn.execute(
            f"SELECT op_id, op_index, payload_json FROM {INTERNAL_LOG_TABLE} ORDER BY op_index"
        ).fetchall()
        return [
            {"op_id": row[0], "op_index": row[1], "payload": json.loads(row[2])}
            for row in rows
        ]
    finally:
        conn.close()


def _table_by_name(schema: Dict[str, Any], name: str) -> Dict[str, Any]:
    for table in schema.get("tables", []):
        if table["name"] == name:
            return table
    raise error("E_OBJECT_NOT_FOUND", "table not found during migration", table=name)


def _column_by_name(table: Dict[str, Any], name: str) -> Dict[str, Any]:
    for column in table["columns"]:
        if column["name"] == name:
            return column
    raise error("E_OBJECT_NOT_FOUND", "column not found during migration", table=table["name"], column=name)


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def _convert_value(
    old_type: str,
    new_type: str,
    value: Any,
    conversion: Optional[Dict[str, Any]],
) -> Any:
    if value is None:
        return None
    new_info = parse_type(new_type)
    conversion = conversion or {}
    try:
        if new_info.family == "decimal":
            scale = conversion.get("target_scale")
            if scale is None:
                scale = new_info.scale if new_info.scale is not None else 0
            rounding = str(conversion.get("rounding", "ROUND_HALF_EVEN"))
            return float(quantize_decimal(value, int(scale), rounding))
        if new_info.family == "integer":
            return int(_to_decimal(value))
        if new_info.family in ("real", "numeric"):
            return float(_to_decimal(value))
        if new_info.family == "boolean":
            return 1 if bool(value) else 0
        if new_info.family == "blob":
            return str(value).encode("utf-8")
        return str(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise error(
            "E_TYPE_CONVERSION",
            "value cannot be converted to the target type",
            old_type=old_type,
            new_type=new_type,
            value=canonical_value(value),
            detail=str(exc),
        )


def _log_value(value: Any, scale: Optional[int] = None) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            if scale is not None:
                return format(Decimal(str(value)), f".{int(scale)}f")
            return decimal_text(value)
        except Exception:
            return repr(value)
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    return str(value)


def _conversion_log_entry(
    *,
    op_id: str,
    op_index: int,
    operation: Dict[str, Any],
    table: str,
    column: str,
    rowid: int,
    column_index: int,
    old_type: str,
    new_type: str,
    old_value: Any,
    new_value: Any,
    conversion: Dict[str, Any],
) -> Dict[str, Any]:
    new_info = parse_type(new_type)
    source_scale = conversion.get("source_scale")
    if old_type not in ("<absent>", None):
        old_info = parse_type(old_type)
        if source_scale is None and old_info.base == "DECIMAL":
            source_scale = old_info.scale
    target_scale = conversion.get("target_scale")
    if target_scale is None and new_info.base == "DECIMAL":
        target_scale = new_info.scale
    scale_delta = None
    if source_scale is not None and target_scale is not None:
        scale_delta = int(target_scale) - int(source_scale)
    return {
        "operation_id": op_id,
        "operation": operation["op"],
        "table": table,
        "column": column,
        "rowid": rowid,
        "column_index": column_index,
        "old_type": old_type,
        "new_type": new_type,
        "old_value": _log_value(old_value, source_scale),
        "new_value": _log_value(new_value, target_scale),
        "changed": canonical_value(old_value) != canonical_value(new_value),
        "source_scale": source_scale,
        "target_scale": target_scale,
        "scale_delta": scale_delta,
        "rounding": conversion.get("rounding"),
        "operation_order": [op_index, rowid, column_index],
    }


def _insert_default_logs(
    conn: sqlite3.Connection,
    operation: Dict[str, Any],
    op_id: str,
    op_index: int,
) -> List[Dict[str, Any]]:
    table = operation["table"]
    column = operation["column"]
    type_info = parse_type(column["type"])
    default_value = python_sql_value(type_info, column.get("default"))
    rowids = [
        row[0]
        for row in conn.execute(f"SELECT rowid FROM \"{table}\" ORDER BY rowid").fetchall()
    ]
    logs = []
    for rowid in rowids:
        logs.append(
            _conversion_log_entry(
                op_id=op_id,
                op_index=op_index,
                operation=operation,
                table=table,
                column=column["name"],
                rowid=int(rowid),
                column_index=-1,
                old_type="<absent>",
                new_type=column["type"],
                old_value=None,
                new_value=default_value,
                conversion={"target_scale": type_info.scale},
            )
        )
    return logs


def _add_column(
    conn: sqlite3.Connection,
    operation: Dict[str, Any],
    op_id: str,
    op_index: int,
) -> List[Dict[str, Any]]:
    column = operation["column"]
    if column.get("unique"):
        raise error(
            "E_MIGRATION_OP",
            "SQLite cannot add a UNIQUE column in place; add it nullable then build a unique index",
            operation=op_id,
        )
    logs = _insert_default_logs(conn, operation, op_id, op_index)
    type_info = parse_type(column["type"])
    parts = [
        '"' + column["name"].replace('"', '""') + '"',
        type_info.canonical,
    ]
    if column.get("not_null"):
        parts.append("NOT NULL")
    if column.get("has_default"):
        parts.append("DEFAULT")
        parts.append(sql_default_literal(type_info, column.get("default")))
    sql = f'ALTER TABLE "{operation["table"]}" ADD COLUMN ' + " ".join(parts)
    conn.execute(sql)
    return logs


def _rebuild_table_for_type(
    conn: sqlite3.Connection,
    operation: Dict[str, Any],
    before_schema: Dict[str, Any],
    after_schema: Dict[str, Any],
    op_id: str,
    op_index: int,
) -> List[Dict[str, Any]]:
    table_name = operation["table"]
    column_name = operation["column"]
    old_table = _table_by_name(before_schema, table_name)
    new_table = _table_by_name(after_schema, table_name)
    old_column = _column_by_name(old_table, column_name)
    new_column = _column_by_name(new_table, column_name)
    conversion = operation.get("conversion", {})
    old_columns = list(old_table["columns"])
    new_columns = list(new_table["columns"])
    if [item["name"] for item in old_columns] != [item["name"] for item in new_columns]:
        raise error("E_MIGRATION_OP", "alter_column_type cannot change column order", operation=op_id)

    temp_name = f"__schema_diag_rebuild_{table_name}"
    conn.execute(f'DROP TABLE IF EXISTS "{temp_name}"')
    temp_table = copy.deepcopy(new_table)
    temp_table["name"] = temp_name
    conn.execute(table_create_sql(temp_table))

    old_column_index = [item["name"] for item in old_columns].index(column_name)
    column_names = [item["name"] for item in old_columns]
    placeholders = ", ".join("?" for _ in column_names)
    insert_sql = (
        f'INSERT INTO "{temp_name}" ('
        + ", ".join('"' + name.replace('"', '""') + '"' for name in column_names)
        + f") VALUES ({placeholders})"
    )
    select_sql = (
        "SELECT rowid, "
        + ", ".join('"' + name.replace('"', '""') + '"' for name in column_names)
        + f' FROM "{table_name}" ORDER BY rowid'
    )
    rows = conn.execute(select_sql).fetchall()
    converted_rows: List[List[Any]] = []
    logs: List[Dict[str, Any]] = []
    for row in rows:
        rowid = int(row[0])
        values = list(row[1:])
        converted = list(values)
        old_value = values[old_column_index]
        new_value = _convert_value(old_column["type"], new_column["type"], old_value, conversion)
        converted[old_column_index] = new_value
        converted_rows.append(converted)
        logs.append(
            _conversion_log_entry(
                op_id=op_id,
                op_index=op_index,
                operation=operation,
                table=table_name,
                column=column_name,
                rowid=rowid,
                column_index=old_column_index,
                old_type=old_column["type"],
                new_type=new_column["type"],
                old_value=old_value,
                new_value=new_value,
                conversion=conversion,
            )
        )
    if converted_rows:
        conn.executemany(insert_sql, converted_rows)

    trigger_sql = [
        trigger["sql"]
        for trigger in after_schema.get("triggers", [])
        if trigger["table"] == table_name
    ]
    index_sql = [
        index_create_sql(index)
        for index in after_schema.get("indexes", [])
        if index["table"] == table_name
    ]
    for view in before_schema.get("views", []):
        conn.execute(f'DROP VIEW IF EXISTS "{view["name"]}"')
    conn.execute(f'DROP TABLE "{table_name}"')
    conn.execute(f'ALTER TABLE "{temp_name}" RENAME TO "{table_name}"')
    for sql in index_sql:
        conn.execute(sql)
    for view in ordered_views(after_schema):
        conn.execute(view_create_sql(view))
    for sql in trigger_sql:
        conn.execute(sql)
    return logs


def _apply_one_sql(
    conn: sqlite3.Connection,
    operation: Dict[str, Any],
    before_schema: Dict[str, Any],
    after_schema: Dict[str, Any],
    op_id: str,
    op_index: int,
) -> List[Dict[str, Any]]:
    op = operation["op"]
    if op == "add_column":
        return _add_column(conn, operation, op_id, op_index)
    if op == "alter_column_type":
        return _rebuild_table_for_type(
            conn, operation, before_schema, after_schema, op_id, op_index
        )
    if op == "drop_column":
        conn.execute(
            f'ALTER TABLE "{operation["table"]}" DROP COLUMN "{operation["column"]}"'
        )
        return []
    if op == "create_index":
        conn.execute(index_create_sql(operation))
        return []
    if op == "drop_index":
        conn.execute(f'DROP INDEX "{operation["name"]}"')
        return []
    if op == "replace_view":
        conn.execute(f'DROP VIEW IF EXISTS "{operation["name"]}"')
        conn.execute(view_create_sql(operation))
        return []
    if op == "replace_trigger":
        conn.execute(f'DROP TRIGGER IF EXISTS "{operation["name"]}"')
        conn.execute(trigger_create_sql(operation))
        return []
    if op == "rename_table":
        conn.execute(
            f'ALTER TABLE "{operation["old_name"]}" RENAME TO "{operation["new_name"]}"'
        )
        return []
    if op == "rename_column":
        conn.execute(
            f'ALTER TABLE "{operation["table"]}" RENAME COLUMN '
            f'"{operation["old_name"]}" TO "{operation["new_name"]}"'
        )
        return []
    raise error("E_MIGRATION_OP", "unsupported operation during SQL execution", op=op)


def apply_operation_atomically(
    stage_path: Path,
    operation: Dict[str, Any],
    before_schema: Dict[str, Any],
    after_schema: Dict[str, Any],
    op_id: str,
    op_index: int,
) -> List[Dict[str, Any]]:
    conn = connect_snapshot(stage_path, read_only=False)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        logs = _apply_one_sql(
            conn,
            operation,
            before_schema,
            after_schema,
            op_id,
            op_index,
        )
        payload = {
            "operation_id": op_id,
            "operation": operation,
            "conversion_log": logs,
        }
        conn.execute(
            f"INSERT INTO {INTERNAL_LOG_TABLE}(op_id, op_index, payload_json) VALUES (?, ?, ?)",
            (op_id, op_index, canonical_json(payload)),
        )
        violation = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violation:
            raise error(
                "E_FOREIGN_KEY_VIOLATION",
                "operation introduced a foreign-key violation",
                operation=op_id,
                violations=violation,
            )
        conn.commit()
        return logs
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def collect_all_conversion_logs(stage_path: Path) -> List[Dict[str, Any]]:
    logs: List[Dict[str, Any]] = []
    for item in read_operation_log(stage_path):
        payload = item["payload"]
        for entry in payload.get("conversion_log", []):
            logs.append(entry)
    logs.sort(
        key=lambda entry: (
            entry.get("operation_order", [0])[0],
            entry.get("operation_order", [0, 0])[1],
            entry.get("operation_order", [0, 0, 0])[2],
        )
    )
    return logs


def finalize_stage_schema(stage_path: Path, target_schema: Dict[str, Any]) -> None:
    conn = connect_snapshot(stage_path, read_only=False)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"DROP TABLE IF EXISTS {INTERNAL_LOG_TABLE}")
        conn.execute(f"PRAGMA user_version={int(target_schema['version'])}")
        conn.commit()
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise error(
                "E_FOREIGN_KEY_VIOLATION",
                "final migration stage violates foreign keys",
                violations=violations,
            )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise error("E_INTEGRITY", "migration stage failed integrity_check", result=integrity)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

