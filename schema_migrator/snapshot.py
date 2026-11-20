from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import error
from .model import schema_digest, view_sql_body
from .sqlparse import normalize_sql_text, parse_query_dependencies, strip_create_view_prefix
from .typesys import parse_type, python_sql_value, sql_default_literal
from .util import canonical_json, canonical_rows, digest_json, sha256_text


INTERNAL_LOG_TABLE = "__schema_diag_log"
APPLICATION_ID = 0x53444731


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect_snapshot(path: Path, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(str(path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _column_sql(column: Dict[str, Any]) -> str:
    type_info = parse_type(column["type"])
    parts = [quote_ident(column["name"]), type_info.canonical]
    if column.get("primary_key"):
        parts.append("PRIMARY KEY")
    if column.get("not_null"):
        parts.append("NOT NULL")
    if column.get("unique"):
        parts.append("UNIQUE")
    if column.get("has_default"):
        parts.append("DEFAULT")
        parts.append(sql_default_literal(type_info, column.get("default")))
    return " ".join(parts)


def table_create_sql(table: Dict[str, Any]) -> str:
    column_defs = [_column_sql(column) for column in table["columns"]]
    pk_columns = [column["name"] for column in table["columns"] if column.get("primary_key")]
    if len(pk_columns) > 1:
        column_defs.append("PRIMARY KEY (" + ", ".join(quote_ident(name) for name in pk_columns) + ")")
    for fk in table.get("foreign_keys", []):
        columns = ", ".join(quote_ident(name) for name in fk["columns"])
        ref_columns = ", ".join(quote_ident(name) for name in fk["ref_columns"])
        clause = (
            f"FOREIGN KEY ({columns}) REFERENCES {quote_ident(fk['ref_table'])} "
            f"({ref_columns}) ON DELETE {fk['on_delete']}"
        )
        column_defs.append(clause)
    return (
        f"CREATE TABLE {quote_ident(table['name'])} (\n  "
        + ",\n  ".join(column_defs)
        + "\n)"
    )


def index_create_sql(index: Dict[str, Any]) -> str:
    unique = "UNIQUE " if index.get("unique") else ""
    columns = ", ".join(quote_ident(column) for column in index["columns"])
    return (
        f"CREATE {unique}INDEX {quote_ident(index['name'])} "
        f"ON {quote_ident(index['table'])} ({columns})"
    )


def view_create_sql(view: Dict[str, Any]) -> str:
    return f"CREATE VIEW {quote_ident(view['name'])} AS {view['sql']}"


def trigger_create_sql(trigger: Dict[str, Any]) -> str:
    return trigger["sql"]


def ordered_views(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    views = {view["name"]: view for view in schema.get("views", [])}
    dependencies: Dict[str, set] = {}
    for name, view in views.items():
        refs = set(view.get("references", []))
        try:
            parsed = parse_query_dependencies(view["sql"], schema, f"view:{name}")
            refs.update(f"view:{item}" for item in parsed["views"])
        except Exception:
            pass
        dependencies[name] = {
            ref.split(":", 1)[1]
            for ref in refs
            if ref.startswith("view:") and ref.split(":", 1)[1] in views
        }
    ordered: List[Dict[str, Any]] = []
    remaining = set(views)
    while remaining:
        ready = sorted(
            name
            for name in remaining
            if not (dependencies.get(name, set()) & remaining)
        )
        if not ready:
            raise error(
                "E_DEPENDENCY_CYCLE",
                "view dependency cycle cannot be materialized",
                views=sorted(remaining),
            )
        for name in ready:
            ordered.append(views[name])
            remaining.remove(name)
    return ordered


def _insert_rows(conn: sqlite3.Connection, table: Dict[str, Any]) -> None:
    if not table.get("rows"):
        return
    column_map = {column["name"]: column for column in table["columns"]}
    names = list(column_map)
    placeholders = ", ".join("?" for _ in names)
    sql = (
        f"INSERT INTO {quote_ident(table['name'])} "
        f"({', '.join(quote_ident(name) for name in names)}) VALUES ({placeholders})"
    )
    prepared_rows: List[List[Any]] = []
    for row_index, row in enumerate(table["rows"]):
        if not isinstance(row, dict):
            raise error(
                "E_SEED",
                "seed row must be an object keyed by column",
                table=table["name"],
                row=row_index,
            )
        unknown = sorted(set(row) - set(column_map))
        if unknown:
            raise error(
                "E_SEED",
                "seed row contains unknown columns",
                table=table["name"],
                row=row_index,
                columns=unknown,
            )
        values = []
        for name in names:
            column = column_map[name]
            type_info = parse_type(column["type"])
            if name in row:
                value = row[name]
            elif column.get("has_default"):
                value = column.get("default")
            elif column.get("not_null"):
                raise error(
                    "E_SEED",
                    "seed row is missing a required column",
                    table=table["name"],
                    row=row_index,
                    column=name,
                )
            else:
                value = None
            values.append(python_sql_value(type_info, value))
        prepared_rows.append(values)
    conn.executemany(sql, prepared_rows)


def create_initial_snapshot(schema: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".building")
    try:
        temp_path.unlink()
    except FileNotFoundError:
        pass
    conn = sqlite3.connect(str(temp_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in schema.get("tables", []):
            conn.execute(table_create_sql(table))
        for index in schema.get("indexes", []):
            conn.execute(index_create_sql(index))
        for view in ordered_views(schema):
            conn.execute(view_create_sql(view))
        for trigger in schema.get("triggers", []):
            conn.execute(trigger_create_sql(trigger))
        for table in schema.get("tables", []):
            _insert_rows(conn, table)
        conn.execute(f"PRAGMA user_version={int(schema['version'])}")
        conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise error("E_FOREIGN_KEY_VIOLATION", "seed data violates foreign keys", violations=violations)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise error("E_INTEGRITY", "new snapshot failed integrity_check", result=integrity)
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        conn.close()
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        if conn:
            conn.close()
    os.replace(temp_path, path)


def _canonical_sql_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return {"$float": repr(value)}
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    return str(value)


def table_data(conn: sqlite3.Connection, table: Dict[str, Any]) -> List[List[Any]]:
    columns = [column["name"] for column in table["columns"]]
    sql = (
        f"SELECT {', '.join(quote_ident(name) for name in columns)} "
        f"FROM {quote_ident(table['name'])} ORDER BY rowid"
    )
    return [[_canonical_sql_value(value) for value in row] for row in conn.execute(sql)]


def data_digest(conn: sqlite3.Connection, schema: Dict[str, Any]) -> str:
    payload = {
        "tables": [
            {"name": table["name"], "rows": table_data(conn, table)}
            for table in sorted(schema.get("tables", []), key=lambda item: item["name"])
        ]
    }
    return digest_json(payload)


def calculate_snapshot_digests(path: Path, schema: Dict[str, Any]) -> Dict[str, str]:
    conn = connect_snapshot(path, read_only=True)
    try:
        schema_hash = schema_digest(schema)
        data_hash = data_digest(conn, schema)
    finally:
        conn.close()
    content_hash = sha256_text(f"{schema_hash}:{data_hash}")
    return {
        "schema_digest": schema_hash,
        "data_digest": data_hash,
        "content_digest": content_hash,
    }


def _normalize_default_literal(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return " ".join(value.strip().split())


def introspect_snapshot(conn: sqlite3.Connection, version: int) -> Dict[str, Any]:
    table_rows = conn.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
          AND name <> ?
        ORDER BY name
        """,
        (INTERNAL_LOG_TABLE,),
    ).fetchall()
    tables: List[Dict[str, Any]] = []
    for table_name, _table_sql in table_rows:
        info_rows = conn.execute(f"PRAGMA table_info({quote_ident(table_name)})").fetchall()
        columns = []
        for _cid, name, declared_type, not_null, default_value, pk in info_rows:
            type_info = parse_type(declared_type or "TEXT")
            column = {
                "name": name,
                "type": type_info.canonical,
                "not_null": bool(not_null),
                "primary_key": bool(pk),
                "unique": False,
                "has_default": default_value is not None,
            }
            if default_value is not None:
                column["default_sql"] = _normalize_default_literal(default_value)
            columns.append(column)
        fk_rows = conn.execute(f"PRAGMA foreign_key_list({quote_ident(table_name)})").fetchall()
        fk_groups: Dict[int, Dict[str, Any]] = {}
        for fk in sorted(fk_rows, key=lambda row: (row[0], row[1])):
            fk_id, seq, ref_table, source_col, target_col, on_update, on_delete, _match = fk
            item = fk_groups.setdefault(
                fk_id,
                {
                    "columns": [],
                    "ref_table": ref_table,
                    "ref_columns": [],
                    "on_delete": str(on_delete).upper().replace("_", " "),
                },
            )
            item["columns"].append(source_col)
            item["ref_columns"].append(target_col)
        tables.append(
            {
                "name": table_name,
                "columns": columns,
                "foreign_keys": [fk_groups[key] for key in sorted(fk_groups)],
            }
        )

    indexes: List[Dict[str, Any]] = []
    index_rows = conn.execute(
        """
        SELECT name, tbl_name, sql
        FROM sqlite_master
        WHERE type = 'index' AND sql IS NOT NULL
        ORDER BY name
        """
    ).fetchall()
    for name, table_name, _sql in index_rows:
        info = conn.execute(f"PRAGMA index_info({quote_ident(name)})").fetchall()
        unique_rows = conn.execute(f"PRAGMA index_list({quote_ident(table_name)})").fetchall()
        unique = False
        for item in unique_rows:
            if item[1] == name:
                unique = bool(item[2])
                break
        indexes.append(
            {
                "name": name,
                "table": table_name,
                "columns": [row[2] for row in sorted(info, key=lambda value: value[0])],
                "unique": unique,
            }
        )

    views: List[Dict[str, Any]] = []
    view_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'view' ORDER BY name"
    ).fetchall()
    for name, sql in view_rows:
        views.append({"name": name, "sql": strip_create_view_prefix(sql, name), "references": []})

    triggers: List[Dict[str, Any]] = []
    trigger_rows = conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
    ).fetchall()
    for name, table_name, sql in trigger_rows:
        triggers.append({"name": name, "table": table_name, "sql": sql, "references": []})

    return {
        "version": version,
        "database": "commerce_demo",
        "tables": tables,
        "indexes": indexes,
        "views": views,
        "triggers": triggers,
    }


def _expected_default_sql(column: Dict[str, Any]) -> Optional[str]:
    if not column.get("has_default"):
        return None
    type_info = parse_type(column["type"])
    return _normalize_default_literal(sql_default_literal(type_info, column.get("default")))


def verify_snapshot(schema: Dict[str, Any], snapshot_path: Path) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    if not snapshot_path.exists():
        return {
            "ok": False,
            "issues": [{"code": "E_SNAPSHOT_MISSING", "path": str(snapshot_path)}],
        }
    conn = connect_snapshot(snapshot_path, read_only=True)
    try:
        actual = introspect_snapshot(conn, int(schema["version"]))
        expected_tables = {table["name"]: table for table in schema.get("tables", [])}
        actual_tables = {table["name"]: table for table in actual["tables"]}
        for name in sorted(set(expected_tables) | set(actual_tables)):
            if name not in expected_tables:
                issues.append({"code": "E_EXTRA_TABLE", "table": name})
                continue
            if name not in actual_tables:
                issues.append({"code": "E_MISSING_TABLE", "table": name})
                continue
            expected_columns = {column["name"]: column for column in expected_tables[name]["columns"]}
            actual_columns = {column["name"]: column for column in actual_tables[name]["columns"]}
            for column_name in sorted(set(expected_columns) | set(actual_columns)):
                if column_name not in expected_columns:
                    issues.append({"code": "E_EXTRA_COLUMN", "table": name, "column": column_name})
                    continue
                if column_name not in actual_columns:
                    issues.append({"code": "E_MISSING_COLUMN", "table": name, "column": column_name})
                    continue
                expected_column = expected_columns[column_name]
                actual_column = actual_columns[column_name]
                for key in ("type", "not_null", "primary_key"):
                    if expected_column.get(key) != actual_column.get(key):
                        issues.append(
                            {
                                "code": "E_COLUMN_MISMATCH",
                                "table": name,
                                "column": column_name,
                                "field": key,
                                "expected": expected_column.get(key),
                                "actual": actual_column.get(key),
                            }
                        )
                if _expected_default_sql(expected_column) != actual_column.get("default_sql"):
                    issues.append(
                        {
                            "code": "E_DEFAULT_MISMATCH",
                            "table": name,
                            "column": column_name,
                            "expected": _expected_default_sql(expected_column),
                            "actual": actual_column.get("default_sql"),
                        }
                    )
            expected_fks = [
                {
                    "columns": fk["columns"],
                    "ref_table": fk["ref_table"],
                    "ref_columns": fk["ref_columns"],
                    "on_delete": fk["on_delete"],
                }
                for fk in expected_tables[name].get("foreign_keys", [])
            ]
            if sorted(expected_fks, key=canonical_json) != sorted(
                actual_tables[name].get("foreign_keys", []), key=canonical_json
            ):
                issues.append(
                    {
                        "code": "E_FOREIGN_KEY_MISMATCH",
                        "table": name,
                        "expected": expected_fks,
                        "actual": actual_tables[name].get("foreign_keys", []),
                    }
                )

        expected_indexes = {item["name"]: item for item in schema.get("indexes", [])}
        actual_indexes = {item["name"]: item for item in actual["indexes"]}
        for name in sorted(set(expected_indexes) | set(actual_indexes)):
            if expected_indexes.get(name) != actual_indexes.get(name):
                issues.append(
                    {
                        "code": "E_INDEX_MISMATCH",
                        "index": name,
                        "expected": expected_indexes.get(name),
                        "actual": actual_indexes.get(name),
                    }
                )

        expected_views = {item["name"]: item for item in schema.get("views", [])}
        actual_views = {item["name"]: item for item in actual["views"]}
        for name in sorted(set(expected_views) | set(actual_views)):
            expected = expected_views.get(name)
            found = actual_views.get(name)
            if expected is None or found is None:
                issues.append(
                    {
                        "code": "E_VIEW_MISMATCH",
                        "view": name,
                        "expected": expected is not None,
                        "actual": found is not None,
                    }
                )
                continue
            if normalize_sql_text(view_sql_body(expected)) != normalize_sql_text(
                strip_create_view_prefix(found["sql"], name)
            ):
                issues.append(
                    {
                        "code": "E_VIEW_SQL_MISMATCH",
                        "view": name,
                        "expected": view_sql_body(expected),
                        "actual": found["sql"],
                    }
                )

        expected_triggers = {item["name"]: item for item in schema.get("triggers", [])}
        actual_triggers = {item["name"]: item for item in actual["triggers"]}
        for name in sorted(set(expected_triggers) | set(actual_triggers)):
            expected = expected_triggers.get(name)
            found = actual_triggers.get(name)
            if expected is None or found is None:
                issues.append(
                    {
                        "code": "E_TRIGGER_MISMATCH",
                        "trigger": name,
                        "expected": expected is not None,
                        "actual": found is not None,
                    }
                )
                continue
            if expected["table"] != found["table"] or normalize_sql_text(
                expected["sql"]
            ) != normalize_sql_text(found["sql"]):
                issues.append(
                    {
                        "code": "E_TRIGGER_SQL_MISMATCH",
                        "trigger": name,
                        "expected_table": expected["table"],
                        "actual_table": found["table"],
                        "expected": expected["sql"],
                        "actual": found["sql"],
                    }
                )

        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if int(user_version) != int(schema["version"]):
            issues.append(
                {
                    "code": "E_USER_VERSION_MISMATCH",
                    "expected": int(schema["version"]),
                    "actual": int(user_version),
                }
            )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            issues.append({"code": "E_FOREIGN_KEY_VIOLATION", "violations": violations})
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            issues.append({"code": "E_INTEGRITY", "result": integrity})
        digests = {
            "schema_digest": schema_digest(schema),
            "data_digest": data_digest(conn, schema),
        }
        digests["content_digest"] = sha256_text(
            f"{digests['schema_digest']}:{digests['data_digest']}"
        )
    finally:
        conn.close()
    return {
        "ok": not issues,
        "issues": issues,
        "digests": digests,
        "actual_object_counts": {
            "tables": len(actual["tables"]),
            "indexes": len(actual["indexes"]),
            "views": len(actual["views"]),
            "triggers": len(actual["triggers"]),
        },
    }

