from __future__ import annotations

import contextlib
import fcntl
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .canon import canonical_dumps
from .errors import Code, WorkbenchError


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS input_bundle (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        content_json TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        loaded_at_clock INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS semantic_versions (
        binding_id TEXT PRIMARY KEY,
        snapshot_version INTEGER NOT NULL,
        generation_min INTEGER NOT NULL,
        manifest_hash TEXT NOT NULL,
        binding_json TEXT NOT NULL,
        is_current INTEGER NOT NULL DEFAULT 0,
        applied_clock INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS unified_views (
        view_id TEXT PRIMARY KEY,
        content_json TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_clock INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS change_events (
        topic TEXT NOT NULL,
        topic_seq INTEGER NOT NULL,
        global_seq INTEGER NOT NULL,
        message_key TEXT NOT NULL,
        change_kind TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        event_json TEXT NOT NULL,
        PRIMARY KEY (topic, topic_seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS partition_watermarks (
        partition_key TEXT PRIMARY KEY,
        topic TEXT NOT NULL,
        partition_id INTEGER NOT NULL,
        watermark_offset INTEGER NOT NULL,
        result_hash TEXT NOT NULL UNIQUE,
        content_json TEXT NOT NULL,
        semantic_binding_id TEXT NOT NULL,
        first_checkpoint_id TEXT NOT NULL,
        last_checkpoint_id TEXT NOT NULL,
        updated_at_clock INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS partition_commit_ledger (
        partition_key TEXT NOT NULL,
        semantic_binding_id TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        result_hash TEXT NOT NULL,
        committed_at_clock INTEGER NOT NULL,
        PRIMARY KEY (partition_key, semantic_binding_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscription_cursors (
        subscriber_id TEXT PRIMARY KEY,
        topic TEXT NOT NULL,
        last_topic_seq INTEGER NOT NULL,
        cursor_revision INTEGER NOT NULL,
        checkpoint_id TEXT NOT NULL,
        content_json TEXT NOT NULL,
        updated_at_clock INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cursor_commit_ledger (
        subscriber_id TEXT NOT NULL,
        from_seq INTEGER NOT NULL,
        to_seq INTEGER NOT NULL,
        checkpoint_id TEXT NOT NULL,
        patch_set_hash TEXT NOT NULL,
        committed_at_clock INTEGER NOT NULL,
        PRIMARY KEY (subscriber_id, from_seq, to_seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS patches (
        patch_id TEXT PRIMARY KEY,
        subscriber_id TEXT NOT NULL,
        topic TEXT NOT NULL,
        from_seq INTEGER NOT NULL,
        to_seq INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        content_json TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        created_clock INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invalidation_closures (
        closure_id TEXT PRIMARY KEY,
        trigger_version TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content_json TEXT NOT NULL,
        checkpoint_id TEXT,
        created_clock INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        as_of_clock_ms INTEGER NOT NULL,
        status TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        created_clock INTEGER NOT NULL,
        committed_at_clock INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diagnostic_runs (
        run_id TEXT PRIMARY KEY,
        as_of_clock_ms INTEGER NOT NULL,
        time_budget_ms INTEGER NOT NULL,
        concurrency_per_partition INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        content_json TEXT NOT NULL,
        created_clock INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diagnostic_tasks (
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        subscriber_id TEXT NOT NULL,
        partition_key TEXT NOT NULL,
        end_category TEXT NOT NULL,
        lifecycle TEXT NOT NULL,
        stable_rank INTEGER NOT NULL,
        content_json TEXT NOT NULL,
        PRIMARY KEY (run_id, task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotent_responses (
        request_key TEXT PRIMARY KEY,
        response_hash TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_clock INTEGER NOT NULL
    )
    """,
]


class FileLock:
    def __init__(self, path: Path, timeout_seconds: float = 20.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.handle = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise WorkbenchError(Code.LOCK_BUSY, "timed out waiting for workbench file lock", {"path": str(self.path)})
                time.sleep(0.03)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")

    @contextlib.contextmanager
    def lock(self, timeout_seconds: float = 20.0) -> Iterator[FileLock]:
        with FileLock(self.lock_path, timeout_seconds) as handle:
            yield handle

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=20.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def initialize(self) -> None:
        with self.lock():
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for statement in SCHEMA_STATEMENTS:
                    conn.execute(statement)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def save_bundle(self, bundle: Dict[str, Any], logical_clock: int = 0) -> None:
        content = canonical_dumps(bundle)
        with self.lock():
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO input_bundle(id, content_json, input_hash, loaded_at_clock) VALUES(1, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET content_json=excluded.content_json, input_hash=excluded.input_hash, loaded_at_clock=excluded.loaded_at_clock",
                    (content, bundle["input_hash"], logical_clock),
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('input_hash', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (bundle["input_hash"],),
                )
                self._set_clock_watermark(conn, logical_clock)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def load_bundle(self) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT content_json FROM input_bundle WHERE id=1").fetchone()
            return None if row is None else json.loads(row["content_json"])
        finally:
            conn.close()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return default if row is None else row["value"]
        finally:
            conn.close()

    def current_binding(self) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT binding_json FROM semantic_versions WHERE is_current=1 ORDER BY applied_clock DESC LIMIT 1").fetchone()
            return None if row is None else json.loads(row["binding_json"])
        finally:
            conn.close()

    def latest_view(self) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT content_json FROM unified_views ORDER BY created_clock DESC, rowid DESC LIMIT 1").fetchone()
            return None if row is None else json.loads(row["content_json"])
        finally:
            conn.close()

    def view_by_hash(self, content_hash: str) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT content_json FROM unified_views WHERE content_hash=? ORDER BY rowid DESC LIMIT 1", (content_hash,)).fetchone()
            return None if row is None else json.loads(row["content_json"])
        finally:
            conn.close()

    def partition_watermark_state(self) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT partition_key, semantic_binding_id, result_hash, content_json FROM partition_watermarks ORDER BY partition_key"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def partition_commit_keys(self) -> set[tuple[str, str]]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT partition_key, semantic_binding_id FROM partition_commit_ledger").fetchall()
            return {(row["partition_key"], row["semantic_binding_id"]) for row in rows}
        finally:
            conn.close()

    def save_merge_view(
        self,
        view: Dict[str, Any],
        binding: Dict[str, Any],
        logical_clock: int,
        change_events: Optional[List[Dict[str, Any]]] = None,
        closures: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        with self.lock():
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._upsert_view(conn, view, logical_clock)
                self._upsert_binding(conn, binding, logical_clock)
                for event in change_events or []:
                    conn.execute(
                        "INSERT OR IGNORE INTO change_events(topic, topic_seq, global_seq, message_key, change_kind, content_hash, event_json) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (event["topic"], event["topic_seq"], event["global_seq"], event["message_key"], event["change_kind"], event.get("new_content_hash") or "", canonical_dumps(event)),
                    )
                for closure in closures or []:
                    conn.execute(
                        "INSERT OR IGNORE INTO invalidation_closures(closure_id, trigger_version, content_hash, content_json, checkpoint_id, created_clock) VALUES(?, ?, ?, ?, NULL, ?)",
                        (closure["closure_id"], closure["trigger_version"], closure["content_hash"], canonical_dumps(closure), logical_clock),
                    )
                self._set_clock_watermark(conn, logical_clock)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def change_seq_state(self) -> Dict[str, Any]:
        conn = self.connect()
        try:
            topic_rows = conn.execute("SELECT topic, MAX(topic_seq) AS seq FROM change_events GROUP BY topic").fetchall()
            global_row = conn.execute("SELECT COALESCE(MAX(global_seq), 0) AS seq FROM change_events").fetchone()
            return {"by_topic": {row["topic"]: int(row["seq"]) for row in topic_rows}, "global": int(global_row["seq"])}
        finally:
            conn.close()

    def cursor_map(self) -> Dict[str, int]:
        conn = self.connect()
        try:
            return {row["subscriber_id"]: int(row["last_topic_seq"]) for row in conn.execute("SELECT subscriber_id, last_topic_seq FROM subscription_cursors")}
        finally:
            conn.close()

    def list_change_events(self) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT event_json FROM change_events ORDER BY global_seq, topic_seq").fetchall()
            return [json.loads(row["event_json"]) for row in rows]
        finally:
            conn.close()

    def prepare_checkpoint(self, plan: Dict[str, Any]) -> str:
        with self.lock():
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO checkpoints(checkpoint_id, as_of_clock_ms, status, plan_hash, plan_json, created_clock, committed_at_clock) "
                    "VALUES(?, ?, 'prepared', ?, ?, ?, NULL) "
                    "ON CONFLICT(checkpoint_id) DO NOTHING",
                    (
                        plan["checkpoint_id"],
                        plan["as_of_clock_ms"],
                        plan["plan_hash"],
                        canonical_dumps(plan),
                        plan["logical_clock_watermark"],
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        return plan["checkpoint_id"]

    def get_checkpoint(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
            return None if row is None else dict(row)
        finally:
            conn.close()

    def prepared_checkpoint_ids(self) -> List[str]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT checkpoint_id FROM checkpoints WHERE status='prepared' ORDER BY rowid").fetchall()
            return [row["checkpoint_id"] for row in rows]
        finally:
            conn.close()

    def commit_prepared_checkpoint(self, checkpoint_id: str) -> Dict[str, Any]:
        with self.lock():
            conn = self.connect()
            try:
                row = conn.execute("SELECT * FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
                if row is None:
                    raise WorkbenchError(Code.NOT_LOADED, "checkpoint does not exist", {"checkpoint_id": checkpoint_id})
                if row["status"] == "committed":
                    conn.close()
                    return {"checkpoint_id": checkpoint_id, "status": "already_committed"}
                plan = json.loads(row["plan_json"])
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT status FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
                if current["status"] == "committed":
                    conn.execute("ROLLBACK")
                    return {"checkpoint_id": checkpoint_id, "status": "already_committed"}
                if plan.get("view") is not None:
                    self._upsert_view(conn, plan["view"], plan["logical_clock_watermark"])
                if plan.get("semantic_binding") is not None:
                    self._upsert_binding(conn, plan["semantic_binding"], plan["logical_clock_watermark"])
                for event in plan.get("change_events", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO change_events(topic, topic_seq, global_seq, message_key, change_kind, content_hash, event_json) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (event["topic"], event["topic_seq"], event["global_seq"], event["message_key"], event["change_kind"], event.get("new_content_hash") or "", canonical_dumps(event)),
                    )
                for closure in plan.get("closures", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO invalidation_closures(closure_id, trigger_version, content_hash, content_json, checkpoint_id, created_clock) VALUES(?, ?, ?, ?, ?, ?)",
                        (closure["closure_id"], closure["trigger_version"], closure["content_hash"], canonical_dumps(closure), checkpoint_id, plan["logical_clock_watermark"]),
                    )
                binding_id = plan["semantic_binding"]["binding_id"] if plan.get("semantic_binding") else conn.execute("SELECT value FROM meta WHERE key='semantic_binding_id'").fetchone()[0]
                for pk, record in plan.get("watermark_records", {}).items():
                    inserted = conn.execute(
                        "INSERT OR IGNORE INTO partition_commit_ledger(partition_key, semantic_binding_id, checkpoint_id, result_hash, committed_at_clock) VALUES(?, ?, ?, ?, ?)",
                        (pk, binding_id, checkpoint_id, record["content_hash"], plan["logical_clock_watermark"]),
                    )
                    if inserted.rowcount == 1:
                        conn.execute(
                            "INSERT INTO partition_watermarks(partition_key, topic, partition_id, watermark_offset, result_hash, content_json, semantic_binding_id, first_checkpoint_id, last_checkpoint_id, updated_at_clock) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(partition_key) DO UPDATE SET topic=excluded.topic, partition_id=excluded.partition_id, watermark_offset=excluded.watermark_offset, result_hash=excluded.result_hash, content_json=excluded.content_json, semantic_binding_id=excluded.semantic_binding_id, last_checkpoint_id=excluded.last_checkpoint_id, updated_at_clock=excluded.updated_at_clock",
                            (pk, record["topic"], record["partition"], record["watermark_offset"], record["content_hash"], canonical_dumps(record), binding_id, checkpoint_id, checkpoint_id, plan["logical_clock_watermark"]),
                        )
                for cursor_update in plan.get("cursor_updates", []):
                    ledger = conn.execute(
                        "INSERT OR IGNORE INTO cursor_commit_ledger(subscriber_id, from_seq, to_seq, checkpoint_id, patch_set_hash, committed_at_clock) VALUES(?, ?, ?, ?, ?, ?)",
                        (cursor_update["subscriber_id"], cursor_update["from_seq"], cursor_update["to_seq"], checkpoint_id, cursor_update["patch_set_hash"], plan["logical_clock_watermark"]),
                    )
                    if ledger.rowcount == 1:
                        conn.execute(
                            "INSERT INTO subscription_cursors(subscriber_id, topic, last_topic_seq, cursor_revision, checkpoint_id, content_json, updated_at_clock) VALUES(?, ?, ?, 1, ?, ?, ?) "
                            "ON CONFLICT(subscriber_id) DO UPDATE SET topic=excluded.topic, last_topic_seq=excluded.last_topic_seq, cursor_revision=subscription_cursors.cursor_revision+1, checkpoint_id=excluded.checkpoint_id, content_json=excluded.content_json, updated_at_clock=excluded.updated_at_clock "
                            "WHERE subscription_cursors.last_topic_seq < excluded.last_topic_seq",
                            (cursor_update["subscriber_id"], cursor_update["topic"], cursor_update["to_seq"], checkpoint_id, canonical_dumps(cursor_update["cursor_record"]), plan["logical_clock_watermark"]),
                        )
                for patch in plan.get("patches", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO patches(patch_id, subscriber_id, topic, from_seq, to_seq, content_hash, content_json, checkpoint_id, created_clock) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (patch["patch_id"], patch["subscriber_id"], patch["topic"], patch["from_topic_seq"], patch["to_topic_seq"], patch["content_hash"], canonical_dumps(patch), checkpoint_id, plan["logical_clock_watermark"]),
                    )
                diagnostic_run = plan.get("diagnostic_run")
                if diagnostic_run is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO diagnostic_runs(run_id, as_of_clock_ms, time_budget_ms, concurrency_per_partition, content_hash, content_json, created_clock) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (diagnostic_run["run_id"], diagnostic_run["as_of_clock_ms"], diagnostic_run["time_budget_ms"], diagnostic_run["concurrency_per_partition"], diagnostic_run["content_hash"], canonical_dumps(diagnostic_run), plan["logical_clock_watermark"]),
                    )
                    for task in diagnostic_run.get("tasks", []):
                        conn.execute(
                            "INSERT OR IGNORE INTO diagnostic_tasks(run_id, task_id, subscriber_id, partition_key, end_category, lifecycle, stable_rank, content_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                            (diagnostic_run["run_id"], task["task_id"], task["subscriber_id"], task["partition_key"], task["end_category"], task["lifecycle"], task["stable_rank"], canonical_dumps(task)),
                        )
                self._set_clock_watermark(conn, plan["logical_clock_watermark"])
                conn.execute("UPDATE checkpoints SET status='committed', committed_at_clock=? WHERE checkpoint_id=?", (plan["logical_clock_watermark"], checkpoint_id))
                conn.execute("COMMIT")
                return {"checkpoint_id": checkpoint_id, "status": "committed"}
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def save_diagnostic_run(self, run: Dict[str, Any]) -> str:
        with self.lock():
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO diagnostic_runs(run_id, as_of_clock_ms, time_budget_ms, concurrency_per_partition, content_hash, content_json, created_clock) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (run["run_id"], run["as_of_clock_ms"], run["time_budget_ms"], run["concurrency_per_partition"], run["content_hash"], canonical_dumps(run), run["as_of_clock_ms"]),
                )
                for task in run["tasks"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO diagnostic_tasks(run_id, task_id, subscriber_id, partition_key, end_category, lifecycle, stable_rank, content_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (run["run_id"], task["task_id"], task["subscriber_id"], task["partition_key"], task["end_category"], task["lifecycle"], task["stable_rank"], canonical_dumps(task)),
                    )
                conn.execute("COMMIT")
                return run["run_id"]
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def get_idempotent(self, request_key: str) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT response_json, response_hash FROM idempotent_responses WHERE request_key=?", (request_key,)).fetchone()
            return None if row is None else json.loads(row["response_json"])
        finally:
            conn.close()

    def put_idempotent(self, request_key: str, response: Dict[str, Any], logical_clock: int) -> None:
        content = canonical_dumps(response)
        with self.lock():
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO idempotent_responses(request_key, response_hash, response_json, created_clock) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(request_key) DO UPDATE SET response_hash=excluded.response_hash, response_json=excluded.response_json",
                    (request_key, response.get("response_hash", ""), content, logical_clock),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def status(self) -> Dict[str, Any]:
        conn = self.connect()
        try:
            clock_row = conn.execute("SELECT value FROM meta WHERE key='logical_clock_watermark'").fetchone()
            binding_row = conn.execute("SELECT binding_json FROM semantic_versions WHERE is_current=1 ORDER BY applied_clock DESC LIMIT 1").fetchone()
            watermarks = [json.loads(row["content_json"]) for row in conn.execute("SELECT content_json FROM partition_watermarks ORDER BY partition_key")]
            cursors = [json.loads(row["content_json"]) for row in conn.execute("SELECT content_json FROM subscription_cursors ORDER BY subscriber_id")]
            closures = [json.loads(row["content_json"]) for row in conn.execute("SELECT content_json FROM invalidation_closures ORDER BY closure_id")]
            patches = [json.loads(row["content_json"]) for row in conn.execute("SELECT content_json FROM patches ORDER BY patch_id")]
            runs = [json.loads(row["content_json"]) for row in conn.execute("SELECT content_json FROM diagnostic_runs ORDER BY run_id")]
            checkpoints = [dict(row) for row in conn.execute("SELECT checkpoint_id, as_of_clock_ms, status, plan_hash, created_clock, committed_at_clock FROM checkpoints ORDER BY rowid")]
            input_hash_row = conn.execute("SELECT value FROM meta WHERE key='input_hash'").fetchone()
            return {
                "schema": "delivery-workbench-status/v1",
                "logical_clock_watermark": int(clock_row["value"]) if clock_row else 0,
                "input_hash": input_hash_row["value"] if input_hash_row else None,
                "semantic_version_binding": json.loads(binding_row["binding_json"]) if binding_row else None,
                "partition_watermarks": watermarks,
                "subscription_cursors": cursors,
                "invalidation_event_chains": closures,
                "patches": patches,
                "diagnostic_runs": runs,
                "recovery": {
                    "prepared_checkpoint_ids": [item["checkpoint_id"] for item in checkpoints if item["status"] == "prepared"],
                    "committed_checkpoint_count": sum(1 for item in checkpoints if item["status"] == "committed"),
                    "has_half_committed_record": any(item["status"] == "prepared" for item in checkpoints),
                },
            }
        finally:
            conn.close()

    def _upsert_view(self, conn: sqlite3.Connection, view: Dict[str, Any], clock: int) -> None:
        conn.execute(
            "INSERT INTO unified_views(view_id, content_json, content_hash, created_clock) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(view_id) DO UPDATE SET content_json=excluded.content_json, content_hash=excluded.content_hash",
            (view["view_id"], canonical_dumps(view), view["content_hash"], clock),
        )

    def _upsert_binding(self, conn: sqlite3.Connection, binding: Dict[str, Any], clock: int) -> None:
        conn.execute("UPDATE semantic_versions SET is_current=0")
        conn.execute(
            "INSERT INTO semantic_versions(binding_id, snapshot_version, generation_min, manifest_hash, binding_json, is_current, applied_clock) VALUES(?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(binding_id) DO UPDATE SET snapshot_version=excluded.snapshot_version, generation_min=excluded.generation_min, manifest_hash=excluded.manifest_hash, binding_json=excluded.binding_json, is_current=1, applied_clock=excluded.applied_clock",
            (binding["binding_id"], binding["snapshot_version"], binding["generation_min"], binding["manifest_hash"], canonical_dumps(binding), clock),
        )
        conn.execute("INSERT INTO meta(key, value) VALUES('semantic_binding_id', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (binding["binding_id"],))

    def _set_clock_watermark(self, conn: sqlite3.Connection, clock: int) -> None:
        row = conn.execute("SELECT value FROM meta WHERE key='logical_clock_watermark'").fetchone()
        current = int(row["value"]) if row else 0
        if clock > current:
            conn.execute("INSERT INTO meta(key, value) VALUES('logical_clock_watermark', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(clock),))
