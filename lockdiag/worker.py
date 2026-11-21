"""Multiprocessing operation probes.

Workers intentionally perform only read-only plan/data probing.  Lock
arbitration remains transactional in the coordinator so that the virtual
timeline is deterministic while SQLite planning still runs in parallel.
"""

from __future__ import annotations

import json
import multiprocessing
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

_INDEX_RE = re.compile(r"USING\s+(?:COVERING\s+)?INDEX\s+([A-Za-z0-9_]+)")
_ROWID_RE = re.compile(r"USING\s+INTEGER\s+PRIMARY\s+KEY")


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_for_operation(op: dict[str, Any]) -> tuple[str, list[Any]]:
    kind = op["op"]
    table = quote_identifier(op["table"])
    if kind == "read":
        column = quote_identifier(op["column"])
        return (
            f"SELECT rowid, * FROM {table} WHERE {column} = ? ORDER BY rowid LIMIT 5",
            [op.get("value")],
        )
    if kind == "scan":
        column = quote_identifier(op["column"])
        return (
            f"SELECT rowid, * FROM {table} WHERE {column} >= ? AND {column} <= ? "
            f"ORDER BY {column}, rowid LIMIT 5",
            [op["start"], op["end"]],
        )
    if kind == "write":
        return f"SELECT rowid, * FROM {table} WHERE rowid = ?", [op["row_id"]]
    return "", []


def _classify_plan(detail_rows: list[tuple], table: str) -> str:
    details = " | ".join(str(row[3]) for row in detail_rows)
    match = _INDEX_RE.search(details)
    if match:
        return f"INDEX:{match.group(1)}"
    if _ROWID_RE.search(details) or "PRIMARY KEY" in details:
        return f"ROWID:{table}"
    if "SCAN " in details:
        return f"TABLE_SCAN:{table}"
    return f"UNKNOWN_PLAN:{details or 'empty'}"


def probe_operation(task: dict[str, Any]) -> dict[str, Any]:
    """Probe one operation in a worker process."""

    db_path = Path(task["db_path"])
    op = task["operation"]
    pid = multiprocessing.current_process().pid
    kind = op["op"]
    if kind in {"lock_decl", "rollback"}:
        index_path = "NO_INDEX:lock_decl" if kind == "lock_decl" else "NO_INDEX:rollback"
        return {
            "tx_id": op["tx_id"],
            "op_seq": op["op_seq"],
            "op_version": op["op_version"],
            "worker_pid": pid,
            "index_path": index_path,
            "sql": "",
            "rows": [],
            "plan": [],
        }

    sql, params = _sql_for_operation(op)
    uri = "file:" + quote(db_path.resolve().as_posix(), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only=ON")
        plan_rows = connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        index_path = _classify_plan(plan_rows, op["table"])
        rows = connection.execute(sql, params).fetchall()
        preview = [list(row) for row in rows[:5]]
        if kind == "write":
            index_path = f"ROWID:{op['table']}"
        return {
            "tx_id": op["tx_id"],
            "op_seq": op["op_seq"],
            "op_version": op["op_version"],
            "worker_pid": pid,
            "index_path": index_path,
            "sql": sql,
            "rows": preview,
            "plan": [list(row) for row in plan_rows],
        }
    finally:
        connection.close()


def probe_all(db_path: str | Path, tasks: list[dict[str, Any]], workers: int) -> list[dict]:
    """Run probes in a process pool and retain deterministic task order."""

    payload = [{"db_path": str(db_path), "operation": task} for task in tasks]
    if workers <= 1 or len(payload) <= 1:
        return [probe_operation(item) for item in payload]
    context = multiprocessing.get_context("spawn")
    with context.Pool(processes=min(workers, len(payload))) as pool:
        results = pool.map(probe_operation, payload, chunksize=1)
    return sorted(results, key=lambda item: (item["tx_id"], item["op_seq"]))


def result_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
