from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json, digest_json
from .errors import FormatCorrupt, StageBusy


SCHEMA = """
CREATE TABLE IF NOT EXISTS committed (
    query_id TEXT PRIMARY KEY,
    query_content_hash TEXT NOT NULL,
    dependency_hash TEXT NOT NULL,
    route_keys_json TEXT NOT NULL,
    config_payload_json TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    updated_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key TEXT NOT NULL,
    stage TEXT NOT NULL,
    completed_query_ids_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_run
    ON checkpoints(run_key, version DESC);
CREATE TABLE IF NOT EXISTS commit_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    checkpoint_version INTEGER,
    committed_ns INTEGER NOT NULL
);
"""


class StageLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.handle = None

    def __enter__(self) -> "StageLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(
                self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise StageBusy(
                "another batch or submission owns the stage lock",
                detail={"lock_path": str(self.path)},
            ) from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()}\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


class StateStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "state.sqlite"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def get_committed(self, query_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM committed WHERE query_id = ?", (query_id,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["route_keys"] = json.loads(record.pop("route_keys_json"))
        record["config_payload"] = json.loads(record.pop("config_payload_json"))
        record["envelope"] = json.loads(record.pop("envelope_json"))
        record["result"] = json.loads(record.pop("result_json"))
        if digest_json(record["result"]) != record["body_hash"]:
            raise FormatCorrupt(
                f"committed body checksum mismatch for {query_id}",
                field="state.committed",
            )
        return record

    def get_all_committed(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT query_id FROM committed ORDER BY query_id"
            ).fetchall()
        return {
            row["query_id"]: self.get_committed(row["query_id"])
            for row in rows
        }

    def commit_many(
        self,
        records: Iterable[dict[str, Any]],
        *,
        checkpoint_version: int | None,
    ) -> int:
        now = time.time_ns()
        rows = []
        audits = []
        for record in records:
            body_hash = digest_json(record["result"])
            rows.append(
                (
                    record["query_id"],
                    record["query_content_hash"],
                    record["dependency_hash"],
                    canonical_json(record["route_keys"]),
                    canonical_json(record["config_payload"]),
                    record["config_digest"],
                    canonical_json(record["envelope"]),
                    canonical_json(record["result"]),
                    body_hash,
                    now,
                )
            )
            audits.append(
                (record["query_id"], body_hash, checkpoint_version, now)
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    """
                    INSERT INTO committed(
                        query_id, query_content_hash, dependency_hash,
                        route_keys_json, config_payload_json, config_digest,
                        envelope_json, result_json, body_hash, updated_ns
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(query_id) DO UPDATE SET
                        query_content_hash=excluded.query_content_hash,
                        dependency_hash=excluded.dependency_hash,
                        route_keys_json=excluded.route_keys_json,
                        config_payload_json=excluded.config_payload_json,
                        config_digest=excluded.config_digest,
                        envelope_json=excluded.envelope_json,
                        result_json=excluded.result_json,
                        body_hash=excluded.body_hash,
                        updated_ns=excluded.updated_ns
                    """,
                    rows,
                )
                connection.executemany(
                    """
                    INSERT INTO commit_audit(
                        query_id, body_hash, checkpoint_version, committed_ns
                    ) VALUES(?, ?, ?, ?)
                    """,
                    audits,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return len(rows)

    def latest_checkpoint(self, run_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE run_key = ?
                ORDER BY version DESC LIMIT 1
                """,
                (run_key,),
            ).fetchone()
        if row is None:
            return None
        checkpoint = dict(row)
        checkpoint["completed_query_ids"] = json.loads(
            checkpoint.pop("completed_query_ids_json")
        )
        payload = json.loads(checkpoint.pop("payload_json"))
        expected = digest_json(
            {
                "run_key": checkpoint["run_key"],
                "stage": checkpoint["stage"],
                "completed_query_ids": checkpoint["completed_query_ids"],
                "payload": payload,
            }
        )
        if expected != checkpoint["checksum"]:
            raise FormatCorrupt(
                f"checkpoint {checkpoint['version']} checksum mismatch",
                field="checkpoint.checksum",
            )
        checkpoint["payload"] = payload
        return checkpoint

    def save_checkpoint(
        self,
        *,
        run_key: str,
        stage: str,
        completed_query_ids: Iterable[str],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        ids = sorted(set(completed_query_ids))
        checksum = digest_json(
            {
                "run_key": run_key,
                "stage": stage,
                "completed_query_ids": ids,
                "payload": payload,
            }
        )
        now = time.time_ns()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO checkpoints(
                    run_key, stage, completed_query_ids_json,
                    checksum, payload_json, created_ns
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    run_key,
                    stage,
                    canonical_json(ids),
                    checksum,
                    canonical_json(payload),
                    now,
                ),
            )
            connection.commit()
            version = int(cursor.lastrowid)
        return {
            "version": version,
            "run_key": run_key,
            "stage": stage,
            "completed_query_ids": ids,
            "checksum": checksum,
            "created_ns": now,
        }

    def commit_audit_count(self, query_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM commit_audit WHERE query_id = ?",
                (query_id,),
            ).fetchone()
        return int(row[0])

    def checkpoint_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM checkpoints"
            ).fetchone()
        return int(row[0])
