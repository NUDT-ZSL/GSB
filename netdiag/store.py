from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .diagnosis import Diagnosis
from .errors import BusinessError
from .models import DiagnosticConfig, ProbeRecord, Scenario, TargetSpec
from .scenario import (
    canonical_json,
    digest_json,
    parse_scenario,
    scenario_digests,
    scenario_payload,
)
from .stats import WindowSummary

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LoadResult:
    config_version: int
    content_digest: str
    config_digest: str
    scenario_digest: str
    changed: bool
    scenario_id: str


@dataclass(frozen=True)
class AttemptClaim:
    attempt_id: str
    recovered: bool


class Store:
    def __init__(
        self,
        db_path: str | Path,
        *,
        lock_timeout_s: float = 20.0,
        initialize: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.lock_dir = self.db_path.parent / f".{self.db_path.name}.locks"
        self.lock_timeout_s = lock_timeout_s
        self._conn: sqlite3.Connection | None = None
        if initialize:
            self.initialize()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config_revisions (
                    version INTEGER PRIMARY KEY,
                    content_digest TEXT NOT NULL UNIQUE,
                    config_digest TEXT NOT NULL,
                    scenario_digest TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    scenario_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config_pointer (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL REFERENCES config_revisions(version),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS targets (
                    config_version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    expected_status INTEGER NOT NULL,
                    period_ms INTEGER NOT NULL,
                    timeout_ms INTEGER NOT NULL,
                    failure_threshold INTEGER NOT NULL,
                    PRIMARY KEY (config_version, name)
                );
                CREATE TABLE IF NOT EXISTS probe_script (
                    config_version INTEGER NOT NULL,
                    probe_seq INTEGER NOT NULL,
                    round_no INTEGER NOT NULL,
                    target_name TEXT NOT NULL,
                    probe_type TEXT NOT NULL,
                    logical_tick INTEGER NOT NULL,
                    plan_index INTEGER NOT NULL,
                    PRIMARY KEY (config_version, probe_seq)
                );
                CREATE TABLE IF NOT EXISTS round_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    config_version INTEGER NOT NULL,
                    round_no INTEGER NOT NULL,
                    target_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    start_clock_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (config_version, round_no, target_name)
                );
                CREATE TABLE IF NOT EXISTS round_commits (
                    commit_id TEXT PRIMARY KEY,
                    config_version INTEGER NOT NULL,
                    round_no INTEGER NOT NULL,
                    target_name TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    clock_before_ms INTEGER NOT NULL,
                    clock_after_ms INTEGER NOT NULL,
                    state_after TEXT NOT NULL,
                    loss_rate REAL,
                    abandoned_count INTEGER NOT NULL,
                    samples_json TEXT NOT NULL,
                    window_summary_json TEXT NOT NULL,
                    decision_chain_json TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    UNIQUE (config_version, round_no, target_name)
                );
                CREATE TABLE IF NOT EXISTS probe_samples (
                    config_version INTEGER NOT NULL,
                    round_no INTEGER NOT NULL,
                    target_name TEXT NOT NULL,
                    probe_seq INTEGER NOT NULL,
                    probe_type TEXT NOT NULL,
                    plan_index INTEGER NOT NULL,
                    logical_start_ms INTEGER NOT NULL,
                    logical_end_ms INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    abandon_kind TEXT,
                    counted_in_loss INTEGER NOT NULL,
                    excluded_from_loss INTEGER NOT NULL,
                    response_status INTEGER,
                    expected_status INTEGER NOT NULL,
                    observed_latency_ms INTEGER,
                    detail TEXT NOT NULL,
                    PRIMARY KEY (config_version, target_name, probe_seq)
                );
                CREATE INDEX IF NOT EXISTS idx_probe_samples_target_order
                    ON probe_samples(config_version, target_name, plan_index);
                CREATE TABLE IF NOT EXISTS window_summaries (
                    config_version INTEGER NOT NULL,
                    target_name TEXT NOT NULL,
                    window_index INTEGER NOT NULL,
                    summary_json TEXT NOT NULL,
                    PRIMARY KEY (config_version, target_name, window_index)
                );
                CREATE TABLE IF NOT EXISTS target_states (
                    target_name TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    fault_domain TEXT NOT NULL,
                    fault_type TEXT NOT NULL,
                    loss_rate REAL,
                    abandoned_count INTEGER NOT NULL,
                    last_round_no INTEGER,
                    evaluated_config_version INTEGER NOT NULL,
                    logical_clock_ms INTEGER NOT NULL,
                    decision_chain_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS round_watermarks (
                    config_version INTEGER NOT NULL,
                    target_name TEXT NOT NULL,
                    last_round_no INTEGER NOT NULL,
                    clock_ms INTEGER NOT NULL,
                    PRIMARY KEY (config_version, target_name)
                );
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            db.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('logical_clock_watermark', '0')"
            )

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    @contextmanager
    def _read_snapshot(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def load_scenario(self, scenario: Scenario) -> LoadResult:
        payload = scenario_payload(scenario)
        content_digest, config_digest, scenario_digest = scenario_digests(scenario)
        scenario_json = canonical_json(payload)
        with self._write_transaction() as db:
            pointer = db.execute(
                "SELECT version FROM config_pointer WHERE singleton = 1"
            ).fetchone()
            if pointer is None:
                version = 1
                row = None
            else:
                version = int(pointer["version"])
                row = db.execute(
                    "SELECT version FROM config_revisions WHERE content_digest = ?",
                    (content_digest,),
                ).fetchone()
                if row is not None:
                    historical_version = int(row["version"])
                    if historical_version != version:
                        raise BusinessError(
                            "CONFIG_ROLLBACK_REJECTED",
                            "loading a historical scenario revision would roll the config back",
                            {
                                "current_version": version,
                                "requested_version": historical_version,
                                "content_digest": content_digest,
                            },
                        )
                    return LoadResult(
                        config_version=version,
                        content_digest=content_digest,
                        config_digest=config_digest,
                        scenario_digest=scenario_digest,
                        changed=False,
                        scenario_id=scenario.scenario_id,
                    )
                version += 1

            db.execute(
                """
                INSERT INTO config_revisions(
                    version, content_digest, config_digest, scenario_digest,
                    scenario_id, scenario_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version,
                    content_digest,
                    config_digest,
                    scenario_digest,
                    scenario.scenario_id,
                    scenario_json,
                    self._now(),
                ),
            )
            for target in scenario.targets:
                db.execute(
                    """
                    INSERT INTO targets(
                        config_version, name, endpoint, expected_status,
                        period_ms, timeout_ms, failure_threshold
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version,
                        target.name,
                        target.endpoint,
                        target.expected_status,
                        target.period_ms,
                        target.timeout_ms,
                        target.failure_threshold,
                    ),
                )
            for round_spec in scenario.rounds:
                for probe in round_spec.probes:
                    db.execute(
                        """
                        INSERT INTO probe_script(
                            config_version, probe_seq, round_no, target_name,
                            probe_type, logical_tick, plan_index
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            version,
                            probe.seq,
                            probe.round_no,
                            probe.target,
                            probe.probe_type,
                            probe.logical_tick,
                            probe.plan_index,
                        ),
                    )
            db.execute(
                """
                INSERT INTO config_pointer(singleton, version, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    version = excluded.version,
                    updated_at = excluded.updated_at
                """,
                (version, self._now()),
            )
            return LoadResult(
                config_version=version,
                content_digest=content_digest,
                config_digest=config_digest,
                scenario_digest=scenario_digest,
                changed=True,
                scenario_id=scenario.scenario_id,
            )

    def current_config_version(self) -> int:
        with self._read_snapshot() as db:
            row = db.execute(
                "SELECT version FROM config_pointer WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise BusinessError(
                "SCENARIO_NOT_LOADED",
                "no scenario has been loaded into this database",
                {"db_path": str(self.db_path)},
            )
        return int(row["version"])

    def current_scenario(self) -> tuple[int, Scenario]:
        with self._read_snapshot() as db:
            row = db.execute(
                """
                SELECT p.version, r.scenario_json
                FROM config_pointer p
                JOIN config_revisions r ON r.version = p.version
                WHERE p.singleton = 1
                """
            ).fetchone()
        if row is None:
            raise BusinessError(
                "SCENARIO_NOT_LOADED",
                "no scenario has been loaded into this database",
                {"db_path": str(self.db_path)},
            )
        return int(row["version"]), parse_scenario(json_loads(row["scenario_json"]))

    def scenario_for_version(self, version: int) -> Scenario:
        with self._read_snapshot() as db:
            row = db.execute(
                "SELECT scenario_json FROM config_revisions WHERE version = ?",
                (version,),
            ).fetchone()
        if row is None:
            raise BusinessError(
                "CONFIG_VERSION_NOT_FOUND",
                "the requested config version does not exist",
                {"config_version": version},
            )
        return parse_scenario(json_loads(row["scenario_json"]))

    def current_digests(self) -> tuple[int, str, str, str]:
        with self._read_snapshot() as db:
            row = db.execute(
                """
                SELECT p.version, r.content_digest, r.config_digest,
                       r.scenario_digest
                FROM config_pointer p
                JOIN config_revisions r ON r.version = p.version
                WHERE p.singleton = 1
                """
            ).fetchone()
        if row is None:
            raise BusinessError(
                "SCENARIO_NOT_LOADED",
                "no scenario has been loaded into this database",
                {"db_path": str(self.db_path)},
            )
        return (
            int(row["version"]),
            row["content_digest"],
            row["config_digest"],
            row["scenario_digest"],
        )

    def logical_clock_watermark(self) -> int:
        with self._read_snapshot() as db:
            row = db.execute(
                "SELECT value FROM schema_meta WHERE key = 'logical_clock_watermark'"
            ).fetchone()
        return int(row["value"]) if row else 0

    def last_committed_round(
        self, config_version: int, target_name: str
    ) -> int | None:
        with self._read_snapshot() as db:
            row = db.execute(
                """
                SELECT last_round_no FROM round_watermarks
                WHERE config_version = ? AND target_name = ?
                """,
                (config_version, target_name),
            ).fetchone()
        return int(row["last_round_no"]) if row else None

    def committed_rounds(
        self, config_version: int, target_name: str
    ) -> set[int]:
        with self._read_snapshot() as db:
            rows = db.execute(
                """
                SELECT round_no FROM round_commits
                WHERE config_version = ? AND target_name = ?
                """,
                (config_version, target_name),
            ).fetchall()
        return {int(row["round_no"]) for row in rows}

    def round_commit(
        self, config_version: int, round_no: int, target_name: str
    ) -> dict[str, Any] | None:
        with self._read_snapshot() as db:
            row = db.execute(
                """
                SELECT * FROM round_commits
                WHERE config_version = ? AND round_no = ? AND target_name = ?
                """,
                (config_version, round_no, target_name),
            ).fetchone()
        return dict(row) if row else None

    def prepare_attempt(
        self,
        *,
        config_version: int,
        round_no: int,
        target_name: str,
        start_clock_ms: int,
    ) -> AttemptClaim | None:
        attempt_id = digest_json(
            ["attempt", config_version, round_no, target_name]
        )
        with self._write_transaction() as db:
            committed = db.execute(
                """
                SELECT commit_id FROM round_commits
                WHERE config_version = ? AND round_no = ? AND target_name = ?
                """,
                (config_version, round_no, target_name),
            ).fetchone()
            if committed is not None:
                return None
            existing = db.execute(
                """
                SELECT status FROM round_attempts
                WHERE config_version = ? AND round_no = ? AND target_name = ?
                """,
                (config_version, round_no, target_name),
            ).fetchone()
            if existing is not None:
                db.execute(
                    """
                    UPDATE round_attempts
                    SET status = 'recovering', updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (self._now(), attempt_id),
                )
                return AttemptClaim(attempt_id=attempt_id, recovered=True)
            db.execute(
                """
                INSERT INTO round_attempts(
                    attempt_id, config_version, round_no, target_name, status,
                    start_clock_ms, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?)
                """,
                (
                    attempt_id,
                    config_version,
                    round_no,
                    target_name,
                    start_clock_ms,
                    self._now(),
                    self._now(),
                ),
            )
        return AttemptClaim(attempt_id=attempt_id, recovered=False)

    def pending_attempts(self) -> list[dict[str, Any]]:
        with self._read_snapshot() as db:
            rows = db.execute(
                """
                SELECT attempt_id, config_version, round_no, target_name,
                       status, start_clock_ms
                FROM round_attempts
                WHERE status IN ('prepared', 'recovering')
                ORDER BY config_version, round_no, target_name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_records(
        self,
        config_version: int,
        target_name: str,
        *,
        up_to_round: int | None = None,
    ) -> list[ProbeRecord]:
        query = """
            SELECT * FROM probe_samples
            WHERE config_version = ? AND target_name = ?
        """
        params: list[Any] = [config_version, target_name]
        if up_to_round is not None:
            query += " AND round_no <= ?"
            params.append(up_to_round)
        query += " ORDER BY plan_index, probe_seq"
        with self._read_snapshot() as db:
            rows = db.execute(query, params).fetchall()
        return [_record_from_row(row) for row in rows]

    def fetch_round_records(
        self, config_version: int, round_no: int, target_name: str
    ) -> list[ProbeRecord]:
        with self._read_snapshot() as db:
            rows = db.execute(
                """
                SELECT * FROM probe_samples
                WHERE config_version = ? AND round_no = ? AND target_name = ?
                ORDER BY plan_index, probe_seq
                """,
                (config_version, round_no, target_name),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def round_clock_before(
        self, config_version: int, round_no: int
    ) -> int | None:
        with self._read_snapshot() as db:
            row = db.execute(
                """
                SELECT clock_before_ms FROM round_commits
                WHERE config_version = ? AND round_no = ?
                ORDER BY target_name
                LIMIT 1
                """,
                (config_version, round_no),
            ).fetchone()
        return int(row["clock_before_ms"]) if row else None

    def commit_round(
        self,
        *,
        attempt_id: str,
        config_version: int,
        round_no: int,
        target: TargetSpec,
        config: DiagnosticConfig,
        records: Sequence[ProbeRecord],
        windows: Sequence[WindowSummary],
        diagnosis: Diagnosis,
        clock_before_ms: int,
        clock_after_ms: int,
    ) -> dict[str, Any]:
        record_dicts = [record.as_dict() for record in records]
        window_dicts = [window.as_dict() for window in windows]
        payload = {
            "config_version": config_version,
            "round_no": round_no,
            "target": target.name,
            "records": record_dicts,
            "windows": window_dicts,
            "decision_chain": diagnosis.decision_chain,
        }
        payload_digest = digest_json(payload)
        commit_id = digest_json(["commit", payload])
        with self._write_transaction() as db:
            existing = db.execute(
                """
                SELECT * FROM round_commits
                WHERE config_version = ? AND round_no = ? AND target_name = ?
                """,
                (config_version, round_no, target.name),
            ).fetchone()
            if existing is not None:
                if existing["payload_digest"] != payload_digest:
                    raise BusinessError(
                        "IDEMPOTENCY_CONFLICT",
                        "the same target/round/config version was committed with different content",
                        {
                            "config_version": config_version,
                            "round_no": round_no,
                            "target": target.name,
                            "stored_payload_digest": existing["payload_digest"],
                            "incoming_payload_digest": payload_digest,
                        },
                    )
                return dict(existing)

            attempt = db.execute(
                "SELECT status FROM round_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise BusinessError(
                    "ROUND_ATTEMPT_MISSING",
                    "round attempt must be prepared before commit",
                    {
                        "attempt_id": attempt_id,
                        "config_version": config_version,
                        "round_no": round_no,
                        "target": target.name,
                    },
                )

            duplicate = None
            for record in records:
                duplicate = db.execute(
                    """
                    SELECT 1 FROM probe_samples
                    WHERE config_version = ? AND target_name = ? AND probe_seq = ?
                    LIMIT 1
                    """,
                    (config_version, target.name, record.seq),
                ).fetchone()
                if duplicate is not None:
                    break
            if duplicate is not None:
                raise BusinessError(
                    "DUPLICATE_WINDOW_SAMPLE",
                    "probe samples for this target/config already exist",
                    {
                        "config_version": config_version,
                        "target": target.name,
                        "round_no": round_no,
                    },
                )

            samples_json = canonical_json(record_dicts)
            windows_json = canonical_json(window_dicts)
            decision_json = canonical_json(diagnosis.decision_chain)
            for record in records:
                db.execute(
                    """
                    INSERT INTO probe_samples(
                        config_version, round_no, target_name, probe_seq,
                        probe_type, plan_index, logical_start_ms, logical_end_ms,
                        outcome, abandon_kind, counted_in_loss, excluded_from_loss,
                        response_status, expected_status, observed_latency_ms, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.config_version,
                        record.round_no,
                        record.target,
                        record.seq,
                        record.probe_type,
                        record.plan_index,
                        record.logical_start_ms,
                        record.logical_end_ms,
                        record.outcome,
                        record.abandon_kind,
                        int(record.counted_in_loss),
                        int(record.excluded_from_loss),
                        record.response_status,
                        record.expected_status,
                        record.observed_latency_ms,
                        record.detail,
                    ),
                )
            db.execute(
                """
                DELETE FROM window_summaries
                WHERE config_version = ? AND target_name = ?
                """,
                (config_version, target.name),
            )
            for window in windows:
                db.execute(
                    """
                    INSERT INTO window_summaries(
                        config_version, target_name, window_index, summary_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        config_version,
                        target.name,
                        window.window_index,
                        canonical_json(window.as_dict()),
                    ),
                )
            _test_crash("commit_mid")
            db.execute(
                """
                INSERT INTO round_commits(
                    commit_id, config_version, round_no, target_name,
                    payload_digest, clock_before_ms, clock_after_ms,
                    state_after, loss_rate, abandoned_count, samples_json,
                    window_summary_json, decision_chain_json, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_id,
                    config_version,
                    round_no,
                    target.name,
                    payload_digest,
                    clock_before_ms,
                    clock_after_ms,
                    diagnosis.state,
                    diagnosis.loss_rate,
                    diagnosis.abandoned_count,
                    samples_json,
                    windows_json,
                    decision_json,
                    self._now(),
                ),
            )
            db.execute(
                """
                INSERT INTO target_states(
                    target_name, state, fault_domain, fault_type, loss_rate,
                    abandoned_count, last_round_no, evaluated_config_version,
                    logical_clock_ms, decision_chain_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_name) DO UPDATE SET
                    state = excluded.state,
                    fault_domain = excluded.fault_domain,
                    fault_type = excluded.fault_type,
                    loss_rate = excluded.loss_rate,
                    abandoned_count = excluded.abandoned_count,
                    last_round_no = excluded.last_round_no,
                    evaluated_config_version = excluded.evaluated_config_version,
                    logical_clock_ms = excluded.logical_clock_ms,
                    decision_chain_json = excluded.decision_chain_json,
                    updated_at = excluded.updated_at
                WHERE excluded.evaluated_config_version
                    >= target_states.evaluated_config_version
                """,
                (
                    target.name,
                    diagnosis.state,
                    diagnosis.fault_domain,
                    diagnosis.fault_type,
                    diagnosis.loss_rate,
                    diagnosis.abandoned_count,
                    round_no,
                    config_version,
                    clock_after_ms,
                    decision_json,
                    self._now(),
                ),
            )
            watermark = db.execute(
                """
                SELECT last_round_no FROM round_watermarks
                WHERE config_version = ? AND target_name = ?
                """,
                (config_version, target.name),
            ).fetchone()
            previous_round = int(watermark["last_round_no"]) if watermark else None
            next_round = (
                round_no
                if previous_round is None
                else max(previous_round, round_no)
            )
            db.execute(
                """
                INSERT INTO round_watermarks(
                    config_version, target_name, last_round_no, clock_ms
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(config_version, target_name) DO UPDATE SET
                    last_round_no = excluded.last_round_no,
                    clock_ms = MAX(round_watermarks.clock_ms, excluded.clock_ms)
                """,
                (config_version, target.name, next_round, clock_after_ms),
            )
            clock_row = db.execute(
                "SELECT value FROM schema_meta WHERE key = 'logical_clock_watermark'"
            ).fetchone()
            old_clock = int(clock_row["value"]) if clock_row else 0
            if clock_after_ms > old_clock:
                db.execute(
                    """
                    UPDATE schema_meta SET value = ?
                    WHERE key = 'logical_clock_watermark'
                    """,
                    (str(clock_after_ms),),
                )
            db.execute(
                """
                UPDATE round_attempts
                SET status = 'committed', updated_at = ?
                WHERE attempt_id = ?
                """,
                (self._now(), attempt_id),
            )
            row = db.execute(
                "SELECT * FROM round_commits WHERE commit_id = ?", (commit_id,)
            ).fetchone()
        return dict(row)

    def status(self) -> dict[str, Any]:
        config_version, scenario = self.current_scenario()
        watermark = self.logical_clock_watermark()
        with self._read_snapshot() as db:
            rows = db.execute(
                """
                SELECT target_name, state, fault_domain, fault_type, loss_rate,
                       abandoned_count, last_round_no, evaluated_config_version,
                       logical_clock_ms
                FROM target_states
                ORDER BY target_name
                """
            ).fetchall()
        states = {row["target_name"]: dict(row) for row in rows}
        targets: list[dict[str, Any]] = []
        for target in sorted(scenario.targets, key=lambda item: item.name):
            persisted = states.get(target.name)
            targets.append(
                {
                    "target": target.name,
                    "state": persisted["state"] if persisted else "suspicious",
                    "fault_domain": (
                        persisted["fault_domain"]
                        if persisted
                        else "scheduler_budget"
                    ),
                    "fault_type": (
                        persisted["fault_type"]
                        if persisted
                        else "no_observations"
                    ),
                    "loss_rate": persisted["loss_rate"] if persisted else None,
                    "abandoned_count": (
                        persisted["abandoned_count"] if persisted else 0
                    ),
                    "last_round_no": (
                        persisted["last_round_no"] if persisted else None
                    ),
                    "evaluated_config_version": (
                        persisted["evaluated_config_version"]
                        if persisted
                        else None
                    ),
                    "logical_clock_ms": (
                        persisted["logical_clock_ms"] if persisted else 0
                    ),
                }
            )
        return {
            "current_config_version": config_version,
            "scenario_id": scenario.scenario_id,
            "logical_clock_watermark_ms": watermark,
            "pending_attempts": len(self.pending_attempts()),
            "targets": targets,
        }

    def window_summaries(
        self, config_version: int, target_name: str
    ) -> list[dict[str, Any]]:
        with self._read_snapshot() as db:
            rows = db.execute(
                """
                SELECT summary_json FROM window_summaries
                WHERE config_version = ? AND target_name = ?
                ORDER BY window_index
                """,
                (config_version, target_name),
            ).fetchall()
        return [json_loads(row["summary_json"]) for row in rows]

    def all_round_commits(self, config_version: int) -> list[dict[str, Any]]:
        with self._read_snapshot() as db:
            rows = db.execute(
                """
                SELECT * FROM round_commits
                WHERE config_version = ?
                ORDER BY target_name, round_no
                """,
                (config_version,),
            ).fetchall()
        return [dict(row) for row in rows]

    def target_state(self, target_name: str) -> dict[str, Any] | None:
        with self._read_snapshot() as db:
            row = db.execute(
                "SELECT * FROM target_states WHERE target_name = ?",
                (target_name,),
            ).fetchone()
        return dict(row) if row else None


def json_loads(text: str) -> Any:
    import json

    return json.loads(text)


def _record_from_row(row: sqlite3.Row) -> ProbeRecord:
    return ProbeRecord(
        config_version=int(row["config_version"]),
        round_no=int(row["round_no"]),
        target=row["target_name"],
        seq=int(row["probe_seq"]),
        probe_type=row["probe_type"],
        plan_index=int(row["plan_index"]),
        logical_start_ms=int(row["logical_start_ms"]),
        logical_end_ms=int(row["logical_end_ms"]),
        outcome=row["outcome"],
        abandon_kind=row["abandon_kind"],
        counted_in_loss=bool(row["counted_in_loss"]),
        excluded_from_loss=bool(row["excluded_from_loss"]),
        response_status=row["response_status"],
        expected_status=int(row["expected_status"]),
        observed_latency_ms=row["observed_latency_ms"],
        detail=row["detail"],
    )


def _test_crash(point: str) -> None:
    if os.environ.get("NETDIAG_TEST_CRASH_AT") == point:
        os._exit(75)
