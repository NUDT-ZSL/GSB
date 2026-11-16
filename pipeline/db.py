from __future__ import annotations

import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT,
    schema_version TEXT,
    business_id TEXT,
    status_code INTEGER,
    body TEXT
);
CREATE TABLE IF NOT EXISTS normalized (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    source TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    business_id TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, schema_version, business_id)
);
CREATE TABLE IF NOT EXISTS task_items (
    task_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    source TEXT,
    schema_version TEXT,
    business_id TEXT,
    status TEXT NOT NULL,
    error_class TEXT,
    PRIMARY KEY (task_id, item_key)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    task_id TEXT PRIMARY KEY,
    page_cursor_json TEXT,
    confirmed_keys_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    page_url TEXT NOT NULL,
    observation INTEGER NOT NULL,
    body TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    # --- tasks ---------------------------------------------------------
    def create_task(self, task_id: str) -> None:
        self._execute(
            "INSERT OR IGNORE INTO tasks (task_id, status) VALUES (?, 'running')",
            (task_id,),
        )
        self._execute("UPDATE tasks SET status = 'running' WHERE task_id = ?", (task_id,))

    def finish_task(self, task_id: str, status: str, summary: dict) -> None:
        self._execute(
            "UPDATE tasks SET status = ?, summary_json = ? WHERE task_id = ?",
            (status, json.dumps(summary, sort_keys=True), task_id),
        )

    def get_task(self, task_id: str) -> dict | None:
        row = self._execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return {
            "task_id": row["task_id"],
            "status": row["status"],
            "summary": json.loads(row["summary_json"]),
        }

    def list_tasks(self) -> list[dict]:
        rows = self._execute("SELECT * FROM tasks ORDER BY task_id").fetchall()
        return [
            {"task_id": r["task_id"], "status": r["status"], "summary": json.loads(r["summary_json"])}
            for r in rows
        ]

    # --- raw responses ---------------------------------------------------
    def record_raw(self, task_id, url, source, version, biz, status_code, body) -> None:
        self._execute(
            "INSERT INTO raw_responses (task_id, url, source, schema_version, business_id, status_code, body)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, url, source, version, biz, status_code, body),
        )

    # --- normalized ------------------------------------------------------
    def insert_normalized(self, task_id, source, version, biz, fields, evidence) -> None:
        """Strict insert. Raises sqlite3.IntegrityError on duplicate key."""
        self._execute(
            "INSERT INTO normalized (task_id, source, schema_version, business_id, fields_json, status, evidence_json)"
            " VALUES (?, ?, ?, ?, ?, 'confirmed', ?)",
            (task_id, source, str(version), biz, json.dumps(fields, sort_keys=True), json.dumps(evidence, sort_keys=True)),
        )

    def upsert_unresolved(self, task_id, source, version_label, biz, raw) -> None:
        self._execute(
            "INSERT OR REPLACE INTO normalized (task_id, source, schema_version, business_id, fields_json, status, evidence_json)"
            " VALUES (?, ?, ?, ?, ?, 'unresolved', ?)",
            (task_id, source or "undeclared", version_label, biz, json.dumps(raw, sort_keys=True), "{}"),
        )

    def normalized_for_biz(self, biz: str) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM normalized WHERE business_id = ? AND status = 'confirmed'"
            " ORDER BY source, schema_version",
            (biz,),
        ).fetchall()
        return [
            {
                "source": r["source"],
                "schema_version": r["schema_version"],
                "business_id": r["business_id"],
                "fields": json.loads(r["fields_json"]),
                "evidence": json.loads(r["evidence_json"]),
            }
            for r in rows
        ]

    def normalized_stats(self) -> dict:
        total = self._execute("SELECT COUNT(*) c FROM normalized").fetchone()["c"]
        keys = self._execute(
            "SELECT COUNT(*) c FROM (SELECT source, schema_version, business_id FROM normalized"
            " GROUP BY source, schema_version, business_id)"
        ).fetchone()["c"]
        dupes = self._execute(
            "SELECT COUNT(*) c FROM (SELECT source, schema_version, business_id FROM normalized"
            " GROUP BY source, schema_version, business_id HAVING COUNT(*) > 1)"
        ).fetchone()["c"]
        return {"rows": total, "unique_keys": keys, "duplicate_key_groups": dupes}

    def count_normalized_for_task(self, task_id: str) -> int:
        return self._execute(
            "SELECT COUNT(*) c FROM normalized WHERE task_id = ? AND status = 'confirmed'",
            (task_id,),
        ).fetchone()["c"]

    def evidence_consistent(self) -> bool:
        rows = self._execute(
            "SELECT source, schema_version, evidence_json FROM normalized WHERE status = 'confirmed'"
        ).fetchall()
        for r in rows:
            evidence = json.loads(r["evidence_json"])
            if evidence.get("source") != r["source"]:
                return False
            if str(evidence.get("schema_version")) != str(r["schema_version"]):
                return False
        return True

    # --- task items ------------------------------------------------------
    def record_task_item(self, task_id, key, source, version, biz, status, error_class=None) -> None:
        self._execute(
            "INSERT INTO task_items (task_id, item_key, source, schema_version, business_id, status, error_class)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(task_id, item_key) DO UPDATE SET status = excluded.status,"
            " error_class = excluded.error_class, schema_version = excluded.schema_version",
            (task_id, key, source, version, biz, status, error_class),
        )

    def clear_task_item_variants(self, task_id, source, biz, keep_key) -> None:
        self._execute(
            "DELETE FROM task_items WHERE task_id = ? AND source = ? AND business_id = ? AND item_key != ?",
            (task_id, source, biz, keep_key),
        )

    def task_items(self, task_id: str) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM task_items WHERE task_id = ? ORDER BY item_key", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- checkpoints -----------------------------------------------------
    def save_checkpoint(self, task_id: str, cursor: dict | None, confirmed_keys: list[str]) -> None:
        self._execute(
            "INSERT INTO checkpoints (task_id, page_cursor_json, confirmed_keys_json) VALUES (?, ?, ?)"
            " ON CONFLICT(task_id) DO UPDATE SET page_cursor_json = excluded.page_cursor_json,"
            " confirmed_keys_json = excluded.confirmed_keys_json",
            (task_id, json.dumps(cursor), json.dumps(sorted(confirmed_keys))),
        )

    def load_checkpoint(self, task_id: str) -> tuple[dict | None, list[str]]:
        row = self._execute(
            "SELECT page_cursor_json, confirmed_keys_json FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None, []
        cursor = json.loads(row["page_cursor_json"]) if row["page_cursor_json"] else None
        return cursor, json.loads(row["confirmed_keys_json"])

    # --- snapshots -------------------------------------------------------
    def save_snapshot(self, task_id: str, page_url: str, observation: int, body: str) -> None:
        self._execute(
            "INSERT INTO snapshots (task_id, page_url, observation, body) VALUES (?, ?, ?, ?)",
            (task_id, page_url, observation, body),
        )

    def snapshots(self, task_id: str) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM snapshots WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]
