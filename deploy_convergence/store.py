"""SQLite persistence, atomic view switching and crash recovery journal."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json
from .errors import EngineError


SCHEMA_VERSION = 2


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO state(key, value) VALUES ('current_view_id', '');

CREATE TABLE IF NOT EXISTS clock_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    watermark_ms INTEGER NOT NULL
);
INSERT OR IGNORE INTO clock_state(singleton, watermark_ms) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS view_versions (
    view_id TEXT PRIMARY KEY,
    package_key TEXT NOT NULL,
    manifest_version TEXT NOT NULL,
    generation_min INTEGER NOT NULL,
    shard_digest_json TEXT NOT NULL,
    parameter_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    committed_at_ms INTEGER,
    commit_seq INTEGER
);
CREATE INDEX IF NOT EXISTS idx_view_versions_package_key
ON view_versions(package_key);

CREATE TABLE IF NOT EXISTS view_services (
    view_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    service TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (view_id, environment, service)
);

CREATE TABLE IF NOT EXISTS view_blobs (
    view_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (view_id, kind)
);

CREATE TABLE IF NOT EXISTS staging_versions (
    staging_id TEXT PRIMARY KEY,
    view_id TEXT NOT NULL,
    package_key TEXT NOT NULL,
    descriptor_json TEXT NOT NULL,
    change_decision_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS staging_services (
    staging_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    service TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (staging_id, environment, service)
);

CREATE TABLE IF NOT EXISTS staging_blobs (
    staging_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (staging_id, kind)
);

CREATE TABLE IF NOT EXISTS operations (
    op_id TEXT PRIMARY KEY,
    package_key TEXT NOT NULL,
    status TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    error_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_operations_package_status
ON operations(package_key, status);

CREATE TABLE IF NOT EXISTS operation_steps (
    op_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL,
    output_json TEXT,
    completed_at_ms INTEGER,
    replay_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (op_id, step_name)
);
"""


@contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(SCHEMA_SQL)
            row = connection.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_meta(version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif int(row["version"]) != SCHEMA_VERSION:
                raise EngineError(
                    "E_STATE_CORRUPT",
                    "unsupported state database schema version",
                    stage="persist",
                    details={
                        "path": str(self.path),
                        "expected": SCHEMA_VERSION,
                        "actual": None if row is None else row["version"],
                    },
                )
            connection.commit()
        finally:
            connection.close()

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def check_clock(self, clock_ms: int, tolerance_ms: int) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT watermark_ms FROM clock_state WHERE singleton=1"
            ).fetchone()
        finally:
            connection.close()
        watermark = int(row["watermark_ms"])
        if clock_ms + tolerance_ms < watermark:
            raise EngineError(
                "E_CLOCK_ROLLBACK",
                "injected logical clock moved beyond rollback tolerance",
                stage="evaluate",
                details={
                    "clock_ms": clock_ms,
                    "watermark_ms": watermark,
                    "tolerance_ms": tolerance_ms,
                    "rollback_by_ms": watermark - clock_ms,
                    "allowed_rollback_ms": tolerance_ms,
                },
            )
        return {"clock_ms": clock_ms, "watermark_ms": watermark}

    def get_current_descriptor(self) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT vv.*
                FROM state s
                JOIN view_versions vv ON vv.view_id = s.value
                WHERE s.key='current_view_id' AND s.value <> ''
                """
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "view_id": row["view_id"],
            "package_key": row["package_key"],
            "manifest_version": row["manifest_version"],
            "generation_min": int(row["generation_min"]),
            "shard_hashes": json.loads(row["shard_digest_json"]),
            "parameter_digest": row["parameter_digest"],
            "input_digest": row["input_digest"],
            "change_kind": row["change_kind"],
            "committed_at_ms": int(row["committed_at_ms"]),
            "commit_seq": int(row["commit_seq"]),
        }

    def get_view_by_package_key(self, package_key: str) -> dict[str, Any] | None:
        return self._get_view(package_key=package_key, current_only=False)

    def get_current_view(self) -> dict[str, Any] | None:
        return self._get_view(package_key=None, current_only=True)

    def _get_view(
        self,
        *,
        package_key: str | None,
        current_only: bool,
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            if current_only:
                row = connection.execute(
                    """
                    SELECT vv.*
                    FROM state s
                    JOIN view_versions vv ON vv.view_id = s.value
                    WHERE s.key='current_view_id' AND s.value <> ''
                    """
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM view_versions WHERE package_key=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (package_key,),
                ).fetchone()
            if row is None:
                return None
            blob = connection.execute(
                "SELECT payload_json FROM view_blobs WHERE view_id=? AND kind='view'",
                (row["view_id"],),
            ).fetchone()
            if blob is None:
                raise EngineError(
                    "E_STATE_CORRUPT",
                    "committed view has no full view blob",
                    stage="persist",
                    details={"view_id": row["view_id"]},
                )
            view = json.loads(blob["payload_json"])
            view["persisted"] = {
                "view_id": row["view_id"],
                "package_key": row["package_key"],
                "commit_seq": int(row["commit_seq"]),
                "committed_at_ms": int(row["committed_at_ms"]),
            }
            return view
        finally:
            connection.close()

    def get_running_operation(self, package_key: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM operations
                WHERE package_key=? AND status='running'
                ORDER BY rowid DESC LIMIT 1
                """,
                (package_key,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else dict(row)

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM operations WHERE op_id=?",
                (op_id,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else dict(row)

    def create_operation(
        self,
        *,
        op_id: str,
        package_key: str,
        clock_ms: int,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM operation_steps WHERE op_id=?",
                (op_id,),
            )
            connection.execute(
                """
                INSERT INTO operations(
                    op_id, package_key, status, clock_ms,
                    created_at_ms, updated_at_ms, error_json
                ) VALUES (?, ?, 'running', ?, ?, ?, NULL)
                ON CONFLICT(op_id) DO UPDATE SET
                    status='running',
                    clock_ms=excluded.clock_ms,
                    updated_at_ms=excluded.updated_at_ms,
                    error_json=NULL
                """,
                (op_id, package_key, clock_ms, clock_ms, clock_ms),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise EngineError(
                "E_STATE_CORRUPT",
                "unable to create operation journal",
                stage="persist",
                details={"reason": str(exc)},
            ) from exc
        finally:
            connection.close()

    def get_step(self, op_id: str, step_name: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT output_json, status, completed_at_ms, replay_count
                FROM operation_steps WHERE op_id=? AND step_name=?
                """,
                (op_id, step_name),
            ).fetchone()
            if row is None or row["status"] != "complete":
                return None
            connection.execute(
                """
                UPDATE operation_steps
                SET replay_count=replay_count+1
                WHERE op_id=? AND step_name=?
                """,
                (op_id, step_name),
            )
            connection.commit()
            return {
                "output": json.loads(row["output_json"]),
                "completed_at_ms": int(row["completed_at_ms"]),
                "replay_count": int(row["replay_count"]) + 1,
            }
        finally:
            connection.close()

    def save_step(
        self,
        *,
        op_id: str,
        step_name: str,
        output: dict[str, Any],
        clock_ms: int,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO operation_steps(
                    op_id, step_name, status, output_json,
                    completed_at_ms, replay_count
                ) VALUES (?, ?, 'complete', ?, ?, 0)
                ON CONFLICT(op_id, step_name) DO UPDATE SET
                    status='complete',
                    output_json=excluded.output_json,
                    completed_at_ms=excluded.completed_at_ms
                """,
                (op_id, step_name, canonical_json(output), clock_ms),
            )
            connection.execute(
                "UPDATE operations SET updated_at_ms=? WHERE op_id=?",
                (clock_ms, op_id),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise EngineError(
                "E_STATE_CORRUPT",
                "unable to persist operation step",
                stage="persist",
                details={"op_id": op_id, "step": step_name, "reason": str(exc)},
            ) from exc
        finally:
            connection.close()

    def fail_operation(self, op_id: str, clock_ms: int, error: EngineError) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE operations
                SET status='failed', updated_at_ms=?, error_json=?
                WHERE op_id=?
                """,
                (clock_ms, canonical_json(error.to_dict()), op_id),
            )
            connection.commit()
        finally:
            connection.close()

    def complete_operation(self, op_id: str, clock_ms: int) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE operations
                SET status='complete', updated_at_ms=?, error_json=NULL
                WHERE op_id=?
                """,
                (clock_ms, op_id),
            )
            connection.commit()
        finally:
            connection.close()

    def stage_view(
        self,
        *,
        op_id: str,
        step_name: str,
        view_id: str,
        package_key: str,
        descriptor: dict[str, Any],
        change_decision: dict[str, Any],
        view: dict[str, Any],
        clock_ms: int,
    ) -> None:
        staging_id = view_id
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table in (
                "staging_services",
                "staging_blobs",
                "staging_versions",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE staging_id=?",
                    (staging_id,),
                )
            connection.execute(
                """
                INSERT INTO staging_versions(
                    staging_id, view_id, package_key,
                    descriptor_json, change_decision_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    staging_id,
                    view_id,
                    package_key,
                    canonical_json(descriptor),
                    canonical_json(change_decision),
                    clock_ms,
                ),
            )
            for environment, services in view["environments"].items():
                for service, payload in services.items():
                    connection.execute(
                        """
                        INSERT INTO staging_services(
                            staging_id, environment, service, payload_json
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            staging_id,
                            environment,
                            service,
                            canonical_json(payload),
                        ),
                    )
            blobs = {
                "view": view,
                "merge_decisions": view["merge_decisions"],
                "migrations": view["migrations"],
                "compatibility_degradations": view["compatibility_degradations"],
                "consistency": view["consistency"],
                "derivations": view["derivations"],
                "conflicts": view["conflicts"],
                "change_decision": change_decision,
            }
            for kind, payload in blobs.items():
                connection.execute(
                    """
                    INSERT INTO staging_blobs(staging_id, kind, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (staging_id, kind, canonical_json(payload)),
                )
            connection.execute(
                """
                INSERT INTO operation_steps(
                    op_id, step_name, status, output_json,
                    completed_at_ms, replay_count
                ) VALUES (?, ?, 'complete', ?, ?, 0)
                ON CONFLICT(op_id, step_name) DO UPDATE SET
                    status='complete',
                    output_json=excluded.output_json,
                    completed_at_ms=excluded.completed_at_ms
                """,
                (
                    op_id,
                    step_name,
                    canonical_json(
                        {
                            "view_id": view_id,
                            "package_key": package_key,
                            "staging_id": staging_id,
                        }
                    ),
                    clock_ms,
                ),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise EngineError(
                "E_STATE_CORRUPT",
                "unable to stage replacement view",
                stage="persist",
                details={"view_id": view_id, "reason": str(exc)},
            ) from exc
        finally:
            connection.close()

    def switch_view(
        self,
        *,
        op_id: str,
        step_name: str,
        view_id: str,
        package_key: str,
        clock_ms: int,
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT vv.view_id, vv.commit_seq
                FROM state s
                JOIN view_versions vv ON vv.view_id=s.value
                WHERE s.key='current_view_id'
                  AND s.value <> ''
                  AND vv.package_key=?
                """,
                (package_key,),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "DELETE FROM staging_services WHERE staging_id=?",
                    (view_id,),
                )
                connection.execute(
                    "DELETE FROM staging_blobs WHERE staging_id=?",
                    (view_id,),
                )
                connection.execute(
                    "DELETE FROM staging_versions WHERE staging_id=?",
                    (view_id,),
                )
                self._complete_step_in_transaction(
                    connection,
                    op_id=op_id,
                    step_name=step_name,
                    output={
                        "view_id": existing["view_id"],
                        "package_key": package_key,
                        "idempotent": True,
                    },
                    clock_ms=clock_ms,
                )
                connection.commit()
                return {
                    "view_id": existing["view_id"],
                    "commit_seq": int(existing["commit_seq"]),
                    "idempotent": True,
                }
            staging = connection.execute(
                "SELECT * FROM staging_versions WHERE staging_id=?",
                (view_id,),
            ).fetchone()
            if staging is None:
                raise EngineError(
                    "E_STATE_CORRUPT",
                    "staged view is missing during switch",
                    stage="persist",
                    details={"view_id": view_id},
                )
            descriptor = json.loads(staging["descriptor_json"])
            change_decision = json.loads(staging["change_decision_json"])
            next_seq = int(
                connection.execute(
                    "SELECT COALESCE(MAX(commit_seq), 0) + 1 FROM view_versions"
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO view_versions(
                    view_id, package_key, manifest_version, generation_min,
                    shard_digest_json, parameter_digest, input_digest,
                    status, change_kind, decision_json,
                    created_at_ms, committed_at_ms, commit_seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'committed', ?, ?, ?, ?, ?)
                """,
                (
                    view_id,
                    package_key,
                    descriptor["manifest_version"],
                    descriptor["generation_min"],
                    canonical_json(descriptor["shard_hashes"]),
                    descriptor["parameter_digest"],
                    descriptor["input_digest"],
                    change_decision["change_kind"],
                    canonical_json(change_decision),
                    int(staging["created_at_ms"]),
                    clock_ms,
                    next_seq,
                ),
            )
            connection.execute(
                """
                INSERT INTO view_services(view_id, environment, service, payload_json)
                SELECT ?, environment, service, payload_json
                FROM staging_services WHERE staging_id=?
                """,
                (view_id, view_id),
            )
            connection.execute(
                """
                INSERT INTO view_blobs(view_id, kind, payload_json)
                SELECT ?, kind, payload_json
                FROM staging_blobs WHERE staging_id=?
                """,
                (view_id, view_id),
            )
            connection.execute(
                "UPDATE state SET value=? WHERE key='current_view_id'",
                (view_id,),
            )
            connection.execute(
                """
                UPDATE clock_state
                SET watermark_ms=MAX(watermark_ms, ?)
                WHERE singleton=1
                """,
                (clock_ms,),
            )
            connection.execute(
                "DELETE FROM staging_services WHERE staging_id=?",
                (view_id,),
            )
            connection.execute(
                "DELETE FROM staging_blobs WHERE staging_id=?",
                (view_id,),
            )
            connection.execute(
                "DELETE FROM staging_versions WHERE staging_id=?",
                (view_id,),
            )
            self._complete_step_in_transaction(
                connection,
                op_id=op_id,
                step_name=step_name,
                output={
                    "view_id": view_id,
                    "package_key": package_key,
                    "commit_seq": next_seq,
                    "idempotent": False,
                },
                clock_ms=clock_ms,
            )
            connection.commit()
            return {
                "view_id": view_id,
                "commit_seq": next_seq,
                "idempotent": False,
            }
        except EngineError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise EngineError(
                "E_STATE_CORRUPT",
                "unable to atomically switch current view pointer",
                stage="persist",
                details={"view_id": view_id, "reason": str(exc)},
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _complete_step_in_transaction(
        connection: sqlite3.Connection,
        *,
        op_id: str,
        step_name: str,
        output: dict[str, Any],
        clock_ms: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO operation_steps(
                op_id, step_name, status, output_json,
                completed_at_ms, replay_count
            ) VALUES (?, ?, 'complete', ?, ?, 0)
            ON CONFLICT(op_id, step_name) DO UPDATE SET
                status='complete',
                output_json=excluded.output_json,
                completed_at_ms=excluded.completed_at_ms
            """,
            (op_id, step_name, canonical_json(output), clock_ms),
        )
        connection.execute(
            "UPDATE operations SET updated_at_ms=? WHERE op_id=?",
            (clock_ms, op_id),
        )

    def status(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            view_row = connection.execute(
                """
                SELECT vv.view_id, vv.package_key, vv.manifest_version,
                       vv.commit_seq, vv.committed_at_ms
                FROM state s
                JOIN view_versions vv ON vv.view_id=s.value
                WHERE s.key='current_view_id' AND s.value <> ''
                """
            ).fetchone()
            clock = connection.execute(
                "SELECT watermark_ms FROM clock_state WHERE singleton=1"
            ).fetchone()
            operations = connection.execute(
                """
                SELECT op_id, package_key, status, clock_ms, error_json
                FROM operations ORDER BY rowid
                """
            ).fetchall()
            steps = connection.execute(
                """
                SELECT op_id, step_name, status, replay_count
                FROM operation_steps ORDER BY op_id, rowid
                """
            ).fetchall()
        finally:
            connection.close()
        return {
            "current_view": None if view_row is None else dict(view_row),
            "clock_watermark_ms": int(clock["watermark_ms"]),
            "operations": [dict(row) for row in operations],
            "steps": [dict(row) for row in steps],
        }
