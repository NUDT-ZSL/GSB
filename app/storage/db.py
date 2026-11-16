"""SQLite persistence: raw responses, versioned normalized records, edit
revisions, edit conflicts, failures and resumable task checkpoints.

Version semantics (shared by crawler and editor):
  * a crawled record is keyed by business id (item_key) + schemaVersion and
    stored as rev 0 with source='crawl' (idempotent insert);
  * a manual edit appends rev = max(rev)+1 with source='edit';
  * a re-crawl never overwrites existing revisions: if edits exist and the
    crawled payload differs from the latest revision, a pending conflict row
    is written with code E_EDIT_CONFLICT keeping both sources.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

E_EDIT_CONFLICT = "E_EDIT_CONFLICT"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    cursor_page INTEGER,
    completed_keys TEXT NOT NULL DEFAULT '[]',
    scheduler_state TEXT NOT NULL DEFAULT '{}',
    stats TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    url TEXT NOT NULL,
    status INTEGER NOT NULL,
    body TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    item_key TEXT NOT NULL,
    rev INTEGER NOT NULL,
    source TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    normalized_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_key, rev)
);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    code TEXT NOT NULL,
    crawl_normalized_json TEXT NOT NULL,
    crawl_schema_version INTEGER NOT NULL,
    crawl_evidence_json TEXT NOT NULL,
    existing_rev INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    category TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_stats() -> dict:
    return {
        "entries_inserted": 0,
        "entries_identical": 0,
        "entries_updated": 0,
        "unresolved": 0,
        "conflicts": 0,
        "retries": 0,
        "session_replays": 0,
        "failures": {},
    }


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------------- task checkpoints ----------------

    def create_task(self, task_id: str) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO tasks(task_id, status, cursor_page, completed_keys,"
                " scheduler_state, stats, created_at, updated_at)"
                " VALUES (?, 'running', 1, '[]', '{}', ?, ?, ?)",
                (task_id, json.dumps(initial_stats()), now, now),
            )

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return {
            "task_id": row["task_id"],
            "status": row["status"],
            "cursor_page": row["cursor_page"],
            "completed_keys": json.loads(row["completed_keys"]),
            "scheduler_state": json.loads(row["scheduler_state"]),
            "stats": json.loads(row["stats"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def save_checkpoint(self, task_id: str, *, status=None, cursor_page=None,
                        completed_keys=None, scheduler_state=None, stats=None) -> None:
        current = self.get_task(task_id)
        if current is None:
            raise KeyError(f"unknown task {task_id}")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET status=?, cursor_page=?, completed_keys=?,"
                " scheduler_state=?, stats=?, updated_at=? WHERE task_id=?",
                (
                    status if status is not None else current["status"],
                    cursor_page if cursor_page is not None else current["cursor_page"],
                    json.dumps(completed_keys if completed_keys is not None else current["completed_keys"]),
                    json.dumps(scheduler_state if scheduler_state is not None else current["scheduler_state"]),
                    json.dumps(stats if stats is not None else current["stats"]),
                    _now(),
                    task_id,
                ),
            )

    def list_tasks(self) -> list:
        with self._lock:
            rows = self._conn.execute("SELECT task_id FROM tasks ORDER BY created_at").fetchall()
        return [self.get_task(r["task_id"]) for r in rows]

    # ---------------- raw responses ----------------

    def record_raw(self, task_id: str, url: str, status: int, body: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO raw_responses(task_id, url, status, body, fetched_at)"
                " VALUES (?,?,?,?,?)",
                (task_id, url, status, body, _now()),
            )

    def count_raw(self, task_id: str | None = None) -> int:
        with self._lock:
            if task_id:
                row = self._conn.execute(
                    "SELECT COUNT(*) c FROM raw_responses WHERE task_id=?", (task_id,)).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) c FROM raw_responses").fetchone()
        return row["c"]

    # ---------------- versions / crawl upsert ----------------

    def versions_of(self, item_key: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM versions WHERE item_key=? ORDER BY rev", (item_key,)).fetchall()
        return [self._version_dict(r) for r in rows]

    def latest_version(self, item_key: str) -> dict | None:
        versions = self.versions_of(item_key)
        return versions[-1] if versions else None

    @staticmethod
    def _version_dict(row) -> dict:
        return {
            "item_key": row["item_key"],
            "rev": row["rev"],
            "source": row["source"],
            "schema_version": row["schema_version"],
            "normalized": json.loads(row["normalized_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "created_at": row["created_at"],
        }

    def store_crawled(self, task_id: str, item_key: str, schema_version: int,
                      normalized: dict, evidence: dict) -> str:
        """Idempotent crawl upsert. Returns inserted|identical|updated|conflict."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM versions WHERE item_key=? ORDER BY rev", (item_key,)).fetchall()
            now = _now()
            if not rows:
                self._conn.execute(
                    "INSERT INTO versions(item_key, rev, source, schema_version,"
                    " normalized_json, evidence_json, created_at) VALUES (?,0,'crawl',?,?,?,?)",
                    (item_key, schema_version, json.dumps(normalized, ensure_ascii=False),
                     json.dumps(evidence, ensure_ascii=False), now),
                )
                return "inserted"
            latest = rows[-1]
            latest_norm = json.loads(latest["normalized_json"])
            has_edit = any(r["source"] == "edit" for r in rows)
            if latest_norm == normalized:
                return "identical"
            if not has_edit:
                # no manual edits to protect: refresh the crawl revision in place
                self._conn.execute(
                    "UPDATE versions SET schema_version=?, normalized_json=?, evidence_json=?,"
                    " created_at=? WHERE item_key=? AND rev=?",
                    (schema_version, json.dumps(normalized, ensure_ascii=False),
                     json.dumps(evidence, ensure_ascii=False), now, item_key, latest["rev"]),
                )
                return "updated"
            # manual edits exist and crawl payload differs -> pending adjudication
            self._conn.execute(
                "INSERT INTO conflicts(task_id, item_key, code, crawl_normalized_json,"
                " crawl_schema_version, crawl_evidence_json, existing_rev, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,'pending',?)",
                (task_id, item_key, E_EDIT_CONFLICT,
                 json.dumps(normalized, ensure_ascii=False), schema_version,
                 json.dumps(evidence, ensure_ascii=False), latest["rev"], now),
            )
            return "conflict"

    def store_edit(self, item_key: str, updates: dict, allowed_fields,
                   editor: str = "operator") -> int:
        """Append a new revision with the edited fields. Returns the new rev."""
        unknown = set(updates) - set(allowed_fields)
        if unknown:
            raise ValueError(f"fields outside shared normalized set: {sorted(unknown)}")
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM versions WHERE item_key=? ORDER BY rev", (item_key,)).fetchall()
            if not rows:
                raise KeyError(f"cannot edit unknown record {item_key}")
            latest = rows[-1]
            base = json.loads(latest["normalized_json"])
            merged = dict(base)
            merged.update(updates)
            new_rev = latest["rev"] + 1
            evidence = {
                "schema_version": latest["schema_version"],
                "parser": "manual_edit",
                "edited_fields": sorted(updates),
                "editor": editor,
                "base_rev": latest["rev"],
            }
            self._conn.execute(
                "INSERT INTO versions(item_key, rev, source, schema_version, normalized_json,"
                " evidence_json, created_at) VALUES (?,?, 'edit', ?,?,?,?)",
                (item_key, new_rev, latest["schema_version"],
                 json.dumps(merged, ensure_ascii=False),
                 json.dumps(evidence, ensure_ascii=False), _now()),
            )
            return new_rev

    # ---------------- conflicts ----------------

    def list_conflicts(self, status: str | None = None) -> list:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM conflicts WHERE status=? ORDER BY id", (status,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM conflicts ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def resolve_conflict(self, conflict_id: int, action: str) -> None:
        """action: keep_edit | accept_crawl"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown conflict {conflict_id}")
            if action == "accept_crawl":
                latest = self._conn.execute(
                    "SELECT MAX(rev) m FROM versions WHERE item_key=?",
                    (row["item_key"],)).fetchone()
                self._conn.execute(
                    "INSERT INTO versions(item_key, rev, source, schema_version,"
                    " normalized_json, evidence_json, created_at) VALUES (?,?, 'crawl',?,?,?,?)",
                    (row["item_key"], latest["m"] + 1, row["crawl_schema_version"],
                     row["crawl_normalized_json"], row["crawl_evidence_json"], _now()),
                )
            self._conn.execute(
                "UPDATE conflicts SET status=? WHERE id=?",
                (f"resolved:{action}", conflict_id),
            )

    # ---------------- failures ----------------

    def add_failure(self, task_id: str, item_key: str, category: str,
                    code: str, detail: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO failures(task_id, item_key, category, code, detail, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (task_id, item_key, category, code, detail, _now()),
            )

    def list_failures(self, task_id: str | None = None) -> list:
        with self._lock:
            if task_id:
                rows = self._conn.execute(
                    "SELECT * FROM failures WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM failures ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ---------------- queries for the console ----------------

    def list_records(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT v.* FROM versions v JOIN (SELECT item_key, MAX(rev) m FROM versions"
                " GROUP BY item_key) t ON v.item_key=t.item_key AND v.rev=t.m"
                " ORDER BY v.item_key").fetchall()
        return [self._version_dict(r) for r in rows]
