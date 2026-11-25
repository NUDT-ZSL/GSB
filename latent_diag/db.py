from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .hashing import canonical_json_bytes, hash_json
from .task import TaskSpec


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    manifest_version INTEGER,
    config_hash TEXT NOT NULL,
    budget_hash TEXT NOT NULL,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error_code TEXT,
    error_json TEXT,
    last_run_id INTEGER
);

CREATE TABLE IF NOT EXISTS stage_results (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    stage TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    artifact_path TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (task_id, revision, stage)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    task_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    artifact_path TEXT,
    payload_json TEXT NOT NULL,
    max_abs REAL NOT NULL,
    max_rel REAL NOT NULL,
    amplification REAL NOT NULL,
    dominant_source TEXT NOT NULL,
    tolerance_ok INTEGER NOT NULL,
    first_deviation_json TEXT,
    last_run_id INTEGER
);

CREATE TABLE IF NOT EXISTS checkpoint_entries (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    task_index INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    ledger_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_task
    ON checkpoint_entries(task_id, checkpoint_id DESC);

CREATE TABLE IF NOT EXISTS cache_index (
    artifact_kind TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    storage TEXT NOT NULL,
    input_hash TEXT,
    task_id TEXT,
    revision INTEGER,
    PRIMARY KEY (artifact_kind, cache_key)
);

CREATE TABLE IF NOT EXISTS rejected_tasks (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    code TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS event_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_run ON event_log(run_id, event_type);

CREATE TABLE IF NOT EXISTS snapshot_versions (
    domain TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    bundle_version INTEGER NOT NULL
);
"""


class LedgerDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.path),
            isolation_level=None,
            timeout=5.0,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _json(self, value: Any) -> str:
        return canonical_json_bytes(value).decode("utf-8")

    def begin_run(
        self,
        *,
        config_hash: str,
        budget_hash: str,
        manifest_version: int | None,
    ) -> int:
        with self.transaction() as conn:
            previous = conn.execute(
                "SELECT run_id FROM runs WHERE status = 'running' ORDER BY run_id"
            ).fetchall()
            for row in previous:
                conn.execute(
                    "UPDATE runs SET status = 'interrupted' WHERE run_id = ?",
                    (int(row["run_id"]),),
                )
            cursor = conn.execute(
                """
                INSERT INTO runs(status, manifest_version, config_hash, budget_hash)
                VALUES('running', ?, ?, ?)
                """,
                (manifest_version, config_hash, budget_hash),
            )
            run_id = int(cursor.lastrowid)
            for row in previous:
                conn.execute(
                    """
                    INSERT INTO event_log(run_id, event_type, task_id, details_json)
                    VALUES(?, 'recovery', NULL, ?)
                    """,
                    (
                        run_id,
                        self._json(
                            {
                                "interrupted_run_id": int(row["run_id"]),
                                "action": "resume_from_checkpoints",
                            }
                        ),
                    ),
                )
            return run_id

    def finish_run(self, run_id: int, status: str, summary: dict[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, summary_json = ? WHERE run_id = ?",
                (status, self._json(summary), run_id),
            )

    def record_event(
        self,
        run_id: int,
        event_type: str,
        task_id: str | None,
        details: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO event_log(run_id, event_type, task_id, details_json)
            VALUES(?, ?, ?, ?)
            """,
            (run_id, event_type, task_id, self._json(details)),
        )

    def _register_artifact(self, conn: sqlite3.Connection, artifact: dict[str, Any]) -> None:
        path = artifact.get("artifact_path")
        if not path:
            return
        conn.execute(
            """
            INSERT INTO cache_index(
                artifact_kind, cache_key, content_hash, artifact_path, storage,
                input_hash, task_id, revision
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_kind, cache_key) DO UPDATE SET
                content_hash=excluded.content_hash,
                artifact_path=excluded.artifact_path,
                storage=excluded.storage,
                input_hash=excluded.input_hash,
                task_id=excluded.task_id,
                revision=excluded.revision
            """,
            (
                artifact["kind"],
                artifact["cache_key"],
                artifact["content_hash"],
                path,
                artifact.get("storage", "array"),
                artifact.get("input_hash"),
                artifact.get("task_id"),
                artifact.get("revision"),
            ),
        )

    def _register_result_artifacts(
        self,
        conn: sqlite3.Connection,
        task: TaskSpec,
        result: dict[str, Any],
    ) -> None:
        for artifact in result["artifacts"]:
            enriched = dict(artifact)
            enriched["input_hash"] = result["input_hash"]
            enriched["task_id"] = task.task_id
            enriched["revision"] = task.revision
            self._register_artifact(conn, enriched)

    def _upsert_task(
        self,
        conn: sqlite3.Connection,
        task: TaskSpec,
        *,
        input_hash: str,
        status: str,
        error: dict[str, Any] | None,
        run_id: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO tasks(
                task_id, revision, input_hash, status, payload_json,
                error_code, error_json, last_run_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                revision=excluded.revision,
                input_hash=excluded.input_hash,
                status=excluded.status,
                payload_json=excluded.payload_json,
                error_code=excluded.error_code,
                error_json=excluded.error_json,
                last_run_id=excluded.last_run_id
            """,
            (
                task.task_id,
                task.revision,
                input_hash,
                status,
                self._json(task.storage_dict()),
                error.get("code") if error else None,
                self._json(error) if error else None,
                run_id,
            ),
        )

    def commit_task_result(
        self,
        *,
        run_id: int,
        task_index: int,
        task: TaskSpec,
        result: dict[str, Any],
        previous_ledger_hash: str | None = None,
    ) -> None:
        status = result["status"]
        error = result.get("error")
        checkpoint_payload = {
            "task_id": task.task_id,
            "revision": task.revision,
            "input_hash": result["input_hash"],
            "ledger_hash": result["ledger_hash"],
            "status": status,
            "error": error,
            "stages": result["stages"],
            "output_hashes": result["output_hashes"],
            "artifacts": result["artifacts"],
        }
        with self.transaction() as conn:
            self._upsert_task(
                conn,
                task,
                input_hash=result["input_hash"],
                status=status,
                error=error,
                run_id=run_id,
            )
            for stage, descriptor in result["stages"].items():
                conn.execute(
                    """
                    INSERT INTO stage_results(
                        task_id, revision, stage, input_hash, content_hash,
                        artifact_path, payload_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, revision, stage) DO UPDATE SET
                        input_hash=excluded.input_hash,
                        content_hash=excluded.content_hash,
                        artifact_path=excluded.artifact_path,
                        payload_json=excluded.payload_json
                    """,
                    (
                        task.task_id,
                        task.revision,
                        stage,
                        result["input_hash"],
                        descriptor["content_hash"],
                        descriptor.get("artifact_path"),
                        self._json(descriptor),
                    ),
                )
            metrics = result["metrics"]
            conn.execute(
                """
                INSERT INTO ledger_entries(
                    task_id, revision, input_hash, content_hash, artifact_path,
                    payload_json, max_abs, max_rel, amplification, dominant_source,
                    tolerance_ok, first_deviation_json, last_run_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    revision=excluded.revision,
                    input_hash=excluded.input_hash,
                    content_hash=excluded.content_hash,
                    artifact_path=excluded.artifact_path,
                    payload_json=excluded.payload_json,
                    max_abs=excluded.max_abs,
                    max_rel=excluded.max_rel,
                    amplification=excluded.amplification,
                    dominant_source=excluded.dominant_source,
                    tolerance_ok=excluded.tolerance_ok,
                    first_deviation_json=excluded.first_deviation_json,
                    last_run_id=excluded.last_run_id
                """,
                (
                    task.task_id,
                    task.revision,
                    result["input_hash"],
                    result["ledger_hash"],
                    result["ledger_path"],
                    self._json(result["ledger"]),
                    float(metrics["max_abs"]),
                    float(metrics["max_rel"]),
                    float(metrics["error_amplification"]),
                    str(metrics["dominant_source"]),
                    1 if metrics["tolerance_ok"] else 0,
                    self._json(metrics.get("first_deviation")),
                    run_id,
                ),
            )
            self._register_result_artifacts(conn, task, result)
            conn.execute(
                """
                INSERT INTO checkpoint_entries(
                    run_id, task_index, task_id, revision, input_hash,
                    ledger_hash, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_index,
                    task.task_id,
                    task.revision,
                    result["input_hash"],
                    result["ledger_hash"],
                    self._json(checkpoint_payload),
                ),
            )
            conn.execute(
                """
                INSERT INTO event_log(run_id, event_type, task_id, details_json)
                VALUES(?, 'recompute', ?, ?)
                """,
                (
                    run_id,
                    task.task_id,
                    self._json(
                        {
                            "input_hash": result["input_hash"],
                            "ledger_hash": result["ledger_hash"],
                            "status": status,
                            "previous_ledger_hash": previous_ledger_hash,
                            "matches_previous": (
                                previous_ledger_hash is None
                                or previous_ledger_hash == result["ledger_hash"]
                            ),
                        }
                    ),
                ),
            )

    def commit_reuse(
        self,
        *,
        run_id: int,
        task_index: int,
        task: TaskSpec,
        checkpoint: dict[str, Any],
    ) -> None:
        with self.transaction() as conn:
            self._upsert_task(
                conn,
                task,
                input_hash=checkpoint["input_hash"],
                status=checkpoint["status"],
                error=checkpoint.get("error"),
                run_id=run_id,
            )
            conn.execute(
                "UPDATE ledger_entries SET last_run_id = ? WHERE task_id = ?",
                (run_id, task.task_id),
            )
            conn.execute(
                """
                INSERT INTO checkpoint_entries(
                    run_id, task_index, task_id, revision, input_hash,
                    ledger_hash, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_index,
                    task.task_id,
                    task.revision,
                    checkpoint["input_hash"],
                    checkpoint["ledger_hash"],
                    self._json(checkpoint),
                ),
            )
            conn.execute(
                """
                INSERT INTO event_log(run_id, event_type, task_id, details_json)
                VALUES(?, 'reuse', ?, ?)
                """,
                (
                    run_id,
                    task.task_id,
                    self._json(
                        {
                            "input_hash": checkpoint["input_hash"],
                            "ledger_hash": checkpoint["ledger_hash"],
                            "checkpoint_source": "content_addressed_cache",
                        }
                    ),
                ),
            )

    def commit_error(
        self,
        *,
        run_id: int,
        task: TaskSpec,
        input_hash: str,
        error: dict[str, Any],
    ) -> None:
        with self.transaction() as conn:
            self._upsert_task(
                conn,
                task,
                input_hash=input_hash,
                status="error",
                error=error,
                run_id=run_id,
            )
            conn.execute("DELETE FROM ledger_entries WHERE task_id = ?", (task.task_id,))
            conn.execute("DELETE FROM stage_results WHERE task_id = ?", (task.task_id,))
            conn.execute(
                """
                INSERT INTO event_log(run_id, event_type, task_id, details_json)
                VALUES(?, 'recompute_failed', ?, ?)
                """,
                (run_id, task.task_id, self._json(error)),
            )

    def record_rejection(
        self,
        *,
        run_id: int,
        task: TaskSpec,
        input_hash: str,
        rejection: dict[str, Any],
        task_index: int,
    ) -> None:
        payload = dict(rejection)
        payload["task_index"] = task_index
        with self.transaction() as conn:
            self._upsert_task(
                conn,
                task,
                input_hash=input_hash,
                status="rejected",
                error={
                    "code": rejection["code"],
                    "message": rejection["reason"],
                    "task_id": task.task_id,
                },
                run_id=run_id,
            )
            conn.execute(
                """
                INSERT INTO rejected_tasks(
                    run_id, task_id, revision, input_hash, code, reason, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task.task_id,
                    task.revision,
                    input_hash,
                    rejection["code"],
                    rejection["reason"],
                    self._json(payload),
                ),
            )
            conn.execute("DELETE FROM ledger_entries WHERE task_id = ?", (task.task_id,))
            conn.execute("DELETE FROM stage_results WHERE task_id = ?", (task.task_id,))
            conn.execute(
                """
                INSERT INTO event_log(run_id, event_type, task_id, details_json)
                VALUES(?, 'rejected', ?, ?)
                """,
                (run_id, task.task_id, self._json(payload)),
            )

    def retire_tasks(self, task_ids: list[str], run_id: int) -> None:
        if not task_ids:
            return
        with self.transaction() as conn:
            for task_id in task_ids:
                conn.execute("DELETE FROM stage_results WHERE task_id = ?", (task_id,))
                conn.execute("DELETE FROM ledger_entries WHERE task_id = ?", (task_id,))
                conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
                conn.execute(
                    """
                    INSERT INTO event_log(run_id, event_type, task_id, details_json)
                    VALUES(?, 'retired', ?, ?)
                    """,
                    (run_id, task_id, self._json({"reason": "removed_from_manifest"})),
                )

    def cache_artifact(self, kind: str, cache_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT artifact_kind, cache_key, content_hash, artifact_path, storage
            FROM cache_index WHERE artifact_kind = ? AND cache_key = ?
            """,
            (kind, cache_key),
        ).fetchone()
        if row is None:
            return None
        return {
            "kind": row["artifact_kind"],
            "cache_key": row["cache_key"],
            "content_hash": row["content_hash"],
            "artifact_path": row["artifact_path"],
            "storage": row["storage"],
        }

    def checkpoint_for(self, task_id: str, input_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT payload_json FROM checkpoint_entries
            WHERE task_id = ? AND input_hash = ?
            ORDER BY checkpoint_id DESC LIMIT 1
            """,
            (task_id, input_hash),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    def ledger_for(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ledger_entries WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def latest_checkpoints(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT c.payload_json
            FROM checkpoint_entries c
            JOIN (
                SELECT task_id, MAX(checkpoint_id) AS checkpoint_id
                FROM checkpoint_entries GROUP BY task_id
            ) latest ON latest.checkpoint_id = c.checkpoint_id
            """
        ).fetchall()
        return {json.loads(row["payload_json"])["task_id"]: json.loads(row["payload_json"]) for row in rows}

    def current_tasks(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload_json FROM tasks ORDER BY task_id"
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def task_payloads(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT payload_json FROM tasks").fetchall()
        return {
            payload["task_id"]: payload
            for payload in (json.loads(row["payload_json"]) for row in rows)
        }

    def ledger_rows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT l.*, t.status AS task_status
            FROM ledger_entries l JOIN tasks t ON t.task_id = l.task_id
            ORDER BY l.task_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def stage_task_sets(self) -> dict[str, set[str]]:
        rows = self.conn.execute(
            """
            SELECT s.stage, s.task_id, t.status
            FROM stage_results s JOIN tasks t ON t.task_id = s.task_id
                AND t.revision = s.revision
            WHERE t.status IN ('ok', 'tolerance_failed')
            """
        ).fetchall()
        result: dict[str, set[str]] = {}
        for row in rows:
            result.setdefault(row["stage"], set()).add(row["task_id"])
        return result

    def accepted_task_ids(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT task_id FROM tasks WHERE status IN ('ok', 'tolerance_failed')"
        ).fetchall()
        return {row["task_id"] for row in rows}

    def task_set(self) -> set[str]:
        return {row["task_id"] for row in self.conn.execute("SELECT task_id FROM tasks")}

    def events(self, run_id: int, event_type: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT task_id, details_json FROM event_log
            WHERE run_id = ? AND event_type = ? ORDER BY seq
            """,
            (run_id, event_type),
        ).fetchall()
        return [
            {"task_id": row["task_id"], **json.loads(row["details_json"])}
            for row in rows
        ]

    def latest_run(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result.get("summary_json"):
            result["summary"] = json.loads(result["summary_json"])
        return result

    def run_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM runs ORDER BY run_id")]

    def cache_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM cache_index ORDER BY artifact_kind, cache_key"
            )
        ]

    def record_snapshot_versions(self, metadata: dict[str, Any]) -> None:
        with self.transaction() as conn:
            bundle_version = int(metadata["bundle_version"])
            for domain, envelope in metadata["domains"].items():
                conn.execute(
                    """
                    INSERT INTO snapshot_versions(domain, version, checksum, bundle_version)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(domain) DO UPDATE SET
                        version=excluded.version,
                        checksum=excluded.checksum,
                        bundle_version=excluded.bundle_version
                    """,
                    (
                        domain,
                        int(envelope["version"]),
                        envelope["checksum"],
                        bundle_version,
                    ),
                )

    def snapshot_version_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM snapshot_versions ORDER BY domain"
            )
        ]

    def ledger_snapshot(self) -> dict[str, Any]:
        rows = self.ledger_rows()
        return {
            "entries": [
                {
                    "task_id": row["task_id"],
                    "revision": row["revision"],
                    "input_hash": row["input_hash"],
                    "content_hash": row["content_hash"],
                    "artifact_path": row["artifact_path"],
                    "tolerance_ok": bool(row["tolerance_ok"]),
                }
                for row in rows
            ],
            "content_hash": hash_json(
                [
                    {
                        "task_id": row["task_id"],
                        "revision": row["revision"],
                        "content_hash": row["content_hash"],
                    }
                    for row in rows
                ]
            ),
        }

    def cache_snapshot(self) -> dict[str, Any]:
        rows = self.cache_rows()
        return {
            "entries": [
                {
                    "artifact_kind": row["artifact_kind"],
                    "cache_key": row["cache_key"],
                    "content_hash": row["content_hash"],
                    "artifact_path": row["artifact_path"],
                }
                for row in rows
            ],
            "content_hash": hash_json(
                [
                    {
                        "artifact_kind": row["artifact_kind"],
                        "cache_key": row["cache_key"],
                        "content_hash": row["content_hash"],
                    }
                    for row in rows
                ]
            ),
        }

