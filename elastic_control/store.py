from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .models import canonical_json

SCHEMA_VERSION = "1"


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    watermark_tick INTEGER NOT NULL,
    current_snapshot_version INTEGER,
    last_committed_round_id TEXT
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    tick INTEGER NOT NULL,
    service_filter TEXT,
    input_digest TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    budget_limit_ticks INTEGER NOT NULL,
    budget_used_ticks INTEGER NOT NULL DEFAULT 0,
    version_observed INTEGER,
    snapshot_version INTEGER,
    result_json TEXT
);

CREATE TABLE IF NOT EXISTS replicas (
    replica_id TEXT PRIMARY KEY,
    service_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    phase TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    next_retry_tick INTEGER,
    ready_tick INTEGER,
    cold_start_ticks INTEGER NOT NULL,
    created_tick INTEGER NOT NULL,
    updated_tick INTEGER NOT NULL,
    last_round_id TEXT,
    UNIQUE (service_name, ordinal)
);

CREATE INDEX IF NOT EXISTS replicas_service_phase
    ON replicas(service_name, phase);

CREATE TABLE IF NOT EXISTS occupancies (
    replica_id TEXT PRIMARY KEY REFERENCES replicas(replica_id) ON DELETE CASCADE,
    service_name TEXT NOT NULL,
    node_name TEXT NOT NULL,
    gpu_index INTEGER NOT NULL,
    memory_mib INTEGER NOT NULL,
    compute_units INTEGER NOT NULL,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    reserved_tick INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS occupancies_node_gpu
    ON occupancies(node_name, gpu_index);

CREATE TABLE IF NOT EXISTS service_counters (
    service_name TEXT PRIMARY KEY,
    next_ordinal INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    replica_id TEXT NOT NULL REFERENCES replicas(replica_id),
    service_name TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    node_name TEXT,
    gpu_index INTEGER,
    memory_mib INTEGER,
    compute_units INTEGER,
    priority INTEGER NOT NULL,
    order_key TEXT NOT NULL,
    planned_ticks INTEGER NOT NULL,
    charged_ticks INTEGER NOT NULL,
    started_tick INTEGER,
    completed_tick INTEGER,
    error_code TEXT,
    detail_json TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    UNIQUE (round_id, replica_id, action)
);

CREATE INDEX IF NOT EXISTS actions_round_status
    ON actions(round_id, status);

CREATE TABLE IF NOT EXISTS round_decisions (
    decision_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    service_name TEXT NOT NULL,
    replica_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome_class TEXT NOT NULL,
    reason_code TEXT,
    order_key TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE (round_id, decision_id)
);

CREATE INDEX IF NOT EXISTS decisions_round
    ON round_decisions(round_id, service_name, kind);

CREATE TABLE IF NOT EXISTS service_rounds (
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    service_name TEXT NOT NULL,
    demand_json TEXT NOT NULL,
    target_replicas INTEGER NOT NULL,
    projected_replicas INTEGER NOT NULL,
    ready_replicas INTEGER NOT NULL,
    cold_starting_replicas INTEGER NOT NULL,
    pending_replicas INTEGER NOT NULL,
    draining_replicas INTEGER NOT NULL,
    launch_deficit INTEGER NOT NULL,
    drain_count INTEGER NOT NULL,
    deferred_json TEXT NOT NULL,
    PRIMARY KEY (round_id, service_name)
);

CREATE TABLE IF NOT EXISTS transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    replica_id TEXT NOT NULL,
    from_phase TEXT NOT NULL,
    to_phase TEXT NOT NULL,
    tick INTEGER NOT NULL,
    round_id TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    version INTEGER PRIMARY KEY,
    round_id TEXT NOT NULL,
    tick INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Store:
    def __init__(self, db_path: str | Path, runtime_dir: str | Path) -> None:
        self.db_path = Path(db_path)
        self.runtime_dir = Path(runtime_dir)
        self.lock_dir = self.runtime_dir / "locks"

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        with self.lock("schema"):
            connection = self._connect()
            try:
                connection.executescript(SCHEMA_SQL)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
                    ("schema_version", SCHEMA_VERSION),
                )
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = ?",
                    ("schema_version",),
                ).fetchone()
                if row is None or row["value"] != SCHEMA_VERSION:
                    raise BusinessError(
                        ErrorCode.STATE_CORRUPT,
                        "unsupported state schema version",
                        details={"found": _row_to_dict(row), "expected": SCHEMA_VERSION},
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO state_meta(
                        id, watermark_tick, current_snapshot_version,
                        last_committed_round_id
                    ) VALUES (1, 0, NULL, NULL)
                    """
                )
                connection.commit()
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=15.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextlib.contextmanager
    def lock(self, scope: str) -> Iterator[None]:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]
        path = self.lock_dir / f"{digest}.lock"
        handle = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @contextlib.contextmanager
    def locks(self, scopes: Sequence[str]) -> Iterator[None]:
        unique_scopes = sorted(set(scopes))
        with contextlib.ExitStack() as stack:
            for scope in unique_scopes:
                stack.enter_context(self.lock(scope))
            yield

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
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

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def get_round(self, round_id: str) -> dict[str, Any] | None:
        with self.read() as connection:
            return _row_to_dict(
                connection.execute(
                    "SELECT * FROM rounds WHERE round_id = ?",
                    (round_id,),
                ).fetchone()
            )

    def list_replicas(
        self,
        connection: sqlite3.Connection,
        service_name: str | None = None,
    ) -> list[dict[str, Any]]:
        if service_name is None:
            rows = connection.execute(
                "SELECT * FROM replicas ORDER BY service_name, ordinal"
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM replicas
                WHERE service_name = ?
                ORDER BY ordinal
                """,
                (service_name,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_occupancies(
        self,
        connection: sqlite3.Connection,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM occupancies
            ORDER BY node_name, gpu_index, replica_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def list_actions(
        self,
        connection: sqlite3.Connection,
        round_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM actions
            WHERE round_id = ?
            ORDER BY service_name, order_key, action_id
            """,
            (round_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_decisions(
        self,
        connection: sqlite3.Connection,
        round_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM round_decisions
            WHERE round_id = ?
            ORDER BY service_name, order_key, decision_id
            """,
            (round_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_service_rounds(
        self,
        connection: sqlite3.Connection,
        round_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM service_rounds
            WHERE round_id = ?
            ORDER BY service_name
            """,
            (round_id,),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["demand"] = json.loads(item.pop("demand_json"))
            item["deferred"] = json.loads(item.pop("deferred_json"))
            output.append(item)
        return output

    def allocate_replica_id(
        self,
        connection: sqlite3.Connection,
        service_name: str,
    ) -> tuple[str, int]:
        row = connection.execute(
            "SELECT next_ordinal FROM service_counters WHERE service_name = ?",
            (service_name,),
        ).fetchone()
        if row is None:
            ordinal = 1
            connection.execute(
                """
                INSERT INTO service_counters(service_name, next_ordinal)
                VALUES (?, ?)
                """,
                (service_name, ordinal + 1),
            )
        else:
            ordinal = int(row["next_ordinal"])
            connection.execute(
                """
                UPDATE service_counters
                SET next_ordinal = ?
                WHERE service_name = ?
                """,
                (ordinal + 1, service_name),
            )
        return f"{service_name}--{ordinal:04d}", ordinal

    def record_transition(
        self,
        connection: sqlite3.Connection,
        *,
        replica_id: str,
        from_phase: str,
        to_phase: str,
        tick: int,
        round_id: str,
        reason: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO transitions(
                replica_id, from_phase, to_phase, tick, round_id, reason
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (replica_id, from_phase, to_phase, tick, round_id, reason),
        )

    def promote_ready_replicas(
        self,
        connection: sqlite3.Connection,
        *,
        tick: int,
        round_id: str,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT replica_id FROM replicas
            WHERE phase = 'cold_starting' AND ready_tick <= ?
            ORDER BY service_name, ordinal
            """,
            (tick,),
        ).fetchall()
        promoted: list[str] = []
        for row in rows:
            replica_id = str(row["replica_id"])
            connection.execute(
                """
                UPDATE replicas
                SET phase = 'ready', updated_tick = ?, last_round_id = ?
                WHERE replica_id = ?
                """,
                (tick, round_id, replica_id),
            )
            self.record_transition(
                connection,
                replica_id=replica_id,
                from_phase="cold_starting",
                to_phase="ready",
                tick=tick,
                round_id=round_id,
                reason="cold_start_complete",
            )
            promoted.append(replica_id)
        return promoted

    def get_state_meta(self, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM state_meta WHERE id = 1"
        ).fetchone()
        if row is None:
            raise BusinessError(ErrorCode.STATE_CORRUPT, "state_meta row is missing")
        return dict(row)

    def set_in_flight(
        self,
        round_id: str,
        action_ids: Sequence[str],
        tick: int,
    ) -> None:
        if not action_ids:
            return
        with self.tx() as connection:
            for action_id in action_ids:
                connection.execute(
                    """
                    UPDATE actions
                    SET status = 'in_flight',
                        started_tick = COALESCE(started_tick, ?)
                    WHERE action_id = ?
                      AND round_id = ?
                      AND status IN ('reserved', 'in_flight')
                    """,
                    (tick, action_id, round_id),
                )
            connection.execute(
                "UPDATE rounds SET status = 'in_progress' WHERE round_id = ?",
                (round_id,),
            )

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        with self.read() as connection:
            return _row_to_dict(
                connection.execute(
                    "SELECT * FROM actions WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
            )

    def complete_launch_success(
        self,
        *,
        round_id: str,
        action_id: str,
        tick: int,
        ready_tick: int,
        detail: dict[str, Any],
    ) -> None:
        with self.tx() as connection:
            action = self._action_for_update(connection, action_id)
            if action["status"] == "succeeded":
                return
            connection.execute(
                """
                UPDATE actions
                SET status = 'succeeded', completed_tick = ?,
                    error_code = NULL, detail_json = ?
                WHERE action_id = ?
                """,
                (tick, canonical_json(detail), action_id),
            )
            connection.execute(
                """
                UPDATE replicas
                SET phase = 'cold_starting', ready_tick = ?,
                    failure_count = 0, next_retry_tick = NULL,
                    updated_tick = ?, last_round_id = ?
                WHERE replica_id = ?
                """,
                (ready_tick, tick, round_id, action["replica_id"]),
            )
            self.record_transition(
                connection,
                replica_id=str(action["replica_id"]),
                from_phase="pending",
                to_phase="cold_starting",
                tick=tick,
                round_id=round_id,
                reason="launch_succeeded",
            )
            decision_id = self._decision_id_for_action(action)
            connection.execute(
                """
                UPDATE round_decisions
                SET status = 'succeeded', outcome_class = 'started',
                    reason_code = NULL
                WHERE decision_id = ?
                """,
                (decision_id,),
            )

    def complete_launch_failure(
        self,
        *,
        round_id: str,
        action_id: str,
        tick: int,
        error_code: str,
        detail: dict[str, Any],
        failure_count: int,
        next_retry_tick: int,
        backoff: dict[str, Any],
    ) -> None:
        with self.tx() as connection:
            action = self._action_for_update(connection, action_id)
            if action["status"] == "failed":
                return
            merged_detail = dict(detail)
            merged_detail["backoff"] = backoff
            connection.execute(
                """
                UPDATE actions
                SET status = 'failed', completed_tick = ?,
                    error_code = ?, detail_json = ?
                WHERE action_id = ?
                """,
                (tick, error_code, canonical_json(merged_detail), action_id),
            )
            connection.execute(
                """
                UPDATE replicas
                SET phase = 'pending', failure_count = ?,
                    next_retry_tick = ?, ready_tick = NULL,
                    updated_tick = ?, last_round_id = ?
                WHERE replica_id = ?
                """,
                (
                    failure_count,
                    next_retry_tick,
                    tick,
                    round_id,
                    action["replica_id"],
                ),
            )
            connection.execute(
                "DELETE FROM occupancies WHERE replica_id = ?",
                (action["replica_id"],),
            )
            self.record_transition(
                connection,
                replica_id=str(action["replica_id"]),
                from_phase="pending",
                to_phase="pending",
                tick=tick,
                round_id=round_id,
                reason=f"launch_failed:{error_code}",
            )
            decision_id = self._decision_id_for_action(action)
            connection.execute(
                """
                UPDATE round_decisions
                SET status = 'launch_failed', outcome_class = 'launch_failed',
                    reason_code = ?
                WHERE decision_id = ?
                """,
                (error_code, decision_id),
            )

    def complete_drain_success(
        self,
        *,
        round_id: str,
        action_id: str,
        tick: int,
        detail: dict[str, Any],
    ) -> None:
        with self.tx() as connection:
            action = self._action_for_update(connection, action_id)
            if action["status"] == "succeeded":
                return
            connection.execute(
                """
                UPDATE actions
                SET status = 'succeeded', completed_tick = ?,
                    error_code = NULL, detail_json = ?
                WHERE action_id = ?
                """,
                (tick, canonical_json(detail), action_id),
            )
            connection.execute(
                """
                UPDATE replicas
                SET phase = 'reclaimed', next_retry_tick = NULL,
                    updated_tick = ?, last_round_id = ?
                WHERE replica_id = ?
                """,
                (tick, round_id, action["replica_id"]),
            )
            connection.execute(
                "DELETE FROM occupancies WHERE replica_id = ?",
                (action["replica_id"],),
            )
            self.record_transition(
                connection,
                replica_id=str(action["replica_id"]),
                from_phase="draining",
                to_phase="reclaimed",
                tick=tick,
                round_id=round_id,
                reason="drain_succeeded",
            )
            decision_id = self._decision_id_for_action(action)
            connection.execute(
                """
                UPDATE round_decisions
                SET status = 'succeeded', outcome_class = 'drained',
                    reason_code = NULL
                WHERE decision_id = ?
                """,
                (decision_id,),
            )

    def complete_drain_failure(
        self,
        *,
        round_id: str,
        action_id: str,
        tick: int,
        error_code: str,
        detail: dict[str, Any],
    ) -> None:
        with self.tx() as connection:
            action = self._action_for_update(connection, action_id)
            if action["status"] == "failed":
                return
            connection.execute(
                """
                UPDATE actions
                SET status = 'failed', completed_tick = ?,
                    error_code = ?, detail_json = ?
                WHERE action_id = ?
                """,
                (tick, error_code, canonical_json(detail), action_id),
            )
            connection.execute(
                """
                UPDATE replicas
                SET updated_tick = ?, last_round_id = ?
                WHERE replica_id = ?
                """,
                (tick, round_id, action["replica_id"]),
            )
            decision_id = self._decision_id_for_action(action)
            connection.execute(
                """
                UPDATE round_decisions
                SET status = 'drain_failed', outcome_class = 'drain_failed',
                    reason_code = ?
                WHERE decision_id = ?
                """,
                (error_code, decision_id),
            )

    def _action_for_update(
        self,
        connection: sqlite3.Connection,
        action_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise BusinessError(
                ErrorCode.STATE_CORRUPT,
                "action disappeared from persistent state",
                details={"action_id": action_id},
            )
        return dict(row)

    @staticmethod
    def _decision_id_for_action(action: dict[str, Any]) -> str:
        return f"{action['round_id']}:{action['replica_id']}:{action['action']}"

    def commit_round(
        self,
        *,
        round_id: str,
        tick: int,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        with self.tx() as connection:
            round_row = connection.execute(
                "SELECT * FROM rounds WHERE round_id = ?",
                (round_id,),
            ).fetchone()
            if round_row is None:
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "round disappeared before commit",
                    details={"round_id": round_id},
                )
            if round_row["status"] == "committed":
                return json.loads(round_row["result_json"])

            unfinished = connection.execute(
                """
                SELECT COUNT(*) AS count FROM actions
                WHERE round_id = ? AND status IN ('reserved', 'in_flight')
                """,
                (round_id,),
            ).fetchone()["count"]
            if unfinished:
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "cannot commit a round with unfinished actions",
                    details={"round_id": round_id, "unfinished": unfinished},
                )

            meta = self.get_state_meta(connection)
            observed = meta["current_snapshot_version"]
            version = int(observed or 0) + 1
            snapshot_payload = self._build_snapshot(connection, round_id, tick)
            payload_json = canonical_json(snapshot_payload)
            connection.execute(
                """
                INSERT INTO snapshots(version, round_id, tick, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (version, round_id, tick, payload_json),
            )
            connection.execute(
                """
                UPDATE state_meta
                SET current_snapshot_version = ?,
                    last_committed_round_id = ?,
                    watermark_tick = MAX(watermark_tick, ?)
                WHERE id = 1
                """,
                (version, round_id, tick),
            )

            committed_result = dict(result)
            committed_result["snapshot"] = {
                "version_observed": observed,
                "version_committed": version,
                "rebased": round_row["version_observed"] != observed,
                "last_round_id": round_id,
            }
            result_json = canonical_json(committed_result)
            connection.execute(
                """
                UPDATE rounds
                SET status = 'committed', snapshot_version = ?,
                    result_json = ?
                WHERE round_id = ?
                """,
                (version, result_json, round_id),
            )
            return committed_result

    def _build_snapshot(
        self,
        connection: sqlite3.Connection,
        round_id: str,
        tick: int,
    ) -> dict[str, Any]:
        meta = self.get_state_meta(connection)
        replicas = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM replicas
                ORDER BY service_name, ordinal
                """
            ).fetchall()
        ]
        occupancies = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM occupancies
                ORDER BY node_name, gpu_index, replica_id
                """
            ).fetchall()
        ]
        actions = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM actions
                WHERE round_id = ?
                ORDER BY service_name, order_key, action_id
                """,
                (round_id,),
            ).fetchall()
        ]
        decisions = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM round_decisions
                WHERE round_id = ?
                ORDER BY service_name, order_key, decision_id
                """,
                (round_id,),
            ).fetchall()
        ]
        service_rounds = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM service_rounds
                WHERE round_id = ?
                ORDER BY service_name
                """,
                (round_id,),
            ).fetchall()
        ]
        for item in service_rounds:
            item["demand"] = json.loads(item.pop("demand_json"))
            item["deferred"] = json.loads(item.pop("deferred_json"))
        for item in actions + decisions:
            if item.get("detail_json"):
                item["detail"] = json.loads(item.pop("detail_json"))
        return {
            "logical_clock": tick,
            "watermark_tick": meta["watermark_tick"],
            "last_committed_round_id": round_id,
            "replicas": replicas,
            "occupancies": occupancies,
            "actions": actions,
            "decisions": decisions,
            "service_rounds": service_rounds,
        }

    def get_current_snapshot(self) -> dict[str, Any] | None:
        with self.read() as connection:
            meta = self.get_state_meta(connection)
            version = meta["current_snapshot_version"]
            if version is None:
                return None
            row = connection.execute(
                "SELECT * FROM snapshots WHERE version = ?",
                (version,),
            ).fetchone()
            if row is None:
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "snapshot pointer references a missing snapshot",
                    details={"version": version},
                )
            return {
                "version": int(row["version"]),
                "round_id": row["round_id"],
                "tick": int(row["tick"]),
                "payload": json.loads(row["payload_json"]),
                "last_committed_round_id": meta["last_committed_round_id"],
            }

    def list_snapshots(self) -> list[dict[str, Any]]:
        with self.read() as connection:
            rows = connection.execute(
                """
                SELECT version, round_id, tick, payload_json
                FROM snapshots ORDER BY version
                """
            ).fetchall()
            return [
                {
                    "version": int(row["version"]),
                    "round_id": row["round_id"],
                    "tick": int(row["tick"]),
                    "payload": json.loads(row["payload_json"]),
                }
                for row in rows
            ]

    def export_state(self) -> dict[str, Any]:
        with self.read() as connection:
            meta = self.get_state_meta(connection)
            rounds = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM rounds ORDER BY tick, round_id"
                ).fetchall()
            ]
            for row in rounds:
                if row.get("result_json"):
                    row["result"] = json.loads(row.pop("result_json"))
                else:
                    row.pop("result_json", None)
            replicas = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM replicas ORDER BY service_name, ordinal"
                ).fetchall()
            ]
            occupancies = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM occupancies ORDER BY node_name, gpu_index"
                ).fetchall()
            ]
            actions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM actions ORDER BY round_id, service_name, order_key"
                ).fetchall()
            ]
            decisions = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM round_decisions
                    ORDER BY round_id, service_name, order_key
                    """
                ).fetchall()
            ]
            transitions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM transitions ORDER BY transition_id"
                ).fetchall()
            ]
            for collection in (actions, decisions):
                for item in collection:
                    if item.get("detail_json"):
                        item["detail"] = json.loads(item.pop("detail_json"))
            return {
                "state_meta": meta,
                "rounds": rounds,
                "replicas": replicas,
                "occupancies": occupancies,
                "actions": actions,
                "decisions": decisions,
                "transitions": transitions,
            }

