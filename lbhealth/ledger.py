"""SQLite ledger with atomic, complete-round-only recovery semantics."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .config import Config
from .errors import LedgerError
from .models import RoundResult, RuntimeState


SCHEMA = """
CREATE TABLE IF NOT EXISTS round_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('incomplete', 'complete', 'unknown')),
    config_version TEXT NOT NULL,
    member_digest TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    full_weight_sum INTEGER NOT NULL,
    started_mono_ms INTEGER NOT NULL,
    started_utc TEXT NOT NULL,
    completed_mono_ms INTEGER,
    completed_utc TEXT,
    result_json TEXT,
    state_json TEXT,
    available_json TEXT,
    weights_json TEXT,
    effective_weight_sum INTEGER,
    error TEXT,
    UNIQUE (round_no, attempt_no)
);

CREATE TABLE IF NOT EXISTS probes (
    attempt_id INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    member_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    ok INTEGER NOT NULL,
    start_mono_ms INTEGER NOT NULL,
    end_mono_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    summary TEXT NOT NULL,
    failure_code TEXT,
    PRIMARY KEY (attempt_id, member_id),
    FOREIGN KEY (attempt_id) REFERENCES round_attempts(attempt_id)
);

CREATE TABLE IF NOT EXISTS transitions (
    attempt_id INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    member_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_mono_ms INTEGER NOT NULL,
    occurred_utc TEXT NOT NULL,
    PRIMARY KEY (attempt_id, ordinal),
    FOREIGN KEY (attempt_id) REFERENCES round_attempts(attempt_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_utc TEXT NOT NULL,
    label TEXT NOT NULL,
    config_version TEXT NOT NULL,
    member_digest TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    available_json TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    probe_sequence_json TEXT NOT NULL,
    recent_rounds_json TEXT NOT NULL,
    replay_baseline_json TEXT,
    config_json TEXT NOT NULL,
    replay_context_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_round_status ON round_attempts(status, round_no);
CREATE INDEX IF NOT EXISTS idx_probe_round ON probes(round_no, member_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_round ON snapshots(round_no, snapshot_id);
"""


@dataclass
class IncompleteRound:
    round_no: int
    attempt_no: int
    started_utc: str
    phase: str = "started before probes; no probe result was persisted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_no,
            "attempt": self.attempt_no,
            "status": "unknown",
            "started_utc": self.started_utc,
            "phase": self.phase,
            "action": "rerun required",
        }


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
            conn.commit()

    def begin_round(
        self,
        round_no: int,
        config: Config,
        started_mono_ms: int,
        started_utc: str,
    ) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                complete = conn.execute(
                    "SELECT 1 FROM round_attempts WHERE round_no = ? AND status = 'complete'",
                    (round_no,),
                ).fetchone()
                if complete is not None:
                    raise LedgerError(
                        "E_LEDGER_ROUND",
                        f"round {round_no} is already complete and cannot be rewritten",
                    )
                row = conn.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next FROM round_attempts "
                    "WHERE round_no = ?",
                    (round_no,),
                ).fetchone()
                attempt_no = int(row["next"])
                cursor = conn.execute(
                    """
                    INSERT INTO round_attempts (
                        round_no, attempt_no, status, config_version, member_digest,
                        config_digest, full_weight_sum, started_mono_ms, started_utc
                    ) VALUES (?, ?, 'incomplete', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        round_no,
                        attempt_no,
                        config.config_version,
                        config.member_digest,
                        config.config_digest,
                        config.pool.full_weight_sum,
                        started_mono_ms,
                        started_utc,
                    ),
                )
                attempt_id = int(cursor.lastrowid)
                conn.commit()
                return attempt_id
            except BaseException:
                conn.rollback()
                raise

    def complete_round(
        self,
        attempt_id: int,
        config: Config,
        result: RoundResult,
        completed_mono_ms: int,
        completed_utc: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT round_no, status, config_version, member_digest, config_digest "
                    "FROM round_attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                if row is None:
                    raise LedgerError("E_LEDGER_ATTEMPT", f"unknown attempt_id {attempt_id}")
                if row["status"] != "incomplete":
                    raise LedgerError(
                        "E_LEDGER_ATTEMPT",
                        f"attempt {attempt_id} is not incomplete and cannot be completed",
                    )
                if (
                    row["config_version"] != config.config_version
                    or row["member_digest"] != config.member_digest
                    or row["config_digest"] != config.config_digest
                ):
                    raise LedgerError(
                        "E_LEDGER_CONFIG",
                        "configuration changed while the round was in flight",
                    )
                for probe in result.probes:
                    conn.execute(
                        """
                        INSERT INTO probes (
                            attempt_id, round_no, member_id, protocol, ok,
                            start_mono_ms, end_mono_ms, duration_ms, summary, failure_code
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            result.round_no,
                            probe.member_id,
                            probe.protocol,
                            1 if probe.ok else 0,
                            probe.start_mono_ms,
                            probe.end_mono_ms,
                            probe.duration_ms,
                            probe.summary,
                            probe.failure_code,
                        ),
                    )
                for ordinal, transition in enumerate(result.transitions):
                    conn.execute(
                        """
                        INSERT INTO transitions (
                            attempt_id, round_no, ordinal, member_id, from_state,
                            to_state, reason, occurred_mono_ms, occurred_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            result.round_no,
                            ordinal,
                            transition.member_id,
                            transition.from_state.value,
                            transition.to_state.value,
                            transition.reason,
                            transition.occurred_mono_ms,
                            transition.occurred_utc,
                        ),
                    )
                state = result.state_after
                conn.execute(
                    """
                    UPDATE round_attempts SET
                        status = 'complete',
                        completed_mono_ms = ?,
                        completed_utc = ?,
                        result_json = ?,
                        state_json = ?,
                        available_json = ?,
                        weights_json = ?,
                        effective_weight_sum = ?,
                        error = NULL
                    WHERE attempt_id = ?
                    """,
                    (
                        completed_mono_ms,
                        completed_utc,
                        _json(result.to_dict()),
                        _json(state.to_dict()),
                        _json(list(result.available_members)),
                        _json(result.effective_weights),
                        result.effective_weight_sum,
                        attempt_id,
                    ),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def recover_incomplete(self) -> list[IncompleteRound]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT round_no, attempt_no, started_utc FROM round_attempts "
                    "WHERE status = 'incomplete' ORDER BY round_no, attempt_no"
                ).fetchall()
                incomplete = [
                    IncompleteRound(
                        round_no=int(row["round_no"]),
                        attempt_no=int(row["attempt_no"]),
                        started_utc=str(row["started_utc"]),
                    )
                    for row in rows
                ]
                conn.execute(
                    "UPDATE round_attempts SET status = 'unknown', "
                    "error = 'process terminated before complete-round commit; rerun required' "
                    "WHERE status = 'incomplete'"
                )
                conn.commit()
                return incomplete
            except BaseException:
                conn.rollback()
                raise

    def unresolved_unknowns(self) -> list[IncompleteRound]:
        """Return unknown attempts for rounds that still have no complete attempt."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT u.round_no, u.attempt_no, u.started_utc
                FROM round_attempts AS u
                WHERE u.status = 'unknown'
                  AND NOT EXISTS (
                      SELECT 1 FROM round_attempts AS c
                      WHERE c.round_no = u.round_no AND c.status = 'complete'
                  )
                ORDER BY u.round_no, u.attempt_no
                """
            ).fetchall()
        return [
            IncompleteRound(
                round_no=int(row["round_no"]),
                attempt_no=int(row["attempt_no"]),
                started_utc=str(row["started_utc"]),
            )
            for row in rows
        ]

    def latest_complete(self) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM round_attempts WHERE status = 'complete' "
                "ORDER BY round_no DESC, attempt_no DESC LIMIT 1"
            ).fetchone()

    def complete_rounds_upto(self, round_no: int, limit: int | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM round_attempts WHERE status = 'complete' AND round_no <= ? "
            "ORDER BY round_no ASC, attempt_no ASC"
        )
        params: list[Any] = [round_no]
        if limit is not None:
            sql = (
                "SELECT * FROM (SELECT * FROM round_attempts WHERE status = 'complete' "
                "AND round_no <= ? ORDER BY round_no DESC, attempt_no DESC LIMIT ?) "
                "ORDER BY round_no ASC, attempt_no ASC"
            )
            params.append(limit)
        with self._connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    def round_result(self, round_no: int) -> RoundResult | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM round_attempts WHERE round_no = ? AND status = 'complete' "
                "ORDER BY attempt_no DESC LIMIT 1",
                (round_no,),
            ).fetchone()
        if row is None or row["result_json"] is None:
            return None
        return RoundResult.from_dict(json.loads(row["result_json"]))

    def latest_state_before(self, round_no: int) -> RuntimeState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM round_attempts WHERE status = 'complete' "
                "AND round_no < ? ORDER BY round_no DESC, attempt_no DESC LIMIT 1",
                (round_no,),
            ).fetchone()
        if row is None or row["state_json"] is None:
            return None
        return RuntimeState.from_dict(json.loads(row["state_json"]))

    def next_round_no(self) -> int:
        latest = self.latest_complete()
        return 1 if latest is None else int(latest["round_no"]) + 1

    def round_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT round_no, attempt_no, status, config_version, member_digest, "
                "started_utc, completed_utc, available_json, weights_json, error "
                "FROM round_attempts ORDER BY round_no DESC, attempt_no DESC LIMIT ?",
                (limit,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "round": int(row["round_no"]),
                    "attempt": int(row["attempt_no"]),
                    "status": str(row["status"]),
                    "config_version": str(row["config_version"]),
                    "member_digest": str(row["member_digest"]),
                    "started_utc": str(row["started_utc"]),
                    "completed_utc": row["completed_utc"],
                    "available_members": _load(row["available_json"], []),
                    "effective_weights": _load(row["weights_json"], {}),
                    "error": row["error"],
                }
            )
        return output

    def insert_snapshot(
        self,
        *,
        created_utc: str,
        label: str,
        config: Config,
        round_no: int,
        state: RuntimeState,
        probe_sequence: list[dict[str, Any]],
        recent_rounds: list[dict[str, Any]],
        replay_baseline: RuntimeState | None,
        replay_context: dict[str, Any] | None,
    ) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO snapshots (
                        created_utc, label, config_version, member_digest, config_digest,
                        round_no, state_json, available_json, weights_json,
                        probe_sequence_json, recent_rounds_json, replay_baseline_json,
                        config_json, replay_context_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        created_utc,
                        label,
                        config.config_version,
                        config.member_digest,
                        config.config_digest,
                        round_no,
                        _json(state.to_dict()),
                        _json(list(state.available_members)),
                        _json(state.effective_weights),
                        _json(probe_sequence),
                        _json(recent_rounds),
                        _json(replay_baseline.to_dict()) if replay_baseline is not None else None,
                        _json(config.raw),
                        _json(replay_context or {}),
                    ),
                )
                snapshot_id = int(cursor.lastrowid)
                conn.commit()
                return snapshot_id
            except BaseException:
                conn.rollback()
                raise

    def get_snapshot_row(self, snapshot_id: int) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()

    def list_snapshot_rows(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM snapshots ORDER BY snapshot_id ASC"
                ).fetchall()
            )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)
