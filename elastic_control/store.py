"""SQLite persistence, optimistic versioning and crash recovery."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .aggregation import Sample, WindowAggregate, aggregate_windows, latest_window_per_metric
from .decision import DecisionOutput, WorkloadDecision
from .errors import (
    ACTION_RESULT_CONFLICT,
    ALREADY_LOADED,
    BUDGET_EXCEEDED,
    NOT_LOADED,
    PERSISTENCE_ERROR,
    ROUND_CONFLICT,
    SCHEMA_ERROR,
    VERIFY_MISMATCH,
    VERSION_CONFLICT,
    VERSION_ROLLBACK,
    ControlError,
)
from .fixedpoint import FixedPointBook, LedgerEntry, MetricLedger, replay_ledger
from .graph import PolicyPatch, apply_patch_to_bundle
from .locking import ProcessThreadLock
from .models import PolicyBundle
from .scheduler import (
    ActionPlan,
    next_runtime_after_failure,
    next_runtime_after_success,
    plan_actions,
)
from .util import (
    canonical_json,
    dec,
    decimal_text,
    jsonable,
    sha256_text,
)


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS objects (
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    PRIMARY KEY (kind, name)
);

CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    metric TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    value_text TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    ingested_round TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_metric_time
    ON samples(metric, timestamp_ms, sample_id);

CREATE TABLE IF NOT EXISTS sample_watermark (
    metric TEXT PRIMARY KEY,
    watermark_ms INTEGER NOT NULL,
    last_clock_ms INTEGER NOT NULL,
    last_window_json TEXT,
    updated_round TEXT
);

CREATE TABLE IF NOT EXISTS workload_state (
    name TEXT PRIMARY KEY,
    replicas INTEGER NOT NULL,
    cooldown_until_ms INTEGER NOT NULL DEFAULT 0,
    freeze_until_ms INTEGER NOT NULL DEFAULT 0,
    backoff_until_ms INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_action_clock_ms INTEGER NOT NULL DEFAULT 0,
    target_cache_json TEXT,
    cache_version INTEGER NOT NULL DEFAULT 0,
    budget_epoch INTEGER NOT NULL DEFAULT 0,
    precision_sensitive INTEGER NOT NULL DEFAULT 0,
    precision_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS metric_ledger (
    metric TEXT PRIMARY KEY,
    scale INTEGER,
    residual_text TEXT NOT NULL,
    cumulative_error_text TEXT NOT NULL,
    next_step_no INTEGER NOT NULL,
    entries_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS budget_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cpu_used_text TEXT NOT NULL,
    memory_used_text TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    created_clock_ms INTEGER NOT NULL,
    policy_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    snapshot_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS round_workloads (
    round_id TEXT NOT NULL,
    workload TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    decision_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (round_id, workload)
);

CREATE TABLE IF NOT EXISTS actions (
    action_key TEXT PRIMARY KEY,
    workload TEXT NOT NULL,
    round_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    direction TEXT NOT NULL,
    delta INTEGER NOT NULL,
    current_replicas INTEGER NOT NULL,
    target_replicas INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    cpu_cost_text TEXT NOT NULL,
    memory_cost_text TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    outcome_success INTEGER NOT NULL,
    outcome_reason TEXT,
    start_at_ms INTEGER NOT NULL,
    finish_at_ms INTEGER NOT NULL,
    budget_charged INTEGER NOT NULL DEFAULT 0,
    request_fingerprint TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (workload, round_id, rule_version, rule_name, direction)
);
CREATE INDEX IF NOT EXISTS idx_actions_round_status
    ON actions(round_id, status);

CREATE TABLE IF NOT EXISTS budget_charges (
    charge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_key TEXT NOT NULL UNIQUE,
    round_id TEXT NOT NULL,
    cpu_text TEXT NOT NULL,
    memory_text TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    state_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_snapshots_current
    ON snapshots(is_current) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS precision_flags (
    flag_key TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    workload TEXT NOT NULL,
    flag_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS precision_divergences (
    round_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    divergence_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (round_id, metric, step_no, step_id)
);

CREATE TABLE IF NOT EXISTS round_outcomes (
    round_id TEXT NOT NULL,
    workload TEXT NOT NULL,
    success INTEGER NOT NULL,
    reason TEXT,
    PRIMARY KEY (round_id, workload)
);

CREATE TABLE IF NOT EXISTS runtime_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    workload TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0
);
"""


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


class Store:
    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        timeout: float = 30.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.lock_dir = self.db_path.parent / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.control_lock_path = self.lock_dir / "control.lock"
        self.decision_lock_path = self.lock_dir / "decision.lock"
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=self.timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        return connection

    def _ensure_schema(self) -> None:
        with ProcessThreadLock(self.control_lock_path, self.timeout):
            connection = self._connect()
            try:
                connection.executescript(SCHEMA_SQL)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            finally:
                connection.close()

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        lock = ProcessThreadLock(self.control_lock_path, self.timeout) if write else None
        if lock is not None:
            lock.__enter__()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()
            if lock is not None:
                lock.__exit__(None, None, None)

    @contextmanager
    def decision_session(self) -> Iterator[None]:
        """Serialise read-decide-write across processes.

        The SQLite transaction prevents budget oversell; this file lock also
        makes the metric residual ledger a single read-modify-write operation.
        """

        with ProcessThreadLock(self.decision_lock_path, self.timeout):
            yield

    def round_lock(self, round_id: str) -> ProcessThreadLock:
        safe = sha256_text(round_id)[:24]
        return ProcessThreadLock(self.lock_dir / f"round-{safe}.lock", self.timeout)

    # ------------------------------------------------------------------
    # Meta and object state
    # ------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.transaction(write=False) as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key=?", (key,)
            ).fetchone()
        return default if row is None else str(row["value"])

    def require_loaded(self, connection: sqlite3.Connection | None = None) -> None:
        owns = connection is None
        if owns:
            connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='loaded'"
            ).fetchone()
            if row is None or row["value"] != "1":
                raise ControlError(
                    NOT_LOADED,
                    "control database has no loaded policy",
                    {"db": str(self.db_path)},
                )
        finally:
            if owns:
                connection.close()

    def policy_version(self) -> int:
        value = self.get_meta("policy_version", "0")
        return int(value or 0)

    def state_version(self) -> int:
        value = self.get_meta("state_version", "0")
        return int(value or 0)

    def current_snapshot(self) -> dict[str, Any] | None:
        with self.transaction(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE is_current=1"
            ).fetchone()
        if row is None:
            return None
        snapshot = json.loads(row["snapshot_json"])
        snapshot["_snapshot_id"] = row["snapshot_id"]
        snapshot["_created_at"] = row["created_at"]
        return snapshot

    def load_bundle(self, connection: sqlite3.Connection | None = None) -> PolicyBundle:
        owns = connection is None
        if owns:
            connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT kind, name, spec_json FROM objects"
            ).fetchall()
        finally:
            if owns:
                connection.close()
        payload: dict[str, Any] = {
            "workloads": [],
            "metrics": [],
            "rules": [],
            "budget": None,
        }
        mapping = {
            "workload": "workloads",
            "metric": "metrics",
            "rule": "rules",
        }
        for row in rows:
            spec = json.loads(row["spec_json"])
            if row["kind"] == "budget":
                payload["budget"] = spec
            else:
                key = mapping.get(row["kind"])
                if key is None:
                    raise ControlError(
                        PERSISTENCE_ERROR,
                        f"unknown persisted object kind {row['kind']!r}",
                        {"kind": row["kind"]},
                    )
                payload[key].append(spec)
        return PolicyBundle.from_payload(payload)

    def load_runtime(self, connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        rows = connection.execute("SELECT * FROM workload_state").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["target_cache"] = (
                None if item["target_cache_json"] is None else json.loads(item["target_cache_json"])
            )
            item["precision_flags"] = json.loads(item["precision_json"] or "[]")
            item["precision_sensitive"] = bool(item["precision_sensitive"])
            result[item["name"]] = item
        return result

    def replicas(self) -> dict[str, int]:
        with self.transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT name, replicas FROM workload_state ORDER BY name"
            ).fetchall()
        return {str(row["name"]): int(row["replicas"]) for row in rows}

    def load_ledger(self, connection: sqlite3.Connection | None = None) -> FixedPointBook:
        owns = connection is None
        if owns:
            connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM metric_ledger ORDER BY metric"
            ).fetchall()
        finally:
            if owns:
                connection.close()
        ledgers: dict[str, MetricLedger] = {}
        for row in rows:
            ledgers[str(row["metric"])] = MetricLedger(
                metric=str(row["metric"]),
                scale=None if row["scale"] is None else int(row["scale"]),
                residual=dec(row["residual_text"]),
                cumulative_error_bound=dec(row["cumulative_error_text"]),
                next_step_no=int(row["next_step_no"]),
                entries=[
                    LedgerEntry.from_dict(item)
                    for item in json.loads(row["entries_json"])
                ],
            )
        return FixedPointBook(ledgers)

    def _persist_ledger(
        self, connection: sqlite3.Connection, book: FixedPointBook
    ) -> None:
        for metric in sorted(book.ledgers):
            ledger = book.ledgers[metric]
            entries_json = canonical_json([entry.to_dict() for entry in ledger.entries])
            current = connection.execute(
                "SELECT * FROM metric_ledger WHERE metric=?", (metric,)
            ).fetchone()
            if current is not None:
                if (
                    current["scale"] == ledger.scale
                    and current["residual_text"] == decimal_text(ledger.residual)
                    and current["cumulative_error_text"]
                    == decimal_text(ledger.cumulative_error_bound)
                    and int(current["next_step_no"]) == ledger.next_step_no
                    and current["entries_json"] == entries_json
                ):
                    continue
                connection.execute(
                    """
                    UPDATE metric_ledger
                    SET scale=?, residual_text=?, cumulative_error_text=?,
                        next_step_no=?, entries_json=?, version=version+1
                    WHERE metric=?
                    """,
                    (
                        ledger.scale,
                        decimal_text(ledger.residual),
                        decimal_text(ledger.cumulative_error_bound),
                        ledger.next_step_no,
                        entries_json,
                        metric,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO metric_ledger(
                        metric, scale, residual_text, cumulative_error_text,
                        next_step_no, entries_json, version
                    ) VALUES(?,?,?,?,?,?,1)
                    """,
                    (
                        metric,
                        ledger.scale,
                        decimal_text(ledger.residual),
                        decimal_text(ledger.cumulative_error_bound),
                        ledger.next_step_no,
                        entries_json,
                    ),
                )

    # ------------------------------------------------------------------
    # Loading samples
    # ------------------------------------------------------------------
    def initialize(
        self,
        bundle: PolicyBundle,
        samples: Sequence[Sample],
        *,
        events: Sequence[Mapping[str, Any]] = (),
        reset: bool = False,
        policy_version: int = 1,
    ) -> dict[str, Any]:
        bundle.validate()
        with self.transaction(write=True) as connection:
            loaded = connection.execute(
                "SELECT value FROM schema_meta WHERE key='loaded'"
            ).fetchone()
            if loaded is not None and loaded["value"] == "1" and not reset:
                raise ControlError(
                    ALREADY_LOADED,
                    "database already contains a loaded policy; use --reset",
                    {"db": str(self.db_path)},
                )
            for table in (
                "precision_flags",
                "precision_divergences",
                "round_outcomes",
                "runtime_events",
                "budget_charges",
                "actions",
                "round_workloads",
                "rounds",
                "snapshots",
                "metric_ledger",
                "workload_state",
                "sample_watermark",
                "samples",
                "objects",
                "budget_account",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.execute("DELETE FROM schema_meta WHERE key != 'schema_version'")
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            self._write_bundle(connection, bundle)
            for name, workload in sorted(bundle.workloads.items()):
                connection.execute(
                    """
                    INSERT INTO workload_state(
                        name, replicas, target_cache_json, cache_version,
                        budget_epoch, precision_sensitive, precision_json
                    ) VALUES(?,?,NULL,0,0,0,'[]')
                    """,
                    (name, workload.current_replicas),
                )
            for sample in sorted(samples, key=lambda item: (item.metric, item.timestamp_ms, item.sample_id)):
                connection.execute(
                    """
                    INSERT INTO samples(
                        sample_id, metric, timestamp_ms, value_text, sequence_no
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        sample.sample_id,
                        sample.metric,
                        sample.timestamp_ms,
                        decimal_text(sample.value),
                        sample.sequence,
                    ),
                )
            for metric_name in sorted(bundle.metrics):
                connection.execute(
                    """
                    INSERT INTO sample_watermark(
                        metric, watermark_ms, last_clock_ms, last_window_json, updated_round
                    ) VALUES(?,0,0,NULL,NULL)
                    """,
                    (metric_name,),
                )
            connection.execute(
                """
                INSERT INTO budget_account(id,cpu_used_text,memory_used_text,version)
                VALUES(1,'0','0',1)
                """
            )
            for index, event in enumerate(events):
                self._insert_event(connection, event, index)
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('policy_version',?)",
                (str(policy_version),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('state_version','1')"
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('loaded','1')"
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('current_round','')"
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('last_decision_clock','0')"
            )
            self._write_snapshot(connection, round_id="initial", at_ms=0, status="loaded")
        return self.status()

    def _write_bundle(
        self, connection: sqlite3.Connection, bundle: PolicyBundle
    ) -> None:
        rows: list[tuple[str, str, str]] = []
        for name, workload in sorted(bundle.workloads.items()):
            rows.append(("workload", name, canonical_json(workload.to_dict())))
        for name, metric in sorted(bundle.metrics.items()):
            rows.append(("metric", name, canonical_json(metric.to_dict())))
        for name, rule in sorted(bundle.rules.items()):
            rows.append(("rule", name, canonical_json(rule.to_dict())))
        if bundle.budget is not None:
            rows.append(("budget", "global", canonical_json(bundle.budget.to_dict())))
        connection.executemany(
            "INSERT INTO objects(kind,name,spec_json) VALUES(?,?,?)",
            rows,
        )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        event: Mapping[str, Any],
        index: int,
    ) -> None:
        kind = str(event.get("type", event.get("kind", ""))).lower()
        round_id = str(event.get("round_id", "*"))
        workload = str(event.get("workload", "*"))
        at_ms = int(event.get("at_ms", 0))
        if kind == "action_outcome":
            success = bool(event.get("success", True))
            connection.execute(
                """
                INSERT OR REPLACE INTO round_outcomes(round_id,workload,success,reason)
                VALUES(?,?,?,?)
                """,
                (round_id, workload, 1 if success else 0, event.get("reason")),
            )
            return
        if kind not in {"freeze", "set_failures", "clock"}:
            raise ControlError(
                SCHEMA_ERROR,
                f"unsupported event type {kind!r}",
                {"index": index, "type": kind},
            )
        connection.execute(
            """
            INSERT INTO runtime_events(round_id,workload,at_ms,kind,payload_json)
            VALUES(?,?,?,?,?)
            """,
            (round_id, workload, at_ms, kind, canonical_json(jsonable(event))),
        )

    # ------------------------------------------------------------------
    # Samples and watermarks
    # ------------------------------------------------------------------
    def load_samples_after_watermark(
        self,
        connection: sqlite3.Connection,
        *,
        at_ms: int | None = None,
    ) -> dict[str, list[Sample]]:
        watermarks = {
            str(row["metric"]): int(row["watermark_ms"])
            for row in connection.execute(
                "SELECT metric, watermark_ms FROM sample_watermark"
            ).fetchall()
        }
        rows = connection.execute(
            "SELECT * FROM samples ORDER BY metric, timestamp_ms, sample_id"
        ).fetchall()
        result: dict[str, list[Sample]] = {metric: [] for metric in watermarks}
        for row in rows:
            metric = str(row["metric"])
            timestamp = int(row["timestamp_ms"])
            watermark = watermarks.get(metric, 0)
            if at_ms is not None and timestamp > at_ms:
                continue
            if timestamp < watermark:
                # Samples below the watermark were committed in an earlier
                # round.  The current loader is append-only at load time; these
                # rows must be skipped rather than counted again.
                continue
            result.setdefault(metric, []).append(
                Sample(
                    sample_id=str(row["sample_id"]),
                    metric=metric,
                    timestamp_ms=timestamp,
                    value=dec(row["value_text"]),
                    sequence=int(row["sequence_no"]),
                )
            )
        return result

    def watermarks(self, connection: sqlite3.Connection | None = None) -> dict[str, dict[str, Any]]:
        owns = connection is None
        if owns:
            connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM sample_watermark ORDER BY metric"
            ).fetchall()
        finally:
            if owns:
                connection.close()
        return {
            str(row["metric"]): {
                "watermark_ms": int(row["watermark_ms"]),
                "last_clock_ms": int(row["last_clock_ms"]),
                "last_window": (
                    None
                    if row["last_window_json"] is None
                    else json.loads(row["last_window_json"])
                ),
                "updated_round": row["updated_round"],
            }
            for row in rows
        }

    # ------------------------------------------------------------------
    # Round commit
    # ------------------------------------------------------------------
    def commit_round(
        self,
        *,
        round_id: str,
        at_ms: int,
        decisions: DecisionOutput,
        input_hashes: Mapping[str, str],
        watermark_updates: Mapping[str, WindowAggregate],
        expected_version: int | None = None,
        outcome_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self.transaction(write=True) as connection:
            self.require_loaded(connection)
            current_version = int(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='policy_version'"
                ).fetchone()["value"]
            )
            if expected_version is not None:
                if expected_version < current_version:
                    raise ControlError(
                        VERSION_ROLLBACK,
                        "requested policy version is older than the committed version",
                        {
                            "expected_version": expected_version,
                            "committed_version": current_version,
                        },
                    )
                if expected_version > current_version:
                    raise ControlError(
                        VERSION_CONFLICT,
                        "requested policy version does not exist",
                        {
                            "expected_version": expected_version,
                            "committed_version": current_version,
                        },
                    )
            round_row = connection.execute(
                "SELECT * FROM rounds WHERE round_id=?", (round_id,)
            ).fetchone()
            if round_row is not None and int(round_row["policy_version"]) != current_version:
                raise ControlError(
                    ROUND_CONFLICT,
                    "round was created under a different policy version",
                    {
                        "round_id": round_id,
                        "round_policy_version": int(round_row["policy_version"]),
                        "committed_version": current_version,
                    },
                )
            if round_row is not None and int(round_row["created_clock_ms"]) != at_ms:
                raise ControlError(
                    ROUND_CONFLICT,
                    "round already exists at a different logical clock",
                    {
                        "round_id": round_id,
                        "round_clock_ms": int(round_row["created_clock_ms"]),
                        "requested_clock_ms": at_ms,
                    },
                )

            new_decisions: list[WorkloadDecision] = []
            for workload_name in sorted(decisions.workloads):
                decision = decisions.workloads[workload_name]
                row = connection.execute(
                    "SELECT * FROM round_workloads WHERE round_id=? AND workload=?",
                    (round_id, workload_name),
                ).fetchone()
                expected_hash = input_hashes.get(workload_name, "")
                if row is not None:
                    if row["input_hash"] != expected_hash:
                        raise ControlError(
                            ROUND_CONFLICT,
                            "the same workload and round already has a different decision input",
                            {
                                "round_id": round_id,
                                "workload": workload_name,
                            },
                        )
                    continue
                new_decisions.append(decision)

            if not new_decisions:
                return {
                    "round_id": round_id,
                    "idempotent": True,
                    "planned_actions": self._actions_for_round(connection, round_id),
                    "policy_version": current_version,
                    "snapshot_id": connection.execute(
                        "SELECT value FROM schema_meta WHERE key='current_snapshot'"
                    ).fetchone()["value"],
                }

            if round_row is None:
                connection.execute(
                    """
                    INSERT INTO rounds(round_id,created_clock_ms,policy_version,status)
                    VALUES(?,?,?,?)
                    """,
                    (round_id, at_ms, current_version, "planning"),
                )
            self._apply_runtime_events(connection, round_id=round_id, at_ms=at_ms)
            bundle = self.load_bundle(connection)
            runtime = self.load_runtime(connection)
            replicas = {
                name: int(item["replicas"]) for name, item in runtime.items()
            }
            budget_row = connection.execute(
                "SELECT * FROM budget_account WHERE id=1"
            ).fetchone()
            used_cpu = dec(budget_row["cpu_used_text"])
            used_memory = dec(budget_row["memory_used_text"])
            active_rows = connection.execute(
                """
                SELECT workload, COUNT(*) AS count FROM actions
                WHERE status IN ('reserved','started','running')
                GROUP BY workload
                """
            ).fetchall()
            active = {str(row["workload"]): int(row["count"]) for row in active_rows}
            outcomes = self._outcomes_for_round(connection, round_id)
            for workload_name, override in (outcome_overrides or {}).items():
                outcomes[workload_name] = dict(override)
            planning = plan_actions(
                bundle,
                new_decisions,
                round_id=round_id,
                at_ms=at_ms,
                used_cpu_core_ms=used_cpu,
                used_memory_mib=used_memory,
                runtime=runtime,
                active_per_workload=active,
                outcomes=outcomes,
            )
            reserved = [action for action in planning.actions if action.will_start]
            for action in planning.actions:
                inserted = self._insert_action(connection, action)
                if not inserted and action.will_start:
                    # A concurrent idempotent insert must still account for the
                    # existing charge, not charge twice.
                    existing = connection.execute(
                        "SELECT budget_charged FROM actions WHERE action_key=?",
                        (action.action_key,),
                    ).fetchone()
                    if existing is None or int(existing["budget_charged"]) != 1:
                        raise ControlError(
                            PERSISTENCE_ERROR,
                            "existing action was not budget-charged",
                            {"action_key": action.action_key},
                        )
                if action.will_start:
                    charge = connection.execute(
                        "SELECT 1 FROM budget_charges WHERE action_key=?",
                        (action.action_key,),
                    ).fetchone()
                    if charge is None:
                        connection.execute(
                            """
                            INSERT INTO budget_charges(
                                action_key,round_id,cpu_text,memory_text,committed
                            ) VALUES(?,?,?,?,1)
                            """,
                            (
                                action.action_key,
                                round_id,
                                decimal_text(action.cpu_cost),
                                decimal_text(action.memory_cost),
                            ),
                        )
            if reserved:
                connection.execute(
                    """
                    UPDATE budget_account
                    SET cpu_used_text=?, memory_used_text=?, version=version+1
                    WHERE id=1
                    """,
                    (
                        decimal_text(used_cpu + planning.newly_reserved_cpu),
                        decimal_text(used_memory + planning.newly_reserved_memory),
                    ),
                )

            for decision in new_decisions:
                connection.execute(
                    """
                    INSERT INTO round_workloads(
                        round_id,workload,policy_version,decision_json,input_hash
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        round_id,
                        decision.workload,
                        current_version,
                        canonical_json(decision.to_dict()),
                        input_hashes.get(decision.workload, ""),
                    ),
                )
                flags = [
                    flag.to_dict()
                    for flag in decisions.precision_flags
                    if flag.workload == decision.workload
                ]
                connection.execute(
                    """
                    UPDATE workload_state
                    SET target_cache_json=?, cache_version=cache_version+1,
                        precision_sensitive=?, precision_json=?
                    WHERE name=?
                    """,
                    (
                        canonical_json(decision.to_dict()),
                        1 if flags else 0,
                        canonical_json(flags),
                        decision.workload,
                    ),
                )
                for flag in flags:
                    flag_key = sha256_text(
                        f"{round_id}|{decision.workload}|{canonical_json(flag)}"
                    )
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO precision_flags(
                            flag_key,round_id,workload,flag_json
                        ) VALUES(?,?,?,?)
                        """,
                        (flag_key, round_id, decision.workload, canonical_json(flag)),
                    )
            if decisions.first_divergence is not None:
                divergence_payload = decisions.first_divergence.to_dict()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO precision_divergences(
                        round_id,metric,step_no,step_id,divergence_json
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        round_id,
                        decisions.first_divergence.metric,
                        decisions.first_divergence.step_no,
                        decisions.first_divergence.step_id,
                        canonical_json(divergence_payload),
                    ),
                )
            self._persist_ledger(connection, decisions.ledger)
            clock_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='last_decision_clock'"
            ).fetchone()
            prior_clock = 0 if clock_row is None else int(clock_row["value"])
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('last_decision_clock',?)",
                (str(max(prior_clock, at_ms)),),
            )
            self._update_watermarks(
                connection,
                round_id=round_id,
                at_ms=at_ms,
                updates=watermark_updates,
            )
            pending = connection.execute(
                """
                SELECT COUNT(*) AS count FROM actions
                WHERE round_id=? AND status IN ('reserved','started','running')
                """,
                (round_id,),
            ).fetchone()["count"]
            round_status = "actions_pending" if pending else "committed"
            connection.execute(
                "UPDATE rounds SET status=? WHERE round_id=?",
                (round_status, round_id),
            )
            self._increment_state_version(connection)
            snapshot_id = self._write_snapshot(
                connection, round_id=round_id, at_ms=at_ms, status=round_status
            )
            connection.execute(
                "UPDATE rounds SET snapshot_id=? WHERE round_id=?",
                (snapshot_id, round_id),
            )
            return {
                "round_id": round_id,
                "idempotent": False,
                "policy_version": current_version,
                "planned_actions": [action.to_dict() for action in planning.actions],
                "newly_reserved_cpu_core_ms": decimal_text(planning.newly_reserved_cpu),
                "newly_reserved_memory_mib": decimal_text(planning.newly_reserved_memory),
                "snapshot_id": snapshot_id,
            }

    def _insert_action(self, connection: sqlite3.Connection, action: ActionPlan) -> bool:
        existing = connection.execute(
            "SELECT request_fingerprint FROM actions WHERE action_key=?",
            (action.action_key,),
        ).fetchone()
        request_json = canonical_json(action.request_payload())
        fingerprint = sha256_text(request_json)
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise ControlError(
                    ROUND_CONFLICT,
                    "action key already exists with a different request",
                    {"action_key": action.action_key},
                )
            return False
        connection.execute(
            """
            INSERT INTO actions(
                action_key,workload,round_id,rule_name,rule_version,direction,
                delta,current_replicas,target_replicas,priority,duration_ms,
                cpu_cost_text,memory_cost_text,status,reason,details_json,
                outcome_success,outcome_reason,start_at_ms,finish_at_ms,
                budget_charged,request_fingerprint,request_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                action.action_key,
                action.workload,
                action.round_id,
                action.rule_name,
                action.rule_version,
                action.direction,
                action.delta,
                action.current_replicas,
                action.target_replicas,
                action.priority,
                action.duration_ms,
                decimal_text(action.cpu_cost),
                decimal_text(action.memory_cost),
                action.status,
                action.reason,
                canonical_json(action.details),
                1 if action.outcome_success else 0,
                action.outcome_reason,
                action.start_at_ms,
                action.finish_at_ms,
                1 if action.will_start else 0,
                fingerprint,
                request_json,
            ),
        )
        return True

    def _actions_for_round(
        self, connection: sqlite3.Connection, round_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM actions WHERE round_id=? ORDER BY priority DESC, workload, action_key",
            (round_id,),
        ).fetchall()
        return [self._action_row(row) for row in rows]

    def _action_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json") or "{}")
        item["request"] = json.loads(item.pop("request_json") or "{}")
        item["result"] = (
            None if item.get("result_json") is None else json.loads(item["result_json"])
        )
        item.pop("result_json", None)
        item["outcome_success"] = bool(item["outcome_success"])
        item["budget_charged"] = bool(item["budget_charged"])
        return item

    def _apply_runtime_events(
        self,
        connection: sqlite3.Connection,
        *,
        round_id: str,
        at_ms: int,
    ) -> None:
        rows = connection.execute(
            """
            SELECT * FROM runtime_events
            WHERE applied=0 AND at_ms<=? AND (round_id=? OR round_id='*')
            ORDER BY event_id
            """,
            (at_ms, round_id),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            workload = str(row["workload"])
            if workload != "*":
                exists = connection.execute(
                    "SELECT 1 FROM workload_state WHERE name=?", (workload,)
                ).fetchone()
                if exists is None:
                    raise ControlError(
                        PERSISTENCE_ERROR,
                        f"runtime event references unknown workload {workload!r}",
                        {"event_id": row["event_id"]},
                    )
                if row["kind"] == "freeze":
                    until = int(payload.get("until_ms", at_ms))
                    connection.execute(
                        """
                        UPDATE workload_state
                        SET freeze_until_ms=MAX(freeze_until_ms, ?)
                        WHERE name=?
                        """,
                        (until, workload),
                    )
                elif row["kind"] == "set_failures":
                    count = int(payload.get("count", 0))
                    connection.execute(
                        """
                        UPDATE workload_state
                        SET consecutive_failures=?, backoff_until_ms=?
                        WHERE name=?
                        """,
                        (count, int(payload.get("backoff_until_ms", 0)), workload),
                    )
            connection.execute(
                "UPDATE runtime_events SET applied=1 WHERE event_id=?",
                (row["event_id"],),
            )

    def _outcomes_for_round(
        self, connection: sqlite3.Connection, round_id: str
    ) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM round_outcomes
            WHERE round_id=? OR round_id='*'
            ORDER BY CASE WHEN round_id=? THEN 1 ELSE 0 END
            """,
            (round_id, round_id),
        ).fetchall()
        return {
            str(row["workload"]): {
                "success": bool(row["success"]),
                "reason": row["reason"],
            }
            for row in rows
        }

    def _update_watermarks(
        self,
        connection: sqlite3.Connection,
        *,
        round_id: str,
        at_ms: int,
        updates: Mapping[str, WindowAggregate],
    ) -> None:
        for metric in sorted(updates):
            window = updates[metric]
            current = connection.execute(
                "SELECT * FROM sample_watermark WHERE metric=?", (metric,)
            ).fetchone()
            if current is None:
                raise ControlError(
                    PERSISTENCE_ERROR,
                    f"missing watermark row for metric {metric!r}",
                    {"metric": metric},
                )
            watermark = max(int(current["watermark_ms"]), window.window_end_ms)
            last_clock = max(int(current["last_clock_ms"]), at_ms)
            connection.execute(
                """
                UPDATE sample_watermark
                SET watermark_ms=?, last_clock_ms=?, last_window_json=?,
                    updated_round=?
                WHERE metric=?
                """,
                (
                    watermark,
                    last_clock,
                    canonical_json(window.to_dict()),
                    round_id,
                    metric,
                ),
            )

    # ------------------------------------------------------------------
    # Patch
    # ------------------------------------------------------------------
    def apply_patch(
        self,
        patch: PolicyPatch,
        *,
        expected_version: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        # Policy version changes and decision read-modify-write sections share
        # the same advisory lock, so a round cannot be computed against a
        # policy that is replaced before its commit.
        with self.decision_session():
            return self._apply_patch_locked(
                patch,
                expected_version=expected_version,
                dry_run=dry_run,
            )

    def _apply_patch_locked(
        self,
        patch: PolicyPatch,
        *,
        expected_version: int | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        self.require_loaded()
        old_version = self.policy_version()
        old_bundle = self.load_bundle()
        result = apply_patch_to_bundle(old_bundle, patch)
        next_version = old_version + 1
        if dry_run:
            return {
                "dry_run": True,
                "base_version": old_version,
                "next_version": next_version,
                "impact": result.impact,
            }
        with self.transaction(write=True) as connection:
            self.require_loaded(connection)
            current_version = int(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='policy_version'"
                ).fetchone()["value"]
            )
            requested = patch.base_version if expected_version is None else expected_version
            if requested is None:
                requested = current_version
            if requested < current_version:
                raise ControlError(
                    VERSION_ROLLBACK,
                    "patch base version is older than the committed version",
                    {"base_version": requested, "committed_version": current_version},
                )
            if requested > current_version:
                raise ControlError(
                    VERSION_CONFLICT,
                    "patch base version is newer than the committed version",
                    {"base_version": requested, "committed_version": current_version},
                )
            if current_version != old_version:
                raise ControlError(
                    VERSION_CONFLICT,
                    "policy changed while the patch was being validated",
                    {"validated_version": old_version, "committed_version": current_version},
                )
            account = connection.execute(
                "SELECT * FROM budget_account WHERE id=1"
            ).fetchone()
            if result.bundle.budget is not None and account is not None:
                used_cpu = dec(account["cpu_used_text"])
                used_memory = dec(account["memory_used_text"])
                if used_cpu > result.bundle.budget.pool_cpu_core_ms:
                    raise ControlError(
                        BUDGET_EXCEEDED,
                        "new CPU budget is below already committed usage",
                        {
                            "used_cpu_core_ms": decimal_text(used_cpu),
                            "new_limit": decimal_text(
                                result.bundle.budget.pool_cpu_core_ms
                            ),
                        },
                    )
                if used_memory > result.bundle.budget.pool_memory_mib:
                    raise ControlError(
                        BUDGET_EXCEEDED,
                        "new memory budget is below already committed usage",
                        {
                            "used_memory_mib": decimal_text(used_memory),
                            "new_limit": decimal_text(
                                result.bundle.budget.pool_memory_mib
                            ),
                        },
                    )
            active = connection.execute(
                """
                SELECT DISTINCT workload FROM actions
                WHERE status IN ('reserved','started','running')
                """
            ).fetchall()
            active_workloads = {str(row["workload"]) for row in active}
            removed = set(old_bundle.workloads) - set(result.bundle.workloads)
            blocked = sorted(removed & active_workloads)
            if blocked:
                raise ControlError(
                    ROUND_CONFLICT,
                    "cannot delete workloads with in-flight actions",
                    {"workloads": blocked},
                )
            connection.execute("DELETE FROM objects")
            connection.execute("DELETE FROM precision_divergences")
            self._write_bundle(connection, result.bundle)
            impact = result.impact
            affected = impact["affected_workloads"]
            for workload_name in sorted(removed):
                connection.execute(
                    "DELETE FROM workload_state WHERE name=?", (workload_name,)
                )
            for workload_name in sorted(result.bundle.workloads):
                if workload_name not in old_bundle.workloads:
                    workload = result.bundle.workloads[workload_name]
                    connection.execute(
                        """
                        INSERT INTO workload_state(
                            name,replicas,target_cache_json,cache_version,
                            budget_epoch,precision_sensitive,precision_json
                        ) VALUES(?,?,NULL,0,0,0,'[]')
                        """,
                        (workload_name, workload.current_replicas),
                    )
                    continue
                detail = affected.get(workload_name)
                if detail is None:
                    continue
                old_workload = old_bundle.workloads[workload_name]
                new_workload = result.bundle.workloads[workload_name]
                sets: list[str] = []
                params: list[Any] = []
                if detail["invalidate_target_cache"]:
                    sets.extend(
                        ["target_cache_json=NULL", "cache_version=cache_version+1"]
                    )
                if detail["invalidate_cooldown_timer"]:
                    sets.append("cooldown_until_ms=0")
                if detail["invalidate_freeze_timer"]:
                    sets.extend(["freeze_until_ms=0", "backoff_until_ms=0"])
                if detail["invalidate_budget_reservation"]:
                    sets.append("budget_epoch=budget_epoch+1")
                if detail["invalidate_precision_ledger"]:
                    sets.extend(["precision_sensitive=0", "precision_json='[]'"])
                if old_workload.current_replicas != new_workload.current_replicas:
                    sets.append("replicas=?")
                    params.append(new_workload.current_replicas)
                if sets:
                    params.append(workload_name)
                    connection.execute(
                        f"UPDATE workload_state SET {', '.join(sets)} WHERE name=?",
                        params,
                    )
            for metric_name in impact.get("changed_metrics", []):
                connection.execute(
                    "DELETE FROM metric_ledger WHERE metric=?", (metric_name,)
                )
                if metric_name in result.bundle.metrics:
                    # Window/target semantics may have changed.  Rewind this
                    # metric's watermark so the next decision recomputes from
                    # committed samples instead of reusing an incompatible
                    # cached aggregate.
                    connection.execute(
                        """
                        UPDATE sample_watermark
                        SET watermark_ms=0, last_clock_ms=0,
                            last_window_json=NULL, updated_round=NULL
                        WHERE metric=?
                        """,
                        (metric_name,),
                    )
                else:
                    connection.execute(
                        "DELETE FROM sample_watermark WHERE metric=?", (metric_name,)
                    )
            for metric_name in sorted(result.bundle.metrics):
                exists = connection.execute(
                    "SELECT 1 FROM sample_watermark WHERE metric=?", (metric_name,)
                ).fetchone()
                if exists is None:
                    connection.execute(
                        """
                        INSERT INTO sample_watermark(
                            metric,watermark_ms,last_clock_ms,last_window_json,updated_round
                        ) VALUES(?,0,0,NULL,NULL)
                        """,
                        (metric_name,),
                    )
            connection.execute(
                "UPDATE schema_meta SET value=? WHERE key='policy_version'",
                (str(next_version),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('last_patch_impact',?)",
                (canonical_json(impact),),
            )
            self._increment_state_version(connection)
            current_round = (
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='current_round'"
                ).fetchone()["value"]
                or "patch"
            )
            snapshot_id = self._write_snapshot(
                connection,
                round_id=current_round,
                at_ms=0,
                status="patch_applied",
                extra={"last_patch_impact": impact},
            )
        return {
            "dry_run": False,
            "base_version": old_version,
            "policy_version": next_version,
            "impact": impact,
            "snapshot_id": snapshot_id,
        }

    # ------------------------------------------------------------------
    # Action execution and recovery
    # ------------------------------------------------------------------
    def start_round_actions(
        self, round_id: str, *, reclaim_running: bool = False
    ) -> list[dict[str, Any]]:
        with self.transaction(write=True) as connection:
            self.require_loaded(connection)
            round_row = connection.execute(
                "SELECT * FROM rounds WHERE round_id=?", (round_id,)
            ).fetchone()
            if round_row is None:
                raise ControlError(
                    PERSISTENCE_ERROR,
                    f"round {round_id!r} does not exist",
                    {"round_id": round_id},
                )
            if reclaim_running:
                connection.execute(
                    """
                    UPDATE actions SET status='started', updated_at=CURRENT_TIMESTAMP
                    WHERE round_id=? AND status='running'
                    """,
                    (round_id,),
                )
            connection.execute(
                """
                UPDATE actions SET status='started', updated_at=CURRENT_TIMESTAMP
                WHERE round_id=? AND status='reserved'
                """,
                (round_id,),
            )
            rows = connection.execute(
                """
                SELECT * FROM actions WHERE round_id=? AND status='started'
                ORDER BY priority DESC, workload, action_key
                """,
                (round_id,),
            ).fetchall()
            if rows:
                connection.execute(
                    "UPDATE rounds SET status='actions_running' WHERE round_id=?",
                    (round_id,),
                )
                self._increment_state_version(connection)
                self._write_snapshot(
                    connection,
                    round_id=round_id,
                    at_ms=int(round_row["created_clock_ms"]),
                    status="actions_running",
                )
            return [self._action_row(row) for row in rows]

    def claim_action(self, action_key: str) -> dict[str, Any] | None:
        """Atomically claim a started action for execution.

        The caller holds the round file lock, so a stale ``running`` row can
        only belong to a process that exited.  The claim is nevertheless a CAS
        update to make duplicate execution impossible across callers.
        """

        with self.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM actions WHERE action_key=?", (action_key,)
            ).fetchone()
            if row is None:
                raise ControlError(
                    PERSISTENCE_ERROR,
                    f"action {action_key!r} does not exist",
                    {"action_key": action_key},
                )
            if row["status"] in {"succeeded", "failed_after_start"}:
                return None
            if row["status"] != "started":
                raise ControlError(
                    ACTION_RESULT_CONFLICT,
                    "action is not in a startable state",
                    {"action_key": action_key, "status": row["status"]},
                )
            cursor = connection.execute(
                """
                UPDATE actions SET status='running', updated_at=CURRENT_TIMESTAMP
                WHERE action_key=? AND status='started'
                """,
                (action_key,),
            )
            if cursor.rowcount != 1:
                return None
            return self._action_row(row)

    def complete_action(
        self,
        action_key: str,
        *,
        success: bool,
        finish_at_ms: int,
        reason: str | None = None,
        result_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.transaction(write=True) as connection:
            self.require_loaded(connection)
            row = connection.execute(
                "SELECT * FROM actions WHERE action_key=?", (action_key,)
            ).fetchone()
            if row is None:
                raise ControlError(
                    PERSISTENCE_ERROR,
                    f"action {action_key!r} does not exist",
                    {"action_key": action_key},
                )
            if row["status"] in {"succeeded", "failed_after_start"}:
                prior_success = row["status"] == "succeeded"
                if prior_success != success:
                    raise ControlError(
                        ACTION_RESULT_CONFLICT,
                        "action already completed with a different result",
                        {
                            "action_key": action_key,
                            "prior_success": prior_success,
                            "new_success": success,
                        },
                    )
                return {
                    "action_key": action_key,
                    "status": row["status"],
                    "idempotent": True,
                    "result": None
                    if row["result_json"] is None
                    else json.loads(row["result_json"]),
                }
            if row["status"] not in {"started", "running"}:
                raise ControlError(
                    ACTION_RESULT_CONFLICT,
                    "action cannot be completed from its current state",
                    {"action_key": action_key, "status": row["status"]},
                )
            if not bool(row["budget_charged"]):
                raise ControlError(
                    PERSISTENCE_ERROR,
                    "started action has no budget charge",
                    {"action_key": action_key},
                )
            workload_name = str(row["workload"])
            state_row = connection.execute(
                "SELECT * FROM workload_state WHERE name=?", (workload_name,)
            ).fetchone()
            if state_row is None:
                raise ControlError(
                    PERSISTENCE_ERROR,
                    "action references a missing workload state",
                    {"action_key": action_key, "workload": workload_name},
                )
            runtime = {
                "cooldown_until_ms": int(state_row["cooldown_until_ms"]),
                "freeze_until_ms": int(state_row["freeze_until_ms"]),
                "backoff_until_ms": int(state_row["backoff_until_ms"]),
                "consecutive_failures": int(state_row["consecutive_failures"]),
                "last_action_clock_ms": int(state_row["last_action_clock_ms"]),
            }
            bundle = self.load_bundle(connection)
            rule = bundle.rules.get(str(row["rule_name"]))
            replicas = int(state_row["replicas"])
            if success:
                replicas += int(row["delta"])
                if rule is not None:
                    updated_runtime = next_runtime_after_success(
                        bundle,
                        at_ms=int(row["start_at_ms"]),
                        duration_ms=int(row["duration_ms"]),
                        direction=str(row["direction"]),
                        rule=rule,
                        runtime=runtime,
                    )
                else:
                    updated_runtime = runtime
                status = "succeeded"
            else:
                if rule is not None:
                    updated_runtime = next_runtime_after_failure(
                        bundle,
                        at_ms=int(row["start_at_ms"]),
                        duration_ms=int(row["duration_ms"]),
                        runtime=runtime,
                    )
                else:
                    updated_runtime = runtime
                status = "failed_after_start"
            result = {
                "success": success,
                "reason": reason or row["outcome_reason"],
                "replicas_before": int(state_row["replicas"]),
                "replicas_after": replicas,
                "replicas_changed": success,
                "finish_at_ms": finish_at_ms,
                **(dict(result_payload or {})),
            }
            connection.execute(
                """
                UPDATE workload_state
                SET replicas=?, cooldown_until_ms=?, freeze_until_ms=?,
                    backoff_until_ms=?, consecutive_failures=?,
                    last_action_clock_ms=?
                WHERE name=?
                """,
                (
                    replicas,
                    updated_runtime.get("cooldown_until_ms", 0),
                    updated_runtime.get("freeze_until_ms", 0),
                    updated_runtime.get("backoff_until_ms", 0),
                    updated_runtime.get("consecutive_failures", 0),
                    updated_runtime.get("last_action_clock_ms", 0),
                    workload_name,
                ),
            )
            connection.execute(
                """
                UPDATE actions
                SET status=?, reason=?, finish_at_ms=?, result_json=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE action_key=?
                """,
                (status, reason or row["outcome_reason"], finish_at_ms, canonical_json(result), action_key),
            )
            pending = connection.execute(
                """
                SELECT COUNT(*) AS count FROM actions
                WHERE round_id=? AND status IN ('reserved','started','running')
                """,
                (row["round_id"],),
            ).fetchone()["count"]
            round_status = "actions_pending" if pending else "completed"
            connection.execute(
                "UPDATE rounds SET status=? WHERE round_id=?",
                (round_status, row["round_id"]),
            )
            self._increment_state_version(connection)
            snapshot_id = self._write_snapshot(
                connection,
                round_id=str(row["round_id"]),
                at_ms=int(
                    connection.execute(
                        "SELECT created_clock_ms FROM rounds WHERE round_id=?",
                        (row["round_id"],),
                    ).fetchone()["created_clock_ms"]
                ),
                status=round_status,
            )
            connection.execute(
                "UPDATE rounds SET snapshot_id=? WHERE round_id=?",
                (snapshot_id, row["round_id"]),
            )
            return {
                "action_key": action_key,
                "status": status,
                "idempotent": False,
                "result": result,
                "snapshot_id": snapshot_id,
            }

    def pending_actions(self, round_id: str | None = None) -> list[dict[str, Any]]:
        with self.transaction(write=False) as connection:
            if round_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM actions
                    WHERE status IN ('reserved','started','running')
                    ORDER BY round_id, priority DESC, workload, action_key
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM actions
                    WHERE round_id=? AND status IN ('reserved','started','running')
                    ORDER BY priority DESC, workload, action_key
                    """,
                    (round_id,),
                ).fetchall()
        return [self._action_row(row) for row in rows]

    def pending_rounds(self) -> list[str]:
        with self.transaction(write=False) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT round_id FROM actions
                WHERE status IN ('reserved','started','running')
                ORDER BY round_id
                """
            ).fetchall()
        return [str(row["round_id"]) for row in rows]

    def round_workload_hashes(self, round_id: str) -> dict[str, str]:
        with self.transaction(write=False) as connection:
            rows = connection.execute(
                """
                SELECT workload, input_hash FROM round_workloads
                WHERE round_id=? ORDER BY workload
                """,
                (round_id,),
            ).fetchall()
        return {str(row["workload"]): str(row["input_hash"]) for row in rows}

    def round_info(self, round_id: str) -> dict[str, Any] | None:
        with self.transaction(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM rounds WHERE round_id=?", (round_id,)
            ).fetchone()
        return _row_dict(row)

    def action_outcome(self, action: Mapping[str, Any]) -> tuple[bool, str | None]:
        return bool(action.get("outcome_success", True)), action.get("outcome_reason")

    # ------------------------------------------------------------------
    # Snapshot/status/verify
    # ------------------------------------------------------------------
    def _increment_state_version(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='state_version'"
        ).fetchone()
        current = 0 if row is None else int(row["value"])
        next_value = current + 1
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('state_version',?)",
            (str(next_value),),
        )
        return next_value

    def _write_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        round_id: str,
        at_ms: int,
        status: str,
        extra: Mapping[str, Any] | None = None,
    ) -> int:
        policy_version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='policy_version'"
            ).fetchone()["value"]
        )
        state_version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='state_version'"
            ).fetchone()["value"]
        )
        runtime = self.load_runtime(connection)
        bundle = self.load_bundle(connection)
        ledger_rows = connection.execute(
            "SELECT * FROM metric_ledger ORDER BY metric"
        ).fetchall()
        ledgers = {
            str(row["metric"]): {
                "scale": None if row["scale"] is None else int(row["scale"]),
                "residual": row["residual_text"],
                "cumulative_error_bound": row["cumulative_error_text"],
                "next_step_no": int(row["next_step_no"]),
                "entries": json.loads(row["entries_json"]),
            }
            for row in ledger_rows
        }
        watermark_rows = connection.execute(
            "SELECT * FROM sample_watermark ORDER BY metric"
        ).fetchall()
        windows = {
            str(row["metric"]): (
                None if row["last_window_json"] is None else json.loads(row["last_window_json"])
            )
            for row in watermark_rows
        }
        sample_watermarks = {
            str(row["metric"]): {
                "watermark_ms": int(row["watermark_ms"]),
                "last_clock_ms": int(row["last_clock_ms"]),
                "updated_round": row["updated_round"],
            }
            for row in watermark_rows
        }
        clock_row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='last_decision_clock'"
        ).fetchone()
        budget_row = connection.execute(
            "SELECT * FROM budget_account WHERE id=1"
        ).fetchone()
        action_rows = connection.execute(
            """
            SELECT * FROM actions
            WHERE round_id=? OR status IN ('reserved','started','running')
            ORDER BY round_id, priority DESC, workload, action_key
            """,
            (round_id,),
        ).fetchall()
        round_rows = connection.execute(
            """
            SELECT * FROM round_workloads
            WHERE round_id=?
            ORDER BY workload
            """,
            (round_id,),
        ).fetchall()
        decisions = {
            str(row["workload"]): json.loads(row["decision_json"])
            for row in round_rows
        }
        divergence_row = connection.execute(
            """
            SELECT divergence_json FROM precision_divergences
            WHERE round_id=?
            ORDER BY step_no, metric, step_id
            LIMIT 1
            """,
            (round_id,),
        ).fetchone()
        if extra is not None and "last_patch_impact" in extra:
            patch_impact = extra.get("last_patch_impact")
        else:
            patch_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='last_patch_impact'"
            ).fetchone()
            patch_impact = (
                None if patch_row is None else json.loads(patch_row["value"])
            )
        snapshot: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "policy_version": policy_version,
            "state_version": state_version,
            "round_id": round_id,
            "at_ms": at_ms,
            "status": status,
            "budget": {
                "limits": (
                    None if bundle.budget is None else bundle.budget.to_dict()
                ),
                "used_cpu_core_ms": budget_row["cpu_used_text"],
                "used_memory_mib": budget_row["memory_used_text"],
                "remaining_cpu_core_ms": (
                    "0"
                    if bundle.budget is None
                    else decimal_text(
                        bundle.budget.pool_cpu_core_ms - dec(budget_row["cpu_used_text"])
                    )
                ),
                "remaining_memory_mib": (
                    "0"
                    if bundle.budget is None
                    else decimal_text(
                        bundle.budget.pool_memory_mib
                        - dec(budget_row["memory_used_text"])
                    )
                ),
            },
            "workloads": {
                name: {
                    "replicas": int(item["replicas"]),
                    "cooldown_until_ms": int(item["cooldown_until_ms"]),
                    "freeze_until_ms": int(item["freeze_until_ms"]),
                    "backoff_until_ms": int(item["backoff_until_ms"]),
                    "consecutive_failures": int(item["consecutive_failures"]),
                    "last_action_clock_ms": int(item["last_action_clock_ms"]),
                    "target_cache": item["target_cache"],
                    "cache_version": int(item["cache_version"]),
                    "budget_epoch": int(item["budget_epoch"]),
                    "precision_sensitive": bool(item["precision_sensitive"]),
                    "precision_flags": item["precision_flags"],
                }
                for name, item in sorted(runtime.items())
            },
            "metric_ledgers": ledgers,
            "windows": windows,
            "sample_watermarks": sample_watermarks,
            "last_decision_clock_ms": (
                0 if clock_row is None else int(clock_row["value"])
            ),
            "round_decisions": decisions,
            "first_precision_divergence": (
                None
                if divergence_row is None
                else json.loads(divergence_row["divergence_json"])
            ),
            "actions": [self._action_row(row) for row in action_rows],
            "patch_impact": patch_impact,
        }
        connection.execute("UPDATE snapshots SET is_current=0 WHERE is_current=1")
        cursor = connection.execute(
            """
            INSERT INTO snapshots(
                round_id,policy_version,clock_ms,state_version,status,
                snapshot_json,is_current
            ) VALUES(?,?,?,?,?,?,1)
            """,
            (
                round_id,
                policy_version,
                at_ms,
                state_version,
                status,
                canonical_json(snapshot),
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('current_snapshot',?)",
            (str(snapshot_id),),
        )
        if round_id and round_id != "initial" and round_id != "patch":
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('current_round',?)",
                (round_id,),
            )
        return snapshot_id

    def status(self) -> dict[str, Any]:
        self.require_loaded()
        snapshot = self.current_snapshot()
        if snapshot is None:
            raise ControlError(
                PERSISTENCE_ERROR,
                "loaded database has no current snapshot",
                {"db": str(self.db_path)},
            )
        snapshot = dict(snapshot)
        snapshot["db"] = str(self.db_path)
        snapshot["current_snapshot"] = snapshot.pop("_snapshot_id", None)
        return snapshot

    def verify(self) -> dict[str, Any]:
        violations: list[dict[str, Any]] = []
        self.require_loaded()
        with self.transaction(write=False) as connection:
            current_rows = connection.execute(
                "SELECT * FROM snapshots WHERE is_current=1"
            ).fetchall()
            if len(current_rows) != 1:
                violations.append(
                    {
                        "code": "CURRENT_SNAPSHOT_COUNT",
                        "message": f"expected one current snapshot, found {len(current_rows)}",
                    }
                )
                snapshot = None
            else:
                snapshot = json.loads(current_rows[0]["snapshot_json"])
            pointer = connection.execute(
                "SELECT value FROM schema_meta WHERE key='current_snapshot'"
            ).fetchone()
            if current_rows and (
                pointer is None
                or str(current_rows[0]["snapshot_id"]) != str(pointer["value"])
            ):
                violations.append(
                    {
                        "code": "SNAPSHOT_POINTER_MISMATCH",
                        "message": "current snapshot pointer does not match the flagged row",
                    }
                )
            policy_version = int(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='policy_version'"
                ).fetchone()["value"]
            )
            if snapshot is not None and int(snapshot["policy_version"]) != policy_version:
                violations.append(
                    {
                        "code": "POLICY_VERSION_MISMATCH",
                        "snapshot": snapshot["policy_version"],
                        "committed": policy_version,
                    }
                )
            state_version_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='state_version'"
            ).fetchone()
            if (
                snapshot is not None
                and state_version_row is not None
                and int(snapshot.get("state_version", -1))
                != int(state_version_row["value"])
            ):
                violations.append(
                    {
                        "code": "STATE_VERSION_MISMATCH",
                        "snapshot": snapshot.get("state_version"),
                        "committed": int(state_version_row["value"]),
                    }
                )
            ledger_rows = connection.execute(
                "SELECT * FROM metric_ledger ORDER BY metric"
            ).fetchall()
            for row in ledger_rows:
                try:
                    residual, cumulative, scale, next_step = replay_ledger(
                        str(row["metric"]), json.loads(row["entries_json"])
                    )
                except ValueError as exc:
                    violations.append(
                        {
                            "code": "LEDGER_REPLAY_FAILED",
                            "metric": row["metric"],
                            "message": str(exc),
                        }
                    )
                    continue
                if residual != dec(row["residual_text"]):
                    violations.append(
                        {
                            "code": "LEDGER_RESIDUAL_MISMATCH",
                            "metric": row["metric"],
                            "stored": row["residual_text"],
                            "replayed": decimal_text(residual),
                        }
                    )
                if cumulative != dec(row["cumulative_error_text"]):
                    violations.append(
                        {
                            "code": "LEDGER_ERROR_MISMATCH",
                            "metric": row["metric"],
                            "stored": row["cumulative_error_text"],
                            "replayed": decimal_text(cumulative),
                        }
                    )
                if row["scale"] is not None and int(row["scale"]) != scale:
                    violations.append(
                        {
                            "code": "LEDGER_SCALE_MISMATCH",
                            "metric": row["metric"],
                        }
                    )
                if int(row["next_step_no"]) != next_step:
                    violations.append(
                        {
                            "code": "LEDGER_STEP_MISMATCH",
                            "metric": row["metric"],
                            "stored": int(row["next_step_no"]),
                            "replayed": next_step,
                        }
                    )
            charge_rows = connection.execute(
                "SELECT cpu_text,memory_text FROM budget_charges WHERE committed=1"
            ).fetchall()
            charged_cpu = sum((dec(row["cpu_text"]) for row in charge_rows), Decimal(0))
            charged_memory = sum(
                (dec(row["memory_text"]) for row in charge_rows), Decimal(0)
            )
            budget_row = connection.execute(
                "SELECT * FROM budget_account WHERE id=1"
            ).fetchone()
            if charged_cpu != dec(budget_row["cpu_used_text"]):
                violations.append(
                    {
                        "code": "BUDGET_CPU_LEDGER_MISMATCH",
                        "account": budget_row["cpu_used_text"],
                        "charges": decimal_text(charged_cpu),
                    }
                )
            if charged_memory != dec(budget_row["memory_used_text"]):
                violations.append(
                    {
                        "code": "BUDGET_MEMORY_LEDGER_MISMATCH",
                        "account": budget_row["memory_used_text"],
                        "charges": decimal_text(charged_memory),
                    }
                )
            bundle = self.load_bundle(connection)
            if bundle.budget is not None:
                if dec(budget_row["cpu_used_text"]) > bundle.budget.pool_cpu_core_ms:
                    violations.append(
                        {"code": "CPU_BUDGET_OVERSELL", "used": budget_row["cpu_used_text"]}
                    )
                if dec(budget_row["memory_used_text"]) > bundle.budget.pool_memory_mib:
                    violations.append(
                        {
                            "code": "MEMORY_BUDGET_OVERSELL",
                            "used": budget_row["memory_used_text"],
                        }
                    )
            state_rows = connection.execute(
                "SELECT name,replicas FROM workload_state ORDER BY name"
            ).fetchall()
            state_replicas = {
                str(row["name"]): int(row["replicas"]) for row in state_rows
            }
            if snapshot is not None:
                snapshot_replicas = {
                    name: int(item["replicas"])
                    for name, item in snapshot.get("workloads", {}).items()
                }
                if state_replicas != snapshot_replicas:
                    violations.append(
                        {
                            "code": "SNAPSHOT_REPLICA_MISMATCH",
                            "state": state_replicas,
                            "snapshot": snapshot_replicas,
                        }
                    )
                runtime_state = self.load_runtime(connection)
                for workload_name, state in sorted(runtime_state.items()):
                    snapshot_workload = snapshot.get("workloads", {}).get(workload_name)
                    if snapshot_workload is None:
                        violations.append(
                            {
                                "code": "SNAPSHOT_WORKLOAD_MISSING",
                                "workload": workload_name,
                            }
                        )
                        continue
                    if bool(snapshot_workload.get("precision_sensitive")) != bool(
                        state.get("precision_sensitive")
                    ):
                        violations.append(
                            {
                                "code": "PRECISION_FLAG_MISMATCH",
                                "workload": workload_name,
                            }
                        )
                    if snapshot_workload.get("precision_flags", []) != state.get(
                        "precision_flags", []
                    ):
                        violations.append(
                            {
                                "code": "PRECISION_DETAIL_MISMATCH",
                                "workload": workload_name,
                            }
                        )
                # Recompute windows from committed samples at the snapshot
                # clock and compare with the cached closed-window values.
                sample_rows = connection.execute(
                    "SELECT * FROM samples WHERE timestamp_ms <= ? ORDER BY sequence_no,sample_id",
                    (int(snapshot.get("at_ms", 0)),),
                ).fetchall()
                samples = [
                    Sample(
                        sample_id=str(row["sample_id"]),
                        metric=str(row["metric"]),
                        timestamp_ms=int(row["timestamp_ms"]),
                        value=dec(row["value_text"]),
                        sequence=int(row["sequence_no"]),
                    )
                    for row in sample_rows
                ]
                try:
                    recomputed = aggregate_windows(
                        bundle.metrics,
                        samples,
                        clock_ms=int(snapshot.get("at_ms", 0)),
                    )
                    latest = latest_window_per_metric(recomputed)
                    for metric, window in latest.items():
                        cached = snapshot.get("windows", {}).get(metric)
                        if cached is not None and dec(cached["aggregate"]) != window.aggregate:
                            violations.append(
                                {
                                    "code": "WINDOW_AGGREGATE_MISMATCH",
                                    "metric": metric,
                                    "cached": cached["aggregate"],
                                    "recomputed": decimal_text(window.aggregate),
                                }
                            )
                except ControlError as exc:
                    violations.append(
                        {
                            "code": "WINDOW_RECOMPUTE_FAILED",
                            "message": exc.message,
                        }
                    )
            action_rows = connection.execute(
                "SELECT * FROM actions ORDER BY action_key"
            ).fetchall()
            for row in action_rows:
                if row["status"] in {
                    "succeeded",
                    "failed_after_start",
                    "started",
                    "running",
                } and not bool(row["budget_charged"]):
                    violations.append(
                        {
                            "code": "UNCHARGED_STARTED_ACTION",
                            "action_key": row["action_key"],
                            "status": row["status"],
                        }
                    )
                if row["status"] in {"not_started", "abandoned"} and bool(
                    row["budget_charged"]
                ):
                    violations.append(
                        {
                            "code": "CHARGED_UNSTARTED_ACTION",
                            "action_key": row["action_key"],
                            "status": row["status"],
                        }
                    )
            decision_rows = connection.execute(
                "SELECT * FROM round_workloads ORDER BY round_id, workload"
            ).fetchall()
            action_by_round_workload = {
                (str(row["round_id"]), str(row["workload"])): row
                for row in action_rows
            }
            for row in decision_rows:
                decision = json.loads(row["decision_json"])
                key = (str(row["round_id"]), str(row["workload"]))
                if int(decision.get("delta", 0)) != 0:
                    action = action_by_round_workload.get(key)
                    if action is None:
                        violations.append(
                            {
                                "code": "DECISION_ACTION_MISSING",
                                "round_id": key[0],
                                "workload": key[1],
                            }
                        )
                    elif int(action["target_replicas"]) != int(
                        decision["target_replicas"]
                    ):
                        violations.append(
                            {
                                "code": "DECISION_ACTION_TARGET_MISMATCH",
                                "round_id": key[0],
                                "workload": key[1],
                                "decision": decision["target_replicas"],
                                "action": int(action["target_replicas"]),
                            }
                        )
                if snapshot is not None and key[0] == snapshot.get("round_id"):
                    cached = (
                        snapshot.get("workloads", {})
                        .get(key[1], {})
                        .get("target_cache")
                    )
                    if cached is not None and int(cached["target_replicas"]) != int(
                        decision["target_replicas"]
                    ):
                        violations.append(
                            {
                                "code": "TARGET_CACHE_DECISION_MISMATCH",
                                "workload": key[1],
                            }
                        )
        return {
            "ok": not violations,
            "violations": violations,
            "checked": {
                "metrics": len(ledger_rows),
                "actions": len(action_rows),
                "workloads": len(state_replicas),
                "budget_charges": len(charge_rows),
                "round_decisions": len(decision_rows),
            },
        }
