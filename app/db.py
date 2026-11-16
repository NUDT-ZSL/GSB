from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scenario TEXT NOT NULL,
    wave TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL,
    task_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    summary_json TEXT,
    error TEXT,
    resume_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS crawl_sessions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    archive_session_id TEXT,
    wave TEXT NOT NULL,
    state TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    completed_at TEXT,
    replay_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    task_id TEXT PRIMARY KEY,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    completed_count INTEGER NOT NULL DEFAULT 0,
    completed_session_ids_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_items (
    task_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    business_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    revision INTEGER NOT NULL,
    detail_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    business_code TEXT,
    result_kind TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    raw_response_id INTEGER,
    last_error TEXT,
    completed_at TEXT,
    PRIMARY KEY(task_id, item_key),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_items_status ON task_items(task_id, status);

CREATE TABLE IF NOT EXISTS task_completed_items (
    task_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY(task_id, item_key),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    session_id TEXT,
    item_key TEXT,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    headers_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    wave TEXT,
    request_seq INTEGER
);
CREATE INDEX IF NOT EXISTS idx_raw_task ON raw_responses(task_id);

CREATE TABLE IF NOT EXISTS record_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    revision INTEGER NOT NULL,
    raw_response_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT,
    field_hash TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(raw_response_id),
    FOREIGN KEY(raw_response_id) REFERENCES raw_responses(id)
);
CREATE INDEX IF NOT EXISTS idx_observations_business ON record_observations(business_id);

CREATE TABLE IF NOT EXISTS normalized_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    revision INTEGER NOT NULL,
    declared_schema_version TEXT,
    parser_version TEXT NOT NULL,
    unresolved INTEGER NOT NULL,
    fields_json TEXT NOT NULL,
    field_hash TEXT NOT NULL,
    schema_evidence_json TEXT NOT NULL,
    raw_response_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(business_id, schema_version, revision),
    FOREIGN KEY(raw_response_id) REFERENCES raw_responses(id)
);
CREATE INDEX IF NOT EXISTS idx_records_business ON normalized_records(business_id, revision);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    revision INTEGER NOT NULL,
    existing_record_id INTEGER NOT NULL,
    incoming_fields_json TEXT NOT NULL,
    incoming_hash TEXT NOT NULL,
    incoming_unresolved INTEGER NOT NULL,
    raw_response_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    UNIQUE(business_id, schema_version, revision, incoming_hash),
    FOREIGN KEY(existing_record_id) REFERENCES normalized_records(id),
    FOREIGN KEY(raw_response_id) REFERENCES raw_responses(id)
);
CREATE INDEX IF NOT EXISTS idx_conflicts_task ON conflicts(task_id);

CREATE TABLE IF NOT EXISTS version_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id TEXT NOT NULL,
    from_record_id INTEGER NOT NULL,
    to_record_id INTEGER NOT NULL,
    from_revision INTEGER NOT NULL,
    to_revision INTEGER NOT NULL,
    added_fields_json TEXT NOT NULL,
    missing_fields_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(business_id, from_record_id, to_record_id),
    FOREIGN KEY(from_record_id) REFERENCES normalized_records(id),
    FOREIGN KEY(to_record_id) REFERENCES normalized_records(id)
);
CREATE INDEX IF NOT EXISTS idx_edges_business ON version_edges(business_id);

CREATE TABLE IF NOT EXISTS render_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    url TEXT NOT NULL,
    container_present INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    stable_count INTEGER NOT NULL,
    body_sha256 TEXT,
    fragment_excerpt TEXT,
    accepted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_task_page ON render_snapshots(task_id, page_no);

CREATE TABLE IF NOT EXISTS request_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    session_id TEXT,
    item_key TEXT,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status_code INTEGER,
    duration_ms INTEGER NOT NULL,
    error_code TEXT,
    fetched_at TEXT NOT NULL,
    body_sha256 TEXT,
    request_seq INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_task ON request_events(task_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def field_hash(fields: dict[str, Any]) -> str:
    return sha256_text(canonical_json(fields))


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self.init_schema()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._init_lock:
            with self.connection() as conn:
                conn.executescript(SCHEMA)

    def _write(self, callback):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = callback(conn)
                conn.execute("COMMIT")
                return result
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def create_task(
        self,
        task_id: str,
        name: str,
        scenario: str,
        wave: str,
        config: dict[str, Any],
    ) -> None:
        now = utcnow()

        def tx(conn: sqlite3.Connection):
            conn.execute(
                "INSERT INTO tasks(id,name,scenario,wave,config_json,status,created_at) "
                "VALUES(?,?,?,?,?,'created',?)",
                (task_id, name, scenario, wave, canonical_json(config), now),
            )
            conn.execute(
                "INSERT INTO checkpoints(task_id,cursor_json,completed_count,"
                "completed_session_ids_json,updated_at) VALUES(?, '{}', 0, '[]', ?)",
                (task_id, now),
            )

        self._write(tx)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            task = dict(row)
            task["config"] = json.loads(task.pop("config_json"))
            task["summary"] = json.loads(task.pop("summary_json") or "{}")
            return task

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        tasks = []
        for row in rows:
            task = dict(row)
            task["config"] = json.loads(task.pop("config_json"))
            task["summary"] = json.loads(task.pop("summary_json") or "{}")
            tasks.append(task)
        return tasks

    def begin_task_run(self, task_id: str, resuming: bool = False) -> bool:
        now = utcnow()
        allowed = ("running", "partial", "recoverable", "failed") if resuming else ("created",)

        def tx(conn: sqlite3.Connection):
            placeholders = ",".join("?" for _ in allowed)
            sql = (
                f"UPDATE tasks SET status='running', task_code=NULL, error=NULL, "
                f"started_at=COALESCE(started_at, ?), finished_at=NULL, "
                f"resume_count=resume_count + ? WHERE id=? AND status IN ({placeholders})"
            )
            cur = conn.execute(sql, (now, 1 if resuming else 0, task_id, *allowed))
            return cur.rowcount == 1

        return bool(self._write(tx))

    def finish_task(
        self,
        task_id: str,
        status: str,
        task_code: str | None,
        summary: dict[str, Any],
        error: str | None = None,
    ) -> None:
        def tx(conn: sqlite3.Connection):
            conn.execute(
                "UPDATE tasks SET status=?, task_code=?, summary_json=?, error=?, "
                "finished_at=? WHERE id=?",
                (status, task_code, canonical_json(summary), error, utcnow(), task_id),
            )

        self._write(tx)

    def update_task_code(self, task_id: str, task_code: str | None) -> None:
        self._write(
            lambda conn: conn.execute(
                "UPDATE tasks SET task_code=? WHERE id=?", (task_code, task_id)
            )
        )

    def get_checkpoint(self, task_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM checkpoints WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"task_id": task_id, "cursor": {}, "completed_count": 0, "completed_session_ids": []}
        item = dict(row)
        item["cursor"] = json.loads(item.pop("cursor_json") or "{}")
        item["completed_session_ids"] = json.loads(item.pop("completed_session_ids_json") or "[]")
        return item

    def save_page(
        self,
        task_id: str,
        page_no: int,
        cursor: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> None:
        now = utcnow()

        def tx(conn: sqlite3.Connection):
            for item in items:
                conn.execute(
                    "INSERT OR IGNORE INTO task_items(task_id,item_key,page_no,business_id,"
                    "schema_version,revision,detail_url,status) VALUES(?,?,?,?,?,?,?,'pending')",
                    (
                        task_id,
                        item["item_key"],
                        page_no,
                        item["business_id"],
                        item["schema_version"],
                        item["revision"],
                        item["detail_url"],
                    ),
                )
            completed_count = conn.execute(
                "SELECT COUNT(*) FROM task_completed_items WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE checkpoints SET cursor_json=?, completed_count=?, updated_at=? WHERE task_id=?",
                (canonical_json(cursor), completed_count, now, task_id),
            )
            conn.execute(
                "UPDATE tasks SET started_at=COALESCE(started_at, ?) WHERE id=?",
                (now, task_id),
            )

        self._write(tx)

    def record_render_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._write(
            lambda conn: conn.execute(
                "INSERT INTO render_snapshots(task_id,page_no,observed_at,url,container_present,"
                "item_count,stable_count,body_sha256,fragment_excerpt,accepted) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot["task_id"],
                    snapshot["page_no"],
                    snapshot["observed_at"],
                    snapshot["url"],
                    1 if snapshot["container_present"] else 0,
                    snapshot["item_count"],
                    snapshot["stable_count"],
                    snapshot.get("body_sha256"),
                    snapshot.get("fragment_excerpt"),
                    1 if snapshot.get("accepted") else 0,
                ),
            )
        )

    def save_raw_response(
        self,
        *,
        task_id: str,
        session_id: str | None,
        item_key: str | None,
        url: str,
        kind: str,
        status_code: int,
        body: str,
        headers: dict[str, str],
        wave: str | None,
        request_seq: int | None,
    ) -> int:
        return self._write(
            lambda conn: conn.execute(
                "INSERT INTO raw_responses(task_id,session_id,item_key,url,kind,status_code,"
                "body,body_sha256,headers_json,fetched_at,wave,request_seq) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    session_id,
                    item_key,
                    url,
                    kind,
                    status_code,
                    body,
                    sha256_text(body),
                    canonical_json(headers),
                    utcnow(),
                    wave,
                    request_seq,
                ),
            ).lastrowid
        )

    def record_request_event(self, event: dict[str, Any]) -> None:
        self._write(
            lambda conn: conn.execute(
                "INSERT INTO request_events(task_id,session_id,item_key,url,kind,attempt,"
                "status_code,duration_ms,error_code,fetched_at,body_sha256,request_seq) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event["task_id"],
                    event.get("session_id"),
                    event.get("item_key"),
                    event["url"],
                    event["kind"],
                    event["attempt"],
                    event.get("status_code"),
                    event["duration_ms"],
                    event.get("error_code"),
                    event["fetched_at"],
                    event.get("body_sha256"),
                    event.get("request_seq"),
                ),
            )
        )

    def create_crawl_session(
        self, session_id: str, task_id: str, archive_session_id: str | None, wave: str
    ) -> None:
        self._write(
            lambda conn: conn.execute(
                "INSERT INTO crawl_sessions(id,task_id,archive_session_id,wave,state,acquired_at) "
                "VALUES(?,?,?,?,'active',?)",
                (session_id, task_id, archive_session_id, wave, utcnow()),
            )
        )

    def increment_session_replays(self, session_id: str) -> None:
        self._write(
            lambda conn: conn.execute(
                "UPDATE crawl_sessions SET replay_count=replay_count+1 WHERE id=?", (session_id,)
            )
        )

    def complete_task_sessions(self, task_id: str) -> list[str]:
        now = utcnow()

        def tx(conn: sqlite3.Connection):
            conn.execute(
                "UPDATE crawl_sessions SET state='completed', completed_at=? "
                "WHERE task_id=? AND state='active'",
                (now, task_id),
            )
            rows = conn.execute(
                "SELECT id FROM crawl_sessions WHERE task_id=? AND state='completed' ORDER BY acquired_at",
                (task_id,),
            ).fetchall()
            ids = [row[0] for row in rows]
            conn.execute(
                "UPDATE checkpoints SET completed_session_ids_json=?, updated_at=? WHERE task_id=?",
                (canonical_json(ids), now, task_id),
            )
            return ids

        return self._write(tx)

    def mark_interrupted_sessions(self, task_id: str) -> None:
        self._write(
            lambda conn: conn.execute(
                "UPDATE crawl_sessions SET state='interrupted' WHERE task_id=? AND state='active'",
                (task_id,),
            )
        )

    def update_item_attempt(
        self, task_id: str, item_key: str, attempts: int, error: str | None = None
    ) -> None:
        self._write(
            lambda conn: conn.execute(
                "UPDATE task_items SET attempts=?, last_error=COALESCE(?, last_error) "
                "WHERE task_id=? AND item_key=?",
                (attempts, error, task_id, item_key),
            )
        )

    def ingest_detail(
        self,
        *,
        task_id: str,
        session_id: str | None,
        item_key: str,
        url: str,
        body: str,
        headers: dict[str, str],
        wave: str,
        request_seq: int | None,
        record: dict[str, Any],
        attempts: int,
    ) -> dict[str, Any]:
        fields = record["fields"]
        incoming_hash = field_hash(fields)
        now = utcnow()

        def tx(conn: sqlite3.Connection):
            raw_id = conn.execute(
                "INSERT INTO raw_responses(task_id,session_id,item_key,url,kind,status_code,"
                "body,body_sha256,headers_json,fetched_at,wave,request_seq) "
                "VALUES(?,?,?,?,'detail',200,?,?,?,?,?,?)",
                (
                    task_id,
                    session_id,
                    item_key,
                    url,
                    body,
                    sha256_text(body),
                    canonical_json(headers),
                    now,
                    wave,
                    request_seq,
                ),
            ).lastrowid
            key = (
                record["business_id"],
                record["schema_version"],
                int(record["revision"]),
            )
            existing = conn.execute(
                "SELECT * FROM normalized_records WHERE business_id=? AND schema_version=? AND revision=?",
                key,
            ).fetchone()
            outcome = "inserted"
            if existing is None:
                record_id = conn.execute(
                    "INSERT INTO normalized_records(business_id,schema_version,revision,"
                    "declared_schema_version,parser_version,unresolved,fields_json,field_hash,"
                    "schema_evidence_json,raw_response_id,task_id,session_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record["business_id"],
                        record["schema_version"],
                        int(record["revision"]),
                        record.get("declared_schema_version"),
                        record["parser_version"],
                        1 if record.get("unresolved") else 0,
                        canonical_json(fields),
                        incoming_hash,
                        canonical_json(record.get("schema_evidence", {})),
                        raw_id,
                        task_id,
                        session_id,
                        now,
                    ),
                ).lastrowid
                self._rebuild_edges(conn, record["business_id"], now)
                status = "completed"
                business_code = None
                result_kind = "inserted"
            elif existing["field_hash"] == incoming_hash:
                record_id = existing["id"]
                status = "completed"
                business_code = None
                result_kind = "duplicate"
                outcome = "duplicate"
            else:
                record_id = existing["id"]
                conn.execute(
                    "INSERT OR IGNORE INTO conflicts(business_id,schema_version,revision,"
                    "existing_record_id,incoming_fields_json,incoming_hash,incoming_unresolved,"
                    "raw_response_id,task_id,detected_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        record["business_id"],
                        record["schema_version"],
                        int(record["revision"]),
                        record_id,
                        canonical_json(fields),
                        incoming_hash,
                        1 if record.get("unresolved") else 0,
                        raw_id,
                        task_id,
                        now,
                    ),
                )
                # Conflicts are always retained.  The canonical row is selected by
                # the smallest field hash, not by arrival order, so the final chain
                # stays identical under arbitrary concurrency.
                if incoming_hash < existing["field_hash"]:
                    conn.execute(
                        "UPDATE normalized_records SET declared_schema_version=?, parser_version=?, "
                        "unresolved=?, fields_json=?, field_hash=?, schema_evidence_json=?, "
                        "raw_response_id=?, task_id=?, session_id=? WHERE id=?",
                        (
                            record.get("declared_schema_version"),
                            record["parser_version"],
                            1 if record.get("unresolved") else 0,
                            canonical_json(fields),
                            incoming_hash,
                            canonical_json(record.get("schema_evidence", {})),
                            raw_id,
                            task_id,
                            session_id,
                            record_id,
                        ),
                    )
                    self._rebuild_edges(conn, record["business_id"], now)
                status = "failed"
                business_code = "E_CONFLICT"
                result_kind = "conflict"
                outcome = "conflict"
            conn.execute(
                "INSERT INTO record_observations(business_id,schema_version,revision,raw_response_id,"
                "task_id,session_id,field_hash,outcome,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record["business_id"],
                    record["schema_version"],
                    int(record["revision"]),
                    raw_id,
                    task_id,
                    session_id,
                    incoming_hash,
                    outcome,
                    now,
                ),
            )
            conn.execute(
                "UPDATE task_items SET status=?, business_code=?, result_kind=?, attempts=?, "
                "raw_response_id=?, last_error=?, completed_at=? WHERE task_id=? AND item_key=?",
                (
                    status,
                    business_code,
                    result_kind,
                    attempts,
                    raw_id,
                    business_code,
                    now if status == "completed" else None,
                    task_id,
                    item_key,
                ),
            )
            if status == "completed":
                conn.execute(
                    "INSERT OR REPLACE INTO task_completed_items(task_id,item_key,completed_at) "
                    "VALUES(?,?,?)",
                    (task_id, item_key, now),
                )
            completed_count = conn.execute(
                "SELECT COUNT(*) FROM task_completed_items WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE checkpoints SET completed_count=?, updated_at=? WHERE task_id=?",
                (completed_count, now, task_id),
            )
            return {
                "outcome": outcome,
                "record_id": record_id,
                "raw_response_id": raw_id,
                "business_code": business_code,
                "field_hash": incoming_hash,
            }

        return self._write(tx)

    def _rebuild_edges(self, conn: sqlite3.Connection, business_id: str, now: str) -> None:
        rows = conn.execute(
            "SELECT * FROM normalized_records WHERE business_id=? "
            "ORDER BY revision ASC, schema_version ASC, field_hash ASC",
            (business_id,),
        ).fetchall()
        conn.execute("DELETE FROM version_edges WHERE business_id=?", (business_id,))
        for previous, current in zip(rows, rows[1:]):
            previous_fields = json.loads(previous["fields_json"])
            current_fields = json.loads(current["fields_json"])
            added = sorted(set(current_fields) - set(previous_fields))
            missing = sorted(set(previous_fields) - set(current_fields))
            conn.execute(
                "INSERT INTO version_edges(business_id,from_record_id,to_record_id,"
                "from_revision,to_revision,added_fields_json,missing_fields_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    business_id,
                    previous["id"],
                    current["id"],
                    previous["revision"],
                    current["revision"],
                    canonical_json(added),
                    canonical_json(missing),
                    now,
                ),
            )

    def fail_item(
        self, task_id: str, item_key: str, business_code: str, error: str, attempts: int
    ) -> None:
        self._write(
            lambda conn: conn.execute(
                "UPDATE task_items SET status='failed', business_code=?, result_kind='failed', "
                "attempts=?, last_error=?, completed_at=NULL WHERE task_id=? AND item_key=?",
                (business_code, attempts, error, task_id, item_key),
            )
        )

    def reset_failed_items(self, task_id: str) -> None:
        self._write(
            lambda conn: conn.execute(
                "UPDATE task_items SET status='pending', business_code=NULL, result_kind=NULL, "
                "last_error=NULL WHERE task_id=? AND status='failed'",
                (task_id,),
            )
        )

    def pending_items(self, task_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_items WHERE task_id=? AND status='pending' ORDER BY page_no,item_key",
                (task_id,),
            ).fetchall()
        return self._rows(rows)

    def task_items(self, task_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_items WHERE task_id=? ORDER BY page_no,item_key LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return self._rows(rows)

    def task_failures(self, task_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_items WHERE task_id=? AND status='failed' "
                "ORDER BY page_no,item_key",
                (task_id,),
            ).fetchall()
        return self._rows(rows)

    def task_progress(self, task_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT status, business_code, result_kind, COUNT(*) AS n FROM task_items "
                "WHERE task_id=? GROUP BY status,business_code,result_kind",
                (task_id,),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM task_items WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            completed = conn.execute(
                "SELECT COUNT(*) FROM task_completed_items WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            snapshots = conn.execute(
                "SELECT COUNT(*) FROM render_snapshots WHERE task_id=?", (task_id,)
            ).fetchone()[0]
        by_status: dict[str, int] = {}
        by_code: dict[str, int] = {}
        for row in rows:
            by_status[row["status"]] = by_status.get(row["status"], 0) + row["n"]
            if row["business_code"]:
                by_code[row["business_code"]] = by_code.get(row["business_code"], 0) + row["n"]
        return {
            "total_items": total,
            "completed_items": completed,
            "pending_items": by_status.get("pending", 0),
            "failed_items": by_status.get("failed", 0),
            "by_status": by_status,
            "failures_by_code": by_code,
            "render_observations": snapshots,
        }

    def task_summary(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            return {}
        progress = self.task_progress(task_id)
        with self.connection() as conn:
            inserted = conn.execute(
                "SELECT COUNT(*) FROM normalized_records WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            duplicate = conn.execute(
                "SELECT COUNT(*) FROM task_items WHERE task_id=? AND result_kind='duplicate'",
                (task_id,),
            ).fetchone()[0]
            conflicts = conn.execute(
                "SELECT COUNT(*) FROM conflicts WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            observations = conn.execute(
                "SELECT COUNT(*) FROM record_observations WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            sessions = conn.execute(
                "SELECT COUNT(*) FROM crawl_sessions WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            replays = conn.execute(
                "SELECT COALESCE(SUM(replay_count),0) FROM crawl_sessions WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
        checkpoint = self.get_checkpoint(task_id)
        return {
            "task_status": task["status"],
            "task_code": task["task_code"],
            "task_failure_count": 1 if task["status"] == "failed" else 0,
            "item_failure_count": progress["failed_items"],
            "failures_by_code": progress["failures_by_code"],
            "total_items": progress["total_items"],
            "completed_items": progress["completed_items"],
            "new_record_count": inserted,
            "duplicate_record_count": duplicate,
            "conflict_count": conflicts,
            "observation_count": observations,
            "render_observations": progress["render_observations"],
            "session_count": sessions,
            "session_replay_count": replays,
            "completed_session_ids": checkpoint["completed_session_ids"],
            "cursor": checkpoint["cursor"],
            "recoverable": task["status"] in {"partial", "recoverable", "failed"},
        }

    def records_for_task(self, task_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM normalized_records WHERE task_id=? ORDER BY business_id,revision,id",
                (task_id,),
            ).fetchall()
        return [self._decode_record(dict(row)) for row in rows]

    def _decode_record(self, row: dict[str, Any]) -> dict[str, Any]:
        row["fields"] = json.loads(row.pop("fields_json"))
        row["schema_evidence"] = json.loads(row.pop("schema_evidence_json"))
        row["unresolved"] = bool(row["unresolved"])
        return row

    def task_differences(self, task_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            touched_rows = conn.execute(
                "SELECT DISTINCT business_id FROM normalized_records WHERE task_id=?", (task_id,)
            ).fetchall()
            touched = [row[0] for row in touched_rows]
            new_rows = conn.execute(
                "SELECT * FROM normalized_records WHERE task_id=? ORDER BY business_id,revision,id",
                (task_id,),
            ).fetchall()
            edge_rows: list[sqlite3.Row] = []
            if touched:
                marks = ",".join("?" for _ in touched)
                edge_rows = conn.execute(
                    f"SELECT * FROM version_edges WHERE business_id IN ({marks}) "
                    "ORDER BY business_id,from_revision,to_revision",
                    touched,
                ).fetchall()
            conflict_rows = conn.execute(
                "SELECT * FROM conflicts WHERE task_id=? ORDER BY detected_at", (task_id,)
            ).fetchall()
            failure_rows = conn.execute(
                "SELECT * FROM task_items WHERE task_id=? AND status='failed' "
                "ORDER BY page_no,item_key",
                (task_id,),
            ).fetchall()
        edges = []
        for row in edge_rows:
            edge = dict(row)
            edge["added_fields"] = json.loads(edge.pop("added_fields_json"))
            edge["missing_fields"] = json.loads(edge.pop("missing_fields_json"))
            edges.append(edge)
        conflicts = []
        for row in conflict_rows:
            conflict = dict(row)
            conflict["incoming_fields"] = json.loads(conflict.pop("incoming_fields_json"))
            conflicts.append(conflict)
        return {
            "task_id": task_id,
            "new_records": [self._decode_record(dict(row)) for row in new_rows],
            "version_edges": edges,
            "conflicts": conflicts,
            "item_failures": [dict(row) for row in failure_rows],
        }

    def task_graph(self, task_id: str) -> dict[str, Any]:
        differences = self.task_differences(task_id)
        with self.connection() as conn:
            touched_rows = conn.execute(
                "SELECT DISTINCT business_id FROM normalized_records WHERE task_id=?", (task_id,)
            ).fetchall()
            touched = [row[0] for row in touched_rows]
            records: list[sqlite3.Row] = []
            if touched:
                marks = ",".join("?" for _ in touched)
                records = conn.execute(
                    f"SELECT * FROM normalized_records WHERE business_id IN ({marks}) "
                    "ORDER BY business_id,revision,schema_version,id",
                    touched,
                ).fetchall()
        nodes = []
        for row in records:
            rec = self._decode_record(dict(row))
            nodes.append(
                {
                    "id": f"r{rec['id']}",
                    "record_id": rec["id"],
                    "business_id": rec["business_id"],
                    "revision": rec["revision"],
                    "schema_version": rec["schema_version"],
                    "unresolved": rec["unresolved"],
                    "field_count": len(rec["fields"]),
                    "fields": rec["fields"],
                    "raw_response_id": rec["raw_response_id"],
                }
            )
        edges = [
            {
                "from": f"r{edge['from_record_id']}",
                "to": f"r{edge['to_record_id']}",
                "business_id": edge["business_id"],
                "from_revision": edge["from_revision"],
                "to_revision": edge["to_revision"],
                "added_fields": edge["added_fields"],
                "missing_fields": edge["missing_fields"],
            }
            for edge in differences["version_edges"]
        ]
        return {"nodes": nodes, "edges": edges}

    def business_chain(self, business_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            records = conn.execute(
                "SELECT * FROM normalized_records WHERE business_id=? ORDER BY revision,schema_version,id",
                (business_id,),
            ).fetchall()
            if not records:
                return None
            edges = conn.execute(
                "SELECT * FROM version_edges WHERE business_id=? ORDER BY from_revision,to_revision",
                (business_id,),
            ).fetchall()
            observations = conn.execute(
                "SELECT o.*, r.url, r.body_sha256, r.fetched_at, r.body "
                "FROM record_observations o JOIN raw_responses r ON r.id=o.raw_response_id "
                "WHERE o.business_id=? ORDER BY o.revision,o.created_at,o.id",
                (business_id,),
            ).fetchall()
        decoded_edges = []
        for row in edges:
            item = dict(row)
            item["added_fields"] = json.loads(item.pop("added_fields_json"))
            item["missing_fields"] = json.loads(item.pop("missing_fields_json"))
            decoded_edges.append(item)
        obs = []
        for row in observations:
            item = dict(row)
            body = item.pop("body")
            item["body_excerpt"] = body[:500]
            obs.append(item)
        return {
            "business_id": business_id,
            "records": [self._decode_record(dict(row)) for row in records],
            "edges": decoded_edges,
            "observations": obs,
        }

    def get_raw_response(self, raw_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM raw_responses WHERE id=?", (raw_id,)).fetchone()
        return dict(row) if row else None

    def render_snapshots(self, task_id: str, page_no: int | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if page_no is None:
                rows = conn.execute(
                    "SELECT * FROM render_snapshots WHERE task_id=? ORDER BY id", (task_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM render_snapshots WHERE task_id=? AND page_no=? ORDER BY id",
                    (task_id, page_no),
                ).fetchall()
        return self._rows(rows)

    def request_events(self, task_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM request_events WHERE task_id=? ORDER BY id LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return self._rows(rows)

    def sessions(self, task_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM crawl_sessions WHERE task_id=? ORDER BY acquired_at", (task_id,)
            ).fetchall()
        return self._rows(rows)

    def global_consistency_summary(self) -> dict[str, Any]:
        with self.connection() as conn:
            records = conn.execute(
                "SELECT * FROM normalized_records ORDER BY business_id,revision,schema_version,id"
            ).fetchall()
            edges = conn.execute(
                "SELECT * FROM version_edges ORDER BY business_id,from_revision,to_revision"
            ).fetchall()
        businesses: dict[str, Any] = {}
        for row in records:
            fields = json.loads(row["fields_json"])
            item = businesses.setdefault(
                row["business_id"],
                {"business_id": row["business_id"], "records": [], "edges": []},
            )
            item["records"].append(
                {
                    "schema_version": row["schema_version"],
                    "revision": row["revision"],
                    "unresolved": bool(row["unresolved"]),
                    "fields": fields,
                    "field_hash": row["field_hash"],
                    "raw_response_id": row["raw_response_id"],
                }
            )
        for row in edges:
            item = businesses.setdefault(
                row["business_id"],
                {"business_id": row["business_id"], "records": [], "edges": []},
            )
            item["edges"].append(
                {
                    "from_revision": row["from_revision"],
                    "to_revision": row["to_revision"],
                    "added_fields": json.loads(row["added_fields_json"]),
                    "missing_fields": json.loads(row["missing_fields_json"]),
                }
            )
        return {
            "businesses": [businesses[key] for key in sorted(businesses)],
            "business_count": len(businesses),
            "record_count": len(records),
            "edge_count": len(edges),
        }


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
