"""Deterministic SQLite sample database builder."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import fail
from .model import DatabaseSpec
from .util import canonical_json, quote_identifier, sha256_text


def _sql_type(value: str) -> str:
    normalized = value.upper()
    if normalized in {"INTEGER", "INT", "REAL", "TEXT", "BLOB", "NUMERIC"}:
        return normalized
    if normalized in {"BOOL", "BOOLEAN"}:
        return "INTEGER"
    if normalized in {"STRING", "VARCHAR", "CHAR"}:
        return "TEXT"
    fail("E_BAD_SCRIPT", f"unsupported SQLite column type: {value}")


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return value


def build_sample_database(
    database: DatabaseSpec,
    path: Path,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".building")
    if temp.exists():
        temp.unlink()
    conn = sqlite3.connect(temp)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN")
        for table in database.tables:
            column_sql = ", ".join(
                f"{quote_identifier(column.name)} {_sql_type(column.type)}"
                for column in table.columns
            )
            conn.execute(f"CREATE TABLE {quote_identifier(table.name)} ({column_sql})")
            if table.rows:
                column_names = [column.name for column in table.columns]
                placeholders = ",".join("?" for _ in column_names)
                insert_sql = (
                    f"INSERT INTO {quote_identifier(table.name)} "
                    f"({','.join(quote_identifier(name) for name in column_names)}) "
                    f"VALUES ({placeholders})"
                )
                ordered_rows = sorted(
                    table.rows,
                    key=lambda row: json.dumps(
                        row.get(table.primary_key),
                        ensure_ascii=True,
                        sort_keys=True,
                        default=str,
                    ),
                )
                for row in ordered_rows:
                    conn.execute(
                        insert_sql,
                        tuple(_sqlite_value(row.get(name)) for name in column_names),
                    )
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            fail("E_SAMPLE_DB", f"integrity_check failed: {integrity}")
        counts = {
            table.name: conn.execute(
                f"SELECT COUNT(*) FROM {quote_identifier(table.name)}"
            ).fetchone()[0]
            for table in database.tables
        }
        logical_content: dict[str, Any] = {"tables": []}
        for table in database.tables:
            columns = [column.name for column in table.columns]
            rows = [
                dict(row)
                for row in conn.execute(
                    f"SELECT {','.join(quote_identifier(name) for name in columns)}"
                    f" FROM {quote_identifier(table.name)}"
                )
            ]
            rows.sort(
                key=lambda row: canonical_json(row.get(table.primary_key))
            )
            logical_content["tables"].append(
                {
                    "name": table.name,
                    "primary_key": table.primary_key,
                    "columns": columns,
                    "rows": rows,
                }
            )
        content_sha256 = sha256_text(canonical_json(logical_content))
    except sqlite3.Error as exc:
        fail("E_SAMPLE_DB", str(exc), str(path))
    finally:
        conn.close()
    path.unlink(missing_ok=True)
    temp.replace(path)
    return {
        "path": path.name,
        "table_counts": counts,
        "content_sha256": content_sha256,
        "integrity": "ok",
    }

