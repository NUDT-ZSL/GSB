from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def reset_database(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    conn = connect(path)
    initialize_schema(conn)
    return conn


def insert_many(
    conn: sqlite3.Connection, table: str, columns: list[str], rows: Iterable[Iterable[Any]]
) -> None:
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
    conn.executemany(sql, rows)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def table_count(conn: sqlite3.Connection, table: str) -> int:
    if table not in {
        "regions",
        "bands",
        "rangers",
        "terminals",
        "routes",
        "base_stations",
        "patrol_logs",
        "queries",
    }:
        raise ValueError(f"unsupported table: {table}")
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
