"""SQLite source-table inspection and primary-key-order queries."""

from __future__ import annotations

import sqlite3
from typing import Any

from .errors import SorterError


def quote_identifier(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise SorterError("INVALID_IDENTIFIER", "SQLite identifier is empty or invalid")
    return '"' + name.replace('"', '""') + '"'


def inspect_source_table(
    connection: sqlite3.Connection,
    table_name: str,
) -> dict[str, Any]:
    quoted = quote_identifier(table_name)
    try:
        info = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
    except sqlite3.Error as exc:
        raise SorterError(
            "SOURCE_TABLE_ERROR",
            "unable to inspect source table",
            details={"table": table_name, "error": str(exc)},
        ) from exc
    if not info:
        raise SorterError(
            "SOURCE_TABLE_NOT_FOUND",
            f"source table does not exist or has no columns: {table_name}",
            details={"table": table_name},
        )

    schema = [
        {
            "cid": int(row[0]),
            "name": str(row[1]),
            "type": str(row[2] or ""),
            "notnull": int(row[3]),
            "default": row[4],
            "pk": int(row[5]),
        }
        for row in info
    ]
    trigger_column = next(
        (column["name"] for column in schema if column["name"].lower() == "trigger_ts"),
        None,
    )
    if trigger_column is None:
        raise SorterError(
            "MISSING_TRIGGER_TS",
            "source table must contain a trigger_ts column",
            details={"table": table_name},
        )

    primary_key_columns = [
        column["name"]
        for column in sorted(
            (column for column in schema if column["pk"] > 0),
            key=lambda column: column["pk"],
        )
    ]
    column_names = [column["name"] for column in schema]
    select_list = ", ".join(quote_identifier(name) for name in column_names)
    if primary_key_columns:
        order_list = ", ".join(
            quote_identifier(name) for name in primary_key_columns
        )
        order_description = {
            "mode": "primary_key",
            "columns": primary_key_columns,
        }
    else:
        order_list = "rowid"
        order_description = {"mode": "rowid", "columns": ["rowid"]}
    query = (
        f"SELECT {select_list} FROM {quoted} "
        f"ORDER BY {order_list}"
    )
    return {
        "schema": schema,
        "column_names": column_names,
        "trigger_column": trigger_column,
        "trigger_column_index": column_names.index(trigger_column),
        "primary_key_columns": primary_key_columns,
        "order_description": order_description,
        "query": query,
    }


def source_row_count(
    connection: sqlite3.Connection,
    table_name: str,
) -> int:
    quoted = quote_identifier(table_name)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    except sqlite3.Error as exc:
        raise SorterError(
            "SOURCE_TABLE_ERROR",
            "unable to count source rows",
            details={"table": table_name, "error": str(exc)},
        ) from exc
