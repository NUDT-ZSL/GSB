from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .errors import EngineError, Issue
from .jsonutil import canonical_json, sha256_hex
from .model import EngineConfig, Resource

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS desired_objects (
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    body_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (kind, namespace, name)
);

CREATE TABLE IF NOT EXISTS actual_objects (
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    body_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (kind, namespace, name)
);

CREATE TABLE IF NOT EXISTS validation_issues (
    ordinal INTEGER PRIMARY KEY,
    issue_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS object_state (
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    desired_exists INTEGER NOT NULL,
    actual_exists INTEGER NOT NULL,
    phase TEXT NOT NULL,
    comparison_mode TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    abandoned INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    next_retry_at_ms INTEGER,
    reason_json TEXT NOT NULL DEFAULT '[]',
    updated_round INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, namespace, name)
);

CREATE TABLE IF NOT EXISTS rounds (
    round_no INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    ended_ms INTEGER,
    planned_count INTEGER NOT NULL DEFAULT 0,
    started_count INTEGER NOT NULL DEFAULT 0,
    committed_count INTEGER NOT NULL DEFAULT 0,
    deferred_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    abandoned_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    committed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS operations (
    round_no INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    action TEXT NOT NULL,
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    depends_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    started_ms INTEGER,
    finished_ms INTEGER,
    next_retry_at_ms INTEGER,
    result_json TEXT,
    committed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (round_no, operation_id),
    FOREIGN KEY (round_no) REFERENCES rounds(round_no)
);

CREATE TABLE IF NOT EXISTS committed_operations (
    operation_id TEXT PRIMARY KEY,
    round_no INTEGER NOT NULL,
    result_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    logical_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS concurrency_slots (
    slot INTEGER PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    namespace TEXT NOT NULL,
    started_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operations_status
ON operations(round_no, status);

CREATE INDEX IF NOT EXISTS idx_rounds_status
ON rounds(status);
"""


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        try:
            self.connection = sqlite3.connect(
                path, timeout=30, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise EngineError(
                "E_DB_OPEN", f"cannot open SQLite database {path}: {exc}"
            ) from exc
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.executescript(SCHEMA)
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(object_state)"
            )
        }
        if "generation" not in columns:
            self.connection.execute(
                "ALTER TABLE object_state "
                "ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set_meta(self, key: str, value: Any) -> None:
        self.connection.execute(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, canonical_json(value)),
        )

    def require_loaded(self) -> None:
        if self.get_meta("initialized") is not True:
            raise EngineError(
                "E_NOT_LOADED",
                "state database is not initialized; run the load command first",
            )

    def config(self) -> EngineConfig:
        raw = self.get_meta("config")
        if raw is None:
            raise EngineError("E_NOT_LOADED", "configuration is missing")
        return EngineConfig.from_dict(raw)

    def initialize(
        self,
        desired: dict[str, Resource],
        actual: dict[str, Resource],
        config: EngineConfig,
        issues: list[Issue],
    ) -> None:
        with self.transaction():
            for table in (
                "concurrency_slots",
                "committed_operations",
                "operations",
                "rounds",
                "object_state",
                "validation_issues",
                "actual_objects",
                "desired_objects",
                "meta",
            ):
                self.connection.execute(f"DELETE FROM {table}")
            self.set_meta("initialized", True)
            self.set_meta("config", config.to_dict())
            self.set_meta("logical_clock_ms", config.initial_logical_ms)
            self.set_meta("round_watermark", 0)
            self.set_meta("next_round_no", 1)
            self.set_meta("active_operations", 0)
            self.set_meta("recovery_count", 0)
            self.set_meta("last_comparison_mode", "exact")
            self.set_meta("state", "loaded")
            for resource in sorted(
                desired.values(), key=lambda item: item.sort_key
            ):
                self.connection.execute(
                    """
                    INSERT INTO desired_objects(
                        kind, namespace, name, body_json, digest
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        resource.kind,
                        resource.namespace,
                        resource.name,
                        canonical_json(resource.body),
                        resource.digest,
                    ),
                )
            for resource in sorted(
                actual.values(), key=lambda item: item.sort_key
            ):
                self.connection.execute(
                    """
                    INSERT INTO actual_objects(
                        kind, namespace, name, body_json, digest
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        resource.kind,
                        resource.namespace,
                        resource.name,
                        canonical_json(resource.body),
                        resource.digest,
                    ),
                )
            for index, issue in enumerate(
                sorted(issues, key=Issue.sort_key)
            ):
                self.connection.execute(
                    """
                    INSERT INTO validation_issues(ordinal, issue_json)
                    VALUES(?, ?)
                    """,
                    (index, canonical_json(issue.to_dict())),
                )
            for key in sorted(set(desired) | set(actual)):
                kind, namespace, name = key.split("/", 2)
                self.connection.execute(
                    """
                    INSERT INTO object_state(
                        kind, namespace, name, desired_exists, actual_exists,
                        phase, comparison_mode, updated_round
                    ) VALUES(?, ?, ?, ?, ?, 'pending', 'exact', 0)
                    """,
                    (
                        kind,
                        namespace,
                        name,
                        int(key in desired),
                        int(key in actual),
                    ),
                )

    def desired(self) -> dict[str, Resource]:
        return self._load_resources("desired_objects")

    def actual(self) -> dict[str, Resource]:
        return self._load_resources("actual_objects")

    def _load_resources(self, table: str) -> dict[str, Resource]:
        rows = self.connection.execute(
            f"""
            SELECT kind, namespace, name, body_json, digest
            FROM {table}
            ORDER BY namespace, kind, name
            """
        ).fetchall()
        resources: dict[str, Resource] = {}
        for row in rows:
            body = json.loads(row["body_json"])
            resource = Resource(
                row["kind"], row["namespace"], row["name"], body
            )
            if resource.digest != row["digest"]:
                raise EngineError(
                    "E_STATE_CORRUPT",
                    f"resource digest mismatch in {table}: {resource.key}",
                )
            resources[resource.key] = resource
        return resources

    def issues(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT issue_json FROM validation_issues ORDER BY ordinal"
        ).fetchall()
        return [json.loads(row["issue_json"]) for row in rows]

    def clock(self) -> int:
        return int(self.get_meta("logical_clock_ms", 0))

    def round_watermark(self) -> int:
        return int(self.get_meta("round_watermark", 0))

    def allocate_round(self, started_ms: int) -> int:
        with self.transaction():
            round_no = int(self.get_meta("next_round_no", 1))
            self.set_meta("next_round_no", round_no + 1)
            self.connection.execute(
                """
                INSERT INTO rounds(round_no, status, started_ms)
                VALUES(?, 'executing', ?)
                """,
                (round_no, started_ms),
            )
            return round_no

    def store_round_operations(
        self, round_no: int, operations: list[dict[str, Any]]
    ) -> None:
        with self.transaction():
            for operation in operations:
                identity = operation["identity"]
                self.connection.execute(
                    """
                    INSERT INTO operations(
                        round_no, operation_id, action, kind, namespace, name,
                        payload_json, depends_json, status
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'planned')
                    ON CONFLICT(round_no, operation_id) DO NOTHING
                    """,
                    (
                        round_no,
                        operation["operation_id"],
                        operation["action"],
                        identity["kind"],
                        identity["namespace"],
                        identity["name"],
                        canonical_json(operation["payload"]),
                        canonical_json(operation["depends_on"]),
                    ),
                )

    def reserve_slot(
        self,
        *,
        round_no: int,
        operation: dict[str, Any],
        started_ms: int,
        max_concurrency: int,
        attempt: int,
    ) -> int:
        with self.transaction():
            slots = {
                row["slot"]
                for row in self.connection.execute(
                    "SELECT slot FROM concurrency_slots"
                )
            }
            free_slot = next(
                (
                    slot
                    for slot in range(max_concurrency)
                    if slot not in slots
                ),
                None,
            )
            if free_slot is None:
                raise EngineError(
                    "E_CONCURRENCY",
                    "logical concurrency slot reservation overflow",
                )
            identity = operation["identity"]
            self.connection.execute(
                """
                UPDATE operations
                SET status = 'started', attempt = ?, started_ms = ?
                WHERE round_no = ? AND operation_id = ?
                """,
                (
                    attempt,
                    started_ms,
                    round_no,
                    operation["operation_id"],
                ),
            )
            self.connection.execute(
                """
                INSERT INTO concurrency_slots(
                    slot, operation_id, namespace, started_ms
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    free_slot,
                    operation["operation_id"],
                    identity["namespace"],
                    started_ms,
                ),
            )
            self.set_meta(
                "active_operations",
                int(
                    self.connection.execute(
                        "SELECT COUNT(*) AS count FROM concurrency_slots"
                    ).fetchone()["count"]
                ),
            )
            self.connection.execute(
                """
                UPDATE object_state
                SET phase = 'converging', updated_round = ?
                WHERE kind = ? AND namespace = ? AND name = ?
                """,
                (
                    round_no,
                    identity["kind"],
                    identity["namespace"],
                    identity["name"],
                ),
            )
            return free_slot

    def release_slot(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> None:
        connection.execute(
            "DELETE FROM concurrency_slots WHERE operation_id = ?",
            (operation_id,),
        )
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM concurrency_slots"
        ).fetchone()["count"]
        self.set_meta("active_operations", int(count))

    def committed_result(self, operation_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT result_json, result_digest
            FROM committed_operations
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        result = json.loads(row["result_json"])
        if sha256_hex(result) != row["result_digest"]:
            raise EngineError(
                "E_STATE_CORRUPT",
                f"committed result digest mismatch: {operation_id}",
            )
        return result

    def commit_operation(
        self,
        *,
        round_no: int,
        operation: dict[str, Any],
        finished_ms: int,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        operation_id = operation["operation_id"]
        with self.transaction():
            existing = self.committed_result(operation_id)
            if existing is not None:
                self.connection.execute(
                    """
                    UPDATE operations
                    SET status = 'committed', finished_ms = ?,
                        result_json = ?, committed = 1
                    WHERE round_no = ? AND operation_id = ?
                    """,
                    (
                        finished_ms,
                        canonical_json(existing),
                        round_no,
                        operation_id,
                    ),
                )
                self.release_slot(self.connection, operation_id)
                return {
                    **existing,
                    "idempotent_replay": True,
                }
            self._apply_side_effect(operation)
            result_digest = sha256_hex(result)
            self.connection.execute(
                """
                INSERT INTO committed_operations(
                    operation_id, round_no, result_digest, result_json,
                    logical_ms
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    round_no,
                    result_digest,
                    canonical_json(result),
                    finished_ms,
                ),
            )
            self.connection.execute(
                """
                UPDATE operations
                SET status = 'committed', finished_ms = ?, result_json = ?,
                    committed = 1
                WHERE round_no = ? AND operation_id = ?
                """,
                (
                    finished_ms,
                    canonical_json(result),
                    round_no,
                    operation_id,
                ),
            )
            identity = operation["identity"]
            self.connection.execute(
                """
                UPDATE object_state
                SET failures = 0, abandoned = 0, next_retry_at_ms = NULL,
                    generation = generation + ?
                WHERE kind = ? AND namespace = ? AND name = ?
                """,
                (
                    int(
                        operation["action"]
                        in ("create", "update", "delete")
                    ),
                    identity["kind"],
                    identity["namespace"],
                    identity["name"],
                ),
            )
            self.release_slot(self.connection, operation_id)
            self.set_meta(
                "logical_clock_ms", max(self.clock(), finished_ms)
            )
        return {**result, "idempotent_replay": False}

    def _apply_side_effect(self, operation: dict[str, Any]) -> None:
        identity = operation["identity"]
        key = (
            identity["kind"],
            identity["namespace"],
            identity["name"],
        )
        action = operation["action"]
        if action in ("create", "update"):
            body = operation["payload"]["object"]
            resource = Resource(
                identity["kind"],
                identity["namespace"],
                identity["name"],
                body,
            )
            self.connection.execute(
                """
                INSERT INTO actual_objects(
                    kind, namespace, name, body_json, digest
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(kind, namespace, name) DO UPDATE SET
                    body_json = excluded.body_json,
                    digest = excluded.digest
                """,
                (
                    resource.kind,
                    resource.namespace,
                    resource.name,
                    canonical_json(resource.body),
                    resource.digest,
                ),
            )
        elif action == "delete":
            self.connection.execute(
                """
                DELETE FROM actual_objects
                WHERE kind = ? AND namespace = ? AND name = ?
                """,
                key,
            )
        elif action != "noop":
            raise EngineError(
                "E_BAD_OPERATION", f"unknown operation action: {action}"
            )
        exists = self.connection.execute(
            """
            SELECT 1 FROM actual_objects
            WHERE kind = ? AND namespace = ? AND name = ?
            """,
            key,
        ).fetchone()
        self.connection.execute(
            """
            UPDATE object_state
            SET actual_exists = ?
            WHERE kind = ? AND namespace = ? AND name = ?
            """,
            (int(exists is not None), *key),
        )

    def record_failure(
        self,
        *,
        round_no: int,
        operation: dict[str, Any],
        finished_ms: int,
        result: dict[str, Any],
        failures: int,
        abandoned: bool,
        next_retry_at_ms: int | None,
    ) -> None:
        operation_id = operation["operation_id"]
        identity = operation["identity"]
        with self.transaction():
            self.connection.execute(
                """
                UPDATE operations
                SET status = ?, finished_ms = ?, result_json = ?,
                    next_retry_at_ms = ?
                WHERE round_no = ? AND operation_id = ?
                """,
                (
                    "abandoned" if abandoned else "failed",
                    finished_ms,
                    canonical_json(result),
                    next_retry_at_ms,
                    round_no,
                    operation_id,
                ),
            )
            self.connection.execute(
                """
                UPDATE object_state
                SET phase = ?, failures = ?, abandoned = ?,
                    next_retry_at_ms = ?, reason_json = ?,
                    updated_round = ?
                WHERE kind = ? AND namespace = ? AND name = ?
                """,
                (
                    "abandoned" if abandoned else "failed",
                    failures,
                    int(abandoned),
                    next_retry_at_ms,
                    canonical_json([result]),
                    round_no,
                    identity["kind"],
                    identity["namespace"],
                    identity["name"],
                ),
            )
            self.release_slot(self.connection, operation_id)
            self.set_meta(
                "logical_clock_ms", max(self.clock(), finished_ms)
            )

    def object_states(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM object_state
            ORDER BY namespace, kind, name
            """
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = f"{row['kind']}/{row['namespace']}/{row['name']}"
            result[key] = {
                "kind": row["kind"],
                "namespace": row["namespace"],
                "name": row["name"],
                "desired_exists": bool(row["desired_exists"]),
                "actual_exists": bool(row["actual_exists"]),
                "phase": row["phase"],
                "comparison_mode": row["comparison_mode"],
                "failures": row["failures"],
                "abandoned": bool(row["abandoned"]),
                "generation": row["generation"],
                "next_retry_at_ms": row["next_retry_at_ms"],
                "reason": json.loads(row["reason_json"]),
                "updated_round": row["updated_round"],
            }
        return result

    def active_slots(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT slot, operation_id, namespace, started_ms
            FROM concurrency_slots
            ORDER BY slot
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def recover(self) -> dict[str, Any]:
        recovered_slots = self.active_slots()
        recovered_operations = self.connection.execute(
            """
            SELECT round_no, operation_id, status
            FROM operations
            WHERE status IN ('started', 'planned')
            ORDER BY round_no, operation_id
            """
        ).fetchall()
        active_rounds = self.connection.execute(
            "SELECT round_no FROM rounds WHERE status = 'executing' ORDER BY round_no"
        ).fetchall()
        with self.transaction():
            for row in active_rounds:
                self.connection.execute(
                    """
                    UPDATE rounds
                    SET status = 'aborted_recovered', ended_ms = ?
                    WHERE round_no = ?
                    """,
                    (self.clock(), row["round_no"]),
                )
            self.connection.execute(
                "DELETE FROM concurrency_slots"
            )
            self.connection.execute(
                """
                UPDATE operations
                SET status = 'uncommitted_recovered'
                WHERE status IN ('started', 'planned')
                """
            )
            self.connection.execute(
                """
                UPDATE object_state
                SET phase = CASE
                    WHEN abandoned = 1 THEN 'abandoned'
                    ELSE 'pending'
                END
                WHERE phase = 'converging'
                """
            )
            self.set_meta("active_operations", 0)
            count = int(self.get_meta("recovery_count", 0)) + (
                1 if recovered_slots or active_rounds else 0
            )
            self.set_meta("recovery_count", count)
            if recovered_slots or active_rounds:
                self.set_meta("last_recovered_at_ms", self.clock())
                self.set_meta("state", "resumed")
        return {
            "released_slots": recovered_slots,
            "aborted_rounds": [row["round_no"] for row in active_rounds],
            "uncommitted_operations": [
                {
                    "round_no": row["round_no"],
                    "operation_id": row["operation_id"],
                    "previous_status": row["status"],
                }
                for row in recovered_operations
            ],
            "logical_clock_ms": self.clock(),
        }

    def commit_round(
        self,
        *,
        round_no: int,
        started_ms: int,
        ended_ms: int,
        summary: dict[str, Any],
        operation_outcomes: list[dict[str, Any]],
        object_states: dict[str, dict[str, Any]],
        comparison_mode: str,
    ) -> None:
        with self.transaction():
            slots = self.connection.execute(
                "SELECT COUNT(*) AS count FROM concurrency_slots"
            ).fetchone()["count"]
            if slots:
                raise EngineError(
                    "E_CONCURRENCY",
                    "cannot commit round with occupied concurrency slots",
                )
            prepared = summary.get("counts", {})
            self.connection.execute(
                """
                UPDATE rounds
                SET status = 'committed', ended_ms = ?, planned_count = ?,
                    started_count = ?, committed_count = ?, deferred_count = ?,
                    failed_count = ?, abandoned_count = ?, summary_json = ?,
                    committed = 1
                WHERE round_no = ?
                """,
                (
                    ended_ms,
                    prepared.get("planned", 0),
                    prepared.get("started", 0),
                    prepared.get("committed", 0),
                    prepared.get("deferred", 0),
                    prepared.get("failed", 0),
                    prepared.get("abandoned", 0),
                    canonical_json(summary),
                    round_no,
                ),
            )
            for outcome in operation_outcomes:
                operation_id = outcome["operation_id"]
                outcome_class = outcome.get("outcome_class")
                if outcome_class == "started_failed":
                    operation_status = (
                        "abandoned"
                        if outcome.get("abandoned")
                        else "failed"
                    )
                elif outcome_class == "abandoned":
                    operation_status = "abandoned"
                elif outcome_class == "committed":
                    operation_status = "committed"
                elif outcome_class and outcome_class.startswith(
                    "not_started_"
                ):
                    operation_status = "deferred"
                else:
                    operation_status = outcome["outcome"]
                self.connection.execute(
                    """
                    UPDATE operations
                    SET status = ?, result_json = ?,
                        next_retry_at_ms = ?
                    WHERE round_no = ? AND operation_id = ?
                      AND committed = 0
                    """,
                    (
                        operation_status,
                        canonical_json(outcome),
                        outcome.get("next_retry_at_ms"),
                        round_no,
                        operation_id,
                    ),
                )
            for key in sorted(object_states):
                state = object_states[key]
                self.connection.execute(
                    """
                    UPDATE object_state
                    SET desired_exists = ?, actual_exists = ?, phase = ?,
                        comparison_mode = ?, failures = ?, abandoned = ?,
                        next_retry_at_ms = ?, reason_json = ?,
                        updated_round = ?
                    WHERE kind = ? AND namespace = ? AND name = ?
                    """,
                    (
                        int(state["desired_exists"]),
                        int(state["actual_exists"]),
                        state["phase"],
                        comparison_mode,
                        int(state.get("failures", 0)),
                        int(state.get("abandoned", False)),
                        state.get("next_retry_at_ms"),
                        canonical_json(state.get("reason", [])),
                        round_no,
                        state["kind"],
                        state["namespace"],
                        state["name"],
                    ),
                )
            self.set_meta(
                "logical_clock_ms", max(self.clock(), ended_ms)
            )
            self.set_meta("round_watermark", round_no)
            self.set_meta("last_comparison_mode", comparison_mode)
            self.set_meta("state", "round_committed")
            self.set_meta("last_round_summary", summary)

    def advance_clock(self, target_ms: int) -> int:
        with self.transaction():
            new_clock = max(self.clock(), int(target_ms))
            self.set_meta("logical_clock_ms", new_clock)
            return new_clock

    def rounds(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM rounds ORDER BY round_no"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["summary"] = json.loads(item.pop("summary_json"))
            item["committed"] = bool(item["committed"])
            result.append(item)
        return result

    def committed_count(self) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) AS count FROM committed_operations"
            ).fetchone()["count"]
        )

    def ledger_audit(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT operation_id, round_no, result_digest, result_json,
                   logical_ms
            FROM committed_operations
            ORDER BY logical_ms, operation_id
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            parsed = json.loads(row["result_json"])
            actual_digest = sha256_hex(parsed)
            result.append(
                {
                    "operation_id": row["operation_id"],
                    "round_no": row["round_no"],
                    "logical_ms": row["logical_ms"],
                    "digest_valid": actual_digest == row["result_digest"],
                }
            )
        return result

    def integrity_snapshot(self) -> dict[str, Any]:
        slots = self.active_slots()
        started = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM operations
            WHERE status IN ('started', 'planned')
            """
        ).fetchone()["count"]
        committed_no_ledger = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM operations o
            LEFT JOIN committed_operations c
                ON c.operation_id = o.operation_id
            WHERE o.committed = 1 AND c.operation_id IS NULL
            """
        ).fetchone()["count"]
        ledger_no_committed = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM committed_operations c
            LEFT JOIN operations o
                ON o.operation_id = c.operation_id AND o.committed = 1
            WHERE o.operation_id IS NULL
            """
        ).fetchone()["count"]
        duplicate_ledger = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT operation_id FROM committed_operations
                GROUP BY operation_id HAVING COUNT(*) > 1
            )
            """
        ).fetchone()["count"]
        executing_rounds = self.connection.execute(
            "SELECT COUNT(*) AS count FROM rounds WHERE status = 'executing'"
        ).fetchone()["count"]
        return {
            "active_slots": slots,
            "started_or_planned_operations": int(started),
            "committed_without_ledger": int(committed_no_ledger),
            "ledger_without_committed_operation": int(ledger_no_committed),
            "duplicate_ledger_entries": int(duplicate_ledger),
            "executing_rounds": int(executing_rounds),
            "logical_clock_ms": self.clock(),
            "round_watermark": self.round_watermark(),
            "committed_operations": self.committed_count(),
        }

