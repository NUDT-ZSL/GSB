from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json, digest_value, extend_chain
from .errors import E_DB_CORRUPT, E_RUN_NOT_FOUND, StorageError
from .scenario import Scenario


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    scenario_hash TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    scenario_json TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    committed_seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    final_state_digest TEXT,
    final_trajectory_digest TEXT,
    final_ledger_digest TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at_ms INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    event_json TEXT,
    event_digest TEXT,
    ledger_json TEXT NOT NULL,
    ledger_digest TEXT NOT NULL,
    trajectory_digest TEXT NOT NULL,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS snapshots_run_seq
    ON snapshots(run_id, seq);
"""


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None

    def open(self, create: bool) -> None:
        if not create and not self.path.exists():
            raise StorageError(
                E_RUN_NOT_FOUND,
                "database file does not exist",
                {"db": str(self.path)},
            )
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=30.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            self._connection = connection
            self._ensure_schema()
        except sqlite3.Error as exc:
            raise StorageError(
                E_DB_CORRUPT,
                f"cannot open SQLite database: {exc}",
                {"db": str(self.path)},
            ) from exc

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("storage is not open")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def _ensure_schema(self) -> None:
        connection = self.connection
        connection.executescript(SCHEMA_SQL)
        with self.transaction():
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise StorageError(
                    E_DB_CORRUPT,
                    "unsupported database schema version",
                    {"found": row["value"], "expected": SCHEMA_VERSION},
                )

    @staticmethod
    def _validate_snapshot_payload(
        snapshot: dict[str, Any], previous_ledger_digest: str | None = None
    ) -> None:
        try:
            state_value = json.loads(snapshot["state_json"])
            ledger_value = json.loads(snapshot["ledger_json"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError(
                E_DB_CORRUPT,
                "snapshot payload is not valid JSON",
                {"seq": snapshot.get("seq")},
            ) from exc
        if digest_value(state_value) != snapshot["state_digest"]:
            raise StorageError(
                E_DB_CORRUPT,
                "snapshot state_digest does not match state_json",
                {"seq": snapshot.get("seq")},
            )
        if previous_ledger_digest is None:
            expected_ledger_digest = digest_value(ledger_value)
        else:
            expected_ledger_digest = extend_chain(
                previous_ledger_digest, canonical_json(ledger_value)
            )
        if expected_ledger_digest != snapshot["ledger_digest"]:
            raise StorageError(
                E_DB_CORRUPT,
                "snapshot ledger_digest does not match ledger_json",
                {"seq": snapshot.get("seq")},
            )
        event_json = snapshot.get("event_json")
        event_digest = snapshot.get("event_digest")
        if event_json is None:
            if event_digest is not None:
                raise StorageError(
                    E_DB_CORRUPT,
                    "initial snapshot unexpectedly has an event digest",
                    {"seq": snapshot.get("seq")},
                )
        else:
            if digest_value(json.loads(event_json)) != event_digest:
                raise StorageError(
                    E_DB_CORRUPT,
                    "snapshot event_digest does not match event_json",
                    {"seq": snapshot.get("seq")},
                )

    def create_run(
        self, run_id: str, scenario: Scenario, initial_snapshot: dict[str, Any]
    ) -> None:
        self._validate_snapshot_payload(initial_snapshot)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is not None:
                raise StorageError(
                    "E_RUN_EXISTS",
                    f"run {run_id!r} already exists",
                    {"run_id": run_id},
                )
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, scenario_hash, scenario_id, scenario_json,
                    event_count, committed_seq, status
                ) VALUES(?, ?, ?, ?, ?, 0, 'running')
                """,
                (
                    run_id,
                    scenario.canonical_hash,
                    scenario.scenario_id,
                    canonical_json(scenario.raw),
                    len(scenario.events),
                ),
            )
            connection.execute(
                """
                INSERT INTO snapshots(
                    run_id, seq, at_ms, state_json, state_digest,
                    event_json, event_digest, ledger_json, ledger_digest,
                    trajectory_digest
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    initial_snapshot["seq"],
                    initial_snapshot["at_ms"],
                    initial_snapshot["state_json"],
                    initial_snapshot["state_digest"],
                    initial_snapshot["event_json"],
                    initial_snapshot["event_digest"],
                    initial_snapshot["ledger_json"],
                    initial_snapshot["ledger_digest"],
                    initial_snapshot["trajectory_digest"],
                ),
            )

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def load_snapshot(self, run_id: str, seq: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE run_id=? AND seq=?",
            (run_id, seq),
        ).fetchone()
        return dict(row) if row is not None else None

    def load_snapshots(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM snapshots WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def read_status_records(
        self, run_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.transaction(immediate=False):
            run = self.load_run(run_id)
            if run is None:
                raise StorageError(
                    E_RUN_NOT_FOUND,
                    f"run {run_id!r} was not found",
                    {"run_id": run_id},
                )
            snapshot = self.load_snapshot(run_id, int(run["committed_seq"]))
            if snapshot is None:
                raise StorageError(
                    E_DB_CORRUPT,
                    "committed snapshot is missing",
                    {"run_id": run_id, "seq": run["committed_seq"]},
                )
            return run, snapshot

    def read_run_and_snapshots(
        self, run_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self.transaction(immediate=False):
            run = self.load_run(run_id)
            if run is None:
                raise StorageError(
                    E_RUN_NOT_FOUND,
                    f"run {run_id!r} was not found",
                    {"run_id": run_id},
                )
            snapshots = self.load_snapshots(run_id)
            return run, snapshots

    def commit_frame(
        self,
        run_id: str,
        snapshot: dict[str, Any],
        crash_before_commit: int | None = None,
        crash_after_commit: int | None = None,
        pause_before_commit_ms: int = 0,
        pause_at_seq: int | None = None,
    ) -> bool:
        seq = int(snapshot["seq"])
        committed = False
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT committed_seq, event_count, status "
                "FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise StorageError(
                    E_RUN_NOT_FOUND,
                    f"run {run_id!r} was not found",
                    {"run_id": run_id},
                )
            if seq <= int(run["committed_seq"]):
                return False
            if seq != int(run["committed_seq"]) + 1:
                raise StorageError(
                    E_DB_CORRUPT,
                    "frame sequence would create a trajectory gap",
                    {
                        "committed_seq": run["committed_seq"],
                        "seq": seq,
                        "run_id": run_id,
                    },
                )
            if seq > int(run["event_count"]):
                raise StorageError(
                    E_DB_CORRUPT,
                    "frame sequence exceeds scenario event count",
                    {"event_count": run["event_count"], "seq": seq},
                )
            previous = connection.execute(
                "SELECT ledger_digest FROM snapshots "
                "WHERE run_id=? AND seq=?",
                (run_id, seq - 1),
            ).fetchone()
            if previous is None:
                raise StorageError(
                    E_DB_CORRUPT,
                    "previous snapshot is missing before frame commit",
                    {"run_id": run_id, "seq": seq},
                )
            self._validate_snapshot_payload(
                snapshot, previous_ledger_digest=str(previous["ledger_digest"])
            )
            existing = connection.execute(
                "SELECT 1 FROM snapshots WHERE run_id=? AND seq=?",
                (run_id, seq),
            ).fetchone()
            if existing is not None:
                raise StorageError(
                    E_DB_CORRUPT,
                    "uncommitted snapshot row is unexpectedly visible",
                    {"run_id": run_id, "seq": seq},
                )
            connection.execute(
                """
                INSERT INTO snapshots(
                    run_id, seq, at_ms, state_json, state_digest,
                    event_json, event_digest, ledger_json, ledger_digest,
                    trajectory_digest
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    seq,
                    snapshot["at_ms"],
                    snapshot["state_json"],
                    snapshot["state_digest"],
                    snapshot["event_json"],
                    snapshot["event_digest"],
                    snapshot["ledger_json"],
                    snapshot["ledger_digest"],
                    snapshot["trajectory_digest"],
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET committed_seq=?, final_state_digest=?,
                    final_trajectory_digest=?, final_ledger_digest=?
                WHERE run_id=?
                """,
                (
                    seq,
                    snapshot["state_digest"],
                    snapshot["trajectory_digest"],
                    snapshot["ledger_digest"],
                    run_id,
                ),
            )
            if pause_before_commit_ms > 0 and (
                pause_at_seq is None or pause_at_seq == seq
            ):
                time.sleep(pause_before_commit_ms / 1000.0)
            if crash_before_commit == seq:
                os._exit(91)
            committed = True
        if crash_after_commit == seq:
            os._exit(92)
        return committed

    def finalize_run(
        self,
        run_id: str,
        final_state_digest: str,
        final_trajectory_digest: str,
        final_ledger_digest: str,
    ) -> None:
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT committed_seq, event_count, status "
                "FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise StorageError(
                    E_RUN_NOT_FOUND,
                    f"run {run_id!r} was not found",
                    {"run_id": run_id},
                )
            if int(run["committed_seq"]) != int(run["event_count"]):
                raise StorageError(
                    "E_INCOMPLETE_RUN",
                    "cannot finalize before all frames are committed",
                    {
                        "committed_seq": run["committed_seq"],
                        "event_count": run["event_count"],
                    },
                )
            connection.execute(
                """
                UPDATE runs
                SET status='complete', final_state_digest=?,
                    final_trajectory_digest=?, final_ledger_digest=?
                WHERE run_id=?
                """,
                (
                    final_state_digest,
                    final_trajectory_digest,
                    final_ledger_digest,
                    run_id,
                ),
            )

    def integrity_check(self) -> str:
        row = self.connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row is not None else "unknown"

    def status(self, run_id: str) -> dict[str, Any]:
        from .engine import Engine, EngineState
        from .scenario import parse_scenario

        run, snapshot = self.read_status_records(run_id)
        scenario = parse_scenario(json.loads(run["scenario_json"]))
        state = EngineState.from_dict(json.loads(snapshot["state_json"]))
        if state.digest() != snapshot["state_digest"]:
            raise StorageError(
                E_DB_CORRUPT,
                "snapshot payload does not match state_digest",
                {"run_id": run_id, "seq": run["committed_seq"]},
            )
        ledger_value = json.loads(snapshot["ledger_json"])
        if int(snapshot["seq"]) == 0:
            expected_ledger_digest = digest_value(ledger_value)
        else:
            previous = self.load_snapshot(run_id, int(snapshot["seq"]) - 1)
            if previous is None:
                raise StorageError(
                    E_DB_CORRUPT,
                    "previous snapshot is missing for ledger verification",
                    {"run_id": run_id, "seq": snapshot["seq"]},
                )
            expected_ledger_digest = extend_chain(
                str(previous["ledger_digest"]), canonical_json(ledger_value)
            )
        if expected_ledger_digest != snapshot["ledger_digest"]:
            raise StorageError(
                E_DB_CORRUPT,
                "snapshot payload does not match ledger_digest",
                {"run_id": run_id, "seq": run["committed_seq"]},
            )
        engine = Engine(scenario, state)
        return {
            "connection": {
                "bytes_received_total": state.connection.bytes_received_total,
                "bytes_sent_total": state.connection.bytes_sent_total,
                "goaway": state.connection.goaway,
                "initial_recv_window": state.connection.initial_recv_window,
                "initial_send_window": state.connection.initial_send_window,
                "logical_clock_ms": state.logical_clock_ms,
                "max_concurrent_streams": state.connection.max_concurrent_streams,
                "max_window": state.connection.max_window,
                "recv_window": state.connection.recv_window,
                "recv_window_updates": state.connection.recv_window_updates,
                "reset_reclaimed": state.connection.reset_reclaimed,
                "send_window": state.connection.send_window,
                "send_window_updates": state.connection.send_window_updates,
                "sendable": state.connection.send_window,
                "settings": state.connection.settings,
                "unacked_send": state.connection.unacked_send,
            },
            "header_assembly": (
                state.header_assembly.to_dict()
                if state.header_assembly is not None
                else None
            ),
            "run": {
                "committed_seq": int(run["committed_seq"]),
                "event_count": int(run["event_count"]),
                "final_ledger_digest": run["final_ledger_digest"],
                "final_state_digest": run["final_state_digest"],
                "final_trajectory_digest": run["final_trajectory_digest"],
                "ledger_digest": snapshot["ledger_digest"],
                "run_id": run_id,
                "scenario_hash": run["scenario_hash"],
                "scenario_id": run["scenario_id"],
                "state_digest": snapshot["state_digest"],
                "status": run["status"],
                "trajectory_digest": snapshot["trajectory_digest"],
            },
            "streams": engine.status_streams(),
        }

