"""Read a SQLite sample database and build its structural model."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .canonical import canonical_json, digest
from .errors import EngineError


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def load_db_model(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        raise EngineError(
            "ERR_SAMPLE_DB_NOT_FOUND",
            "SQLite sample database does not exist",
            {"path": str(path)},
        )
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise EngineError(
            "ERR_SAMPLE_DB",
            "cannot open SQLite sample database",
            {"path": str(path), "error": str(exc)},
        ) from exc
    try:
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
        table_names = sorted(row["name"] for row in objects if row["type"] == "table")
        if not table_names:
            raise EngineError(
                "ERR_DB_SCHEMA",
                "sample database contains no user tables",
                {"path": str(path)},
            )
        tables: dict[str, Any] = {}
        for table_name in table_names:
            columns_info = connection.execute(
                f"PRAGMA table_info({quote_identifier(table_name)})"
            ).fetchall()
            columns = [
                {
                    "name": row["name"].lower(),
                    "declared_type": (row["type"] or "").upper(),
                    "not_null": bool(row["notnull"]),
                    "primary_key": int(row["pk"]),
                }
                for row in columns_info
            ]
            if not columns:
                raise EngineError(
                    "ERR_DB_SCHEMA",
                    "sample table has no columns",
                    {"table": table_name},
                )
            indexes_info = connection.execute(
                f"PRAGMA index_list({quote_identifier(table_name)})"
            ).fetchall()
            indexes: dict[str, Any] = {}
            for index_row in indexes_info:
                index_name = index_row["name"]
                index_columns = connection.execute(
                    f"PRAGMA index_info({quote_identifier(index_name)})"
                ).fetchall()
                indexes[index_name.lower()] = {
                    "columns": [
                        row["name"].lower()
                        for row in sorted(index_columns, key=lambda item: item["seqno"])
                        if row["name"]
                    ],
                    "unique": bool(index_row["unique"]),
                    "origin": index_row["origin"],
                }
            row_count = connection.execute(
                f"SELECT COUNT(*) AS count FROM {quote_identifier(table_name)}"
            ).fetchone()["count"]
            tables[table_name.lower()] = {
                "columns": columns,
                "indexes": dict(sorted(indexes.items())),
                "row_count": int(row_count),
            }
    except sqlite3.Error as exc:
        raise EngineError(
            "ERR_SAMPLE_DB",
            "cannot inspect SQLite sample database",
            {"path": str(path), "error": str(exc)},
        ) from exc
    finally:
        connection.close()
    model = {
        "schema_version": 1,
        "database": path.name,
        "tables": dict(sorted(tables.items())),
    }
    model["digest"] = digest(model)
    return model


def validate_stats_against_model(
    snapshot: dict[str, Any], model: dict[str, Any]
) -> None:
    model_tables = model["tables"]
    for table_name, table_stats in snapshot["tables"].items():
        if table_name not in model_tables:
            raise EngineError(
                "ERR_SNAPSHOT_SCHEMA",
                "statistics reference a table absent from the sample database",
                {"table": table_name},
            )
        model_columns = {
            column["name"] for column in model_tables[table_name]["columns"]
        }
        for column_name in table_stats.get("columns", {}):
            if column_name not in model_columns:
                raise EngineError(
                    "ERR_SNAPSHOT_SCHEMA",
                    "statistics reference a column absent from the sample database",
                    {"table": table_name, "column": column_name},
                )
        model_indexes = model_tables[table_name]["indexes"]
        for index_name, index_stats in table_stats.get("indexes", {}).items():
            if index_name not in model_indexes:
                raise EngineError(
                    "ERR_SNAPSHOT_SCHEMA",
                    "statistics reference an index absent from the sample database",
                    {"table": table_name, "index": index_name},
                )
            expected = model_indexes[index_name]["columns"]
            if index_stats.get("columns") and index_stats["columns"] != expected:
                raise EngineError(
                    "ERR_SNAPSHOT_SCHEMA",
                    "statistics index columns differ from the sample database",
                    {
                        "table": table_name,
                        "index": index_name,
                        "expected": expected,
                        "actual": index_stats["columns"],
                    },
                )
            for prefix in index_stats.get("prefix_selectivity", {}):
                prefix_columns = prefix.split(",")
                if prefix_columns != expected[: len(prefix_columns)]:
                    raise EngineError(
                        "ERR_SNAPSHOT_SCHEMA",
                        "index selectivity prefix does not match index columns",
                        {"table": table_name, "index": index_name, "prefix": prefix},
                    )


def model_json(model: dict[str, Any]) -> str:
    return canonical_json(model)
