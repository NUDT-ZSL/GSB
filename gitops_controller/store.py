"""SQLite-backed transactional state for merge cache, plans, and recovery."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .project import Project
from .util import canonical_json

RUNTIME_SCHEMA_VERSION = 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merges (
    merge_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS desired_states (
    environment TEXT PRIMARY KEY,
    merge_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    injected_clock_ms INTEGER NOT NULL,
    updated_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_decisions (
    decision_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    app TEXT NOT NULL,
    source_format TEXT NOT NULL,
    normalized_absolute_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS environment_state (
    environment TEXT PRIMARY KEY,
    logical_clock_ms INTEGER NOT NULL,
    last_plan_id TEXT,
    last_run_id TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    budget_ms INTEGER NOT NULL,
    operation_cost_ms INTEGER NOT NULL,
    clock_start_ms INTEGER NOT NULL,
    clock_end_ms INTEGER NOT NULL,
    owner_pid INTEGER NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    app TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    target_revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    started INTEGER NOT NULL,
    logical_start_ms INTEGER,
    logical_end_ms INTEGER,
    outcome_json TEXT NOT NULL,
    reason TEXT,
    owner_pid INTEGER,
    PRIMARY KEY (run_id, sequence),
    UNIQUE (run_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS commits (
    idempotency_key TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    app TEXT NOT NULL,
    target_revision INTEGER NOT NULL,
    target_digest TEXT NOT NULL,
    image_version TEXT NOT NULL,
    replicas INTEGER NOT NULL,
    action TEXT NOT NULL,
    committed_clock_ms INTEGER NOT NULL,
    result_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commits_env_app
    ON commits(environment, app);
CREATE INDEX IF NOT EXISTS idx_runs_env_status
    ON runs(environment, status);
CREATE INDEX IF NOT EXISTS idx_operations_run_status
    ON operations(run_id, status);
"""


class Store:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.path = project.runtime_db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        connection = self._connect()
        try:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                pass
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO runtime_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(RUNTIME_SCHEMA_VERSION)),
            )
            connection.execute(
                "INSERT OR IGNORE INTO runtime_meta(key, value) VALUES(?, ?)",
                ("merge_seq", "0"),
            )
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _next_sequence(self, connection: sqlite3.Connection, key: str) -> int:
        row = connection.execute(
            "SELECT value FROM runtime_meta WHERE key = ?",
            (key,),
        ).fetchone()
        value = int(row["value"]) + 1 if row is not None else 1
        connection.execute(
            "INSERT INTO runtime_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        return value

    def get_meta(self, key: str) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM runtime_meta WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row is not None else None
        finally:
            connection.close()

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO runtime_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def max_known_clock_ms(self) -> int:
        value = self.get_meta("load_clock_watermark_ms")
        return int(value) if value is not None else self.project.initial_clock_ms

    def register_merge(
        self,
        desired_states: dict[str, dict[str, Any]],
        migration_decisions: list[dict[str, Any]],
        injected_clock_ms: int,
    ) -> dict[str, bool]:
        """Atomically cache merges and only rewrite changed desired state."""

        inserted: dict[str, bool] = {}
        with self.transaction() as connection:
            for environment in self.project.environments:
                payload = desired_states[environment]
                merge_key = payload["merge_key"]
                existing = connection.execute(
                    "SELECT 1 FROM merges WHERE merge_key = ?",
                    (merge_key,),
                ).fetchone()
                inserted[environment] = existing is None
                if existing is None:
                    sequence = self._next_sequence(connection, "merge_seq")
                    connection.execute(
                        "INSERT INTO merges(merge_key, payload_json, created_seq) "
                        "VALUES(?, ?, ?)",
                        (merge_key, canonical_json(payload), sequence),
                    )
                current = connection.execute(
                    "SELECT merge_key FROM desired_states WHERE environment = ?",
                    (environment,),
                ).fetchone()
                if current is None or current["merge_key"] != merge_key:
                    sequence = self._next_sequence(connection, "merge_seq")
                    connection.execute(
                        """
                        INSERT INTO desired_states(
                            environment, merge_key, payload_json,
                            injected_clock_ms, updated_seq
                        ) VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(environment) DO UPDATE SET
                            merge_key = excluded.merge_key,
                            payload_json = excluded.payload_json,
                            injected_clock_ms = excluded.injected_clock_ms,
                            updated_seq = excluded.updated_seq
                        """,
                        (
                            environment,
                            merge_key,
                            canonical_json(payload),
                            injected_clock_ms,
                            sequence,
                        ),
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO environment_state(
                        environment, logical_clock_ms
                    ) VALUES(?, ?)
                    """,
                    (environment, injected_clock_ms),
                )
                connection.execute(
                    """
                    UPDATE environment_state
                    SET logical_clock_ms = MAX(logical_clock_ms, ?)
                    WHERE environment = ?
                    """,
                    (injected_clock_ms, environment),
                )
            for decision in migration_decisions:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO migration_decisions(
                        decision_id, environment, app, source_format,
                        normalized_absolute_ms, payload_json
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision["decision_id"],
                        decision["environment"],
                        decision["app"],
                        decision["source_format"],
                        decision["absolute_effective_time_ms"],
                        canonical_json(decision),
                    ),
                )
            connection.execute(
                """
                INSERT INTO runtime_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = CAST(MAX(CAST(value AS INTEGER), ?) AS TEXT)
                """,
                ("load_clock_watermark_ms", str(injected_clock_ms), injected_clock_ms),
            )
        return inserted

    def get_desired(self, environment: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload_json FROM desired_states WHERE environment = ?",
                (environment,),
            ).fetchone()
            return json.loads(row["payload_json"]) if row is not None else None
        finally:
            connection.close()

    def get_all_desired(self) -> dict[str, dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT environment, payload_json FROM desired_states ORDER BY environment"
            ).fetchall()
            return {row["environment"]: json.loads(row["payload_json"]) for row in rows}
        finally:
            connection.close()

    def migration_count(self, environment: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM migration_decisions WHERE environment = ?",
                (environment,),
            ).fetchone()
            return int(row["count"])
        finally:
            connection.close()

    def get_env_clock(self, environment: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT logical_clock_ms FROM environment_state WHERE environment = ?",
                (environment,),
            ).fetchone()
            return (
                int(row["logical_clock_ms"])
                if row is not None
                else self.project.initial_clock_ms
            )
        finally:
            connection.close()

    def get_environment_state(self, environment: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT environment, logical_clock_ms, last_plan_id, last_run_id
                FROM environment_state WHERE environment = ?
                """,
                (environment,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def save_plan(self, plan: dict[str, Any]) -> None:
        with self.transaction() as connection:
            sequence = self._next_sequence(connection, "merge_seq")
            connection.execute(
                """
                INSERT INTO plans(
                    plan_id, environment, plan_digest, payload_json, created_seq
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    plan_digest = excluded.plan_digest
                """,
                (
                    plan["plan_id"],
                    plan["environment"],
                    plan["plan_digest"],
                    canonical_json(plan),
                    sequence,
                ),
            )
            connection.execute(
                """
                UPDATE environment_state SET last_plan_id = ?
                WHERE environment = ?
                """,
                (plan["plan_id"], plan["environment"]),
            )

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload_json FROM plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            return json.loads(row["payload_json"]) if row is not None else None
        finally:
            connection.close()

    def create_or_get_run(
        self,
        *,
        run_id: str,
        plan: dict[str, Any],
        operations: list[dict[str, Any]],
        budget_ms: int,
        operation_cost_ms: int,
        clock_start_ms: int,
        owner_pid: int,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            active = connection.execute(
                """
                SELECT * FROM runs
                WHERE environment = ? AND plan_digest = ?
                  AND status IN ('running', 'partial', 'failed')
                ORDER BY created_seq ASC LIMIT 1
                """,
                (plan["environment"], plan["plan_digest"]),
            ).fetchone()
            if active is not None:
                return dict(active)
            existing = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is None:
                sequence = self._next_sequence(connection, "merge_seq")
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, environment, plan_id, plan_digest, status,
                        budget_ms, operation_cost_ms, clock_start_ms,
                        clock_end_ms, owner_pid, created_seq
                    ) VALUES(?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        plan["environment"],
                        plan["plan_id"],
                        plan["plan_digest"],
                        budget_ms,
                        operation_cost_ms,
                        clock_start_ms,
                        clock_start_ms,
                        owner_pid,
                        sequence,
                    ),
                )
                for operation in operations:
                    connection.execute(
                        """
                        INSERT INTO operations(
                            run_id, sequence, app, idempotency_key,
                            status, phase, target_revision, payload_json,
                            started, outcome_json
                        ) VALUES(?, ?, ?, ?, 'pending', 'pending', ?, ?, 0, ?)
                        """,
                        (
                            run_id,
                            operation["sequence"],
                            operation["app"],
                            operation["idempotency_key"],
                            operation["target"]["manifest_revision"],
                            canonical_json(operation),
                            canonical_json(
                                {
                                    "reason": None,
                                    "started": False,
                                    "status": "pending",
                                }
                            ),
                        ),
                    )
                connection.execute(
                    """
                    UPDATE environment_state SET last_run_id = ?
                    WHERE environment = ?
                    """,
                    (run_id, plan["environment"]),
                )
                existing = connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            return dict(existing)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def get_latest_incomplete_run(self, environment: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE environment = ? AND status IN ('running', 'partial', 'failed')
                ORDER BY created_seq DESC LIMIT 1
                """,
                (environment,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def get_latest_run(self, environment: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM runs WHERE environment = ?
                ORDER BY created_seq DESC LIMIT 1
                """,
                (environment,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def get_operations(self, run_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM operations WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                item["outcome"] = json.loads(item.pop("outcome_json"))
                item["started"] = bool(item["started"])
                result.append(item)
            return result
        finally:
            connection.close()

    def get_operation(self, run_id: str, sequence: int) -> dict[str, Any] | None:
        operations = self.get_operations(run_id)
        for operation in operations:
            if operation["sequence"] == sequence:
                return operation
        return None

    def claim_operation(
        self,
        run_id: str,
        sequence: int,
        *,
        owner_pid: int,
        start_clock_ms: int,
    ) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status, owner_pid FROM operations WHERE run_id = ? AND sequence = ?",
                (run_id, sequence),
            ).fetchone()
            if row is None or row["status"] in ("committed", "failed", "abandoned"):
                return False
            if row["status"] == "running" and row["owner_pid"] != owner_pid:
                return False
            connection.execute(
                """
                UPDATE operations SET
                    status = 'running', phase = 'started', started = 1,
                    logical_start_ms = COALESCE(logical_start_ms, ?),
                    owner_pid = ?
                WHERE run_id = ? AND sequence = ?
                """,
                (start_clock_ms, owner_pid, run_id, sequence),
            )
            return True

    def mark_operation(
        self,
        run_id: str,
        sequence: int,
        *,
        status: str,
        phase: str,
        started: bool,
        outcome_status: str | None = None,
        reason: str | None,
        logical_end_ms: int | None,
        advance_clock_to_ms: int | None = None,
    ) -> None:
        outcome = {
            "reason": reason,
            "started": started,
            "status": outcome_status or status,
        }
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE operations SET
                    status = ?, phase = ?, started = ?,
                    logical_end_ms = COALESCE(?, logical_end_ms),
                    reason = ?, outcome_json = ?
                WHERE run_id = ? AND sequence = ?
                """,
                (
                    status,
                    phase,
                    1 if started else 0,
                    logical_end_ms,
                    reason,
                    canonical_json(outcome),
                    run_id,
                    sequence,
                ),
            )
            if advance_clock_to_ms is not None:
                row = connection.execute(
                    "SELECT environment FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is not None:
                    connection.execute(
                        """
                        UPDATE environment_state
                        SET logical_clock_ms = MAX(logical_clock_ms, ?)
                        WHERE environment = ?
                        """,
                        (advance_clock_to_ms, row["environment"]),
                    )

    def finalize_commit(
        self,
        *,
        run_id: str,
        operation: dict[str, Any],
        committed_clock_ms: int,
        result: dict[str, Any],
    ) -> bool:
        payload = operation["payload"]
        target = payload["target"]
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT environment FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                return False
            existing = connection.execute(
                "SELECT 1 FROM commits WHERE idempotency_key = ?",
                (payload["idempotency_key"],),
            ).fetchone()
            inserted = existing is None
            if inserted:
                connection.execute(
                    """
                    INSERT INTO commits(
                        idempotency_key, environment, app, target_revision,
                        target_digest, image_version, replicas, action,
                        committed_clock_ms, result_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["idempotency_key"],
                        run["environment"],
                        operation["app"],
                        target["manifest_revision"],
                        payload["target_digest"],
                        target["image_version"],
                        target["replicas"],
                        payload["action"],
                        committed_clock_ms,
                        canonical_json(result),
                    ),
                )
            connection.execute(
                """
                UPDATE operations SET
                    status = 'committed', phase = 'committed',
                    logical_end_ms = COALESCE(logical_end_ms, ?),
                    reason = NULL,
                    outcome_json = ?
                WHERE run_id = ? AND sequence = ?
                """,
                (
                    committed_clock_ms,
                    canonical_json(
                        {
                            "reason": None,
                            "started": True,
                            "status": "committed",
                        }
                    ),
                    run_id,
                    operation["sequence"],
                ),
            )
            connection.execute(
                """
                UPDATE environment_state
                SET logical_clock_ms = MAX(logical_clock_ms, ?)
                WHERE environment = ?
                """,
                (committed_clock_ms, run["environment"]),
            )
            return inserted

    def get_commit(self, idempotency_key: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM commits WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json"))
            return item
        finally:
            connection.close()

    def max_committed_revision(self, environment: str, app: str) -> int | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT MAX(target_revision) AS revision
                FROM commits WHERE environment = ? AND app = ?
                """,
                (environment, app),
            ).fetchone()
            return int(row["revision"]) if row["revision"] is not None else None
        finally:
            connection.close()

    def committed_revisions(self, environment: str) -> dict[str, int]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT app, MAX(target_revision) AS revision
                FROM commits WHERE environment = ?
                GROUP BY app ORDER BY app
                """,
                (environment,),
            ).fetchall()
            return {row["app"]: int(row["revision"]) for row in rows}
        finally:
            connection.close()

    def list_commits(self, environment: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT idempotency_key, app, target_revision, target_digest,
                       image_version, replicas, action, committed_clock_ms
                FROM commits WHERE environment = ?
                ORDER BY app, target_revision, idempotency_key
                """,
                (environment,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        clock_end_ms: int,
        owner_pid: int | None = None,
    ) -> None:
        with self.transaction() as connection:
            if owner_pid is None:
                connection.execute(
                    """
                    UPDATE runs SET status = ?, clock_end_ms = ?
                    WHERE run_id = ?
                    """,
                    (status, clock_end_ms, run_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE runs SET status = ?, clock_end_ms = ?, owner_pid = ?
                    WHERE run_id = ?
                    """,
                    (status, clock_end_ms, owner_pid, run_id),
                )

    def open_operation_count(self, environment: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM operations o
                JOIN runs r ON r.run_id = o.run_id
                WHERE r.environment = ?
                  AND o.status IN ('pending', 'running', 'not_started')
                """,
                (environment,),
            ).fetchone()
            return int(row["count"])
        finally:
            connection.close()
