from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json, digest_json
from .engine import Engine
from .errors import (
    CONNECTION_EXISTS_001,
    CONNECTION_NOT_FOUND_001,
    DB_001,
    IDEMPOTENCY_001,
    PATCH_BASE_001,
    error,
)
from .locks import FileLock

SCHEMA_VERSION = "1"


class CrashInjector:
    """Process-level failure injection for recovery tests."""

    def __init__(self, point: str | None = None, seq: int | None = None) -> None:
        self.point = point if point is not None else os.environ.get("H2DIAG_CRASH_POINT")
        seq_env = os.environ.get("H2DIAG_CRASH_SEQ")
        self.seq = seq if seq is not None else (int(seq_env) if seq_env else None)

    def hit(self, stage: str, seq: int | None = None) -> None:
        if self.point != stage:
            return
        if self.seq is not None and seq != self.seq:
            return
        # Deliberately bypass exception handling and finally blocks.
        os._exit(86)


class Database:
    def __init__(self, path: str | Path, *, crash: CrashInjector | None = None) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.crash = crash or CrashInjector()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def mutation_lock(self) -> FileLock:
        return FileLock(self.lock_path)

    def ensure_schema(self) -> None:
        with self.mutation_lock():
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scenario_versions (
                    connection_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    parent_version INTEGER,
                    patch_id TEXT,
                    mode TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    scenario_json TEXT NOT NULL,
                    event_count INTEGER NOT NULL,
                    last_seq INTEGER NOT NULL,
                    created_order INTEGER NOT NULL,
                    PRIMARY KEY (connection_id, version)
                );
                CREATE TABLE IF NOT EXISTS connections (
                    connection_id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    epoch_version INTEGER NOT NULL,
                    current_version INTEGER NOT NULL,
                    committed_seq INTEGER NOT NULL,
                    clock_ms INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    state_digest TEXT NOT NULL,
                    state_order INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS state_commits (
                    commit_order INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    patch_version INTEGER NOT NULL,
                    frame_seq INTEGER NOT NULL,
                    clock_ms INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    state_digest TEXT NOT NULL,
                    event_digest TEXT,
                    UNIQUE (connection_id, kind, patch_version, frame_seq)
                );
                CREATE TABLE IF NOT EXISTS frames (
                    connection_id TEXT NOT NULL,
                    patch_version INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    stream_id INTEGER,
                    stream_seq INTEGER,
                    direction TEXT NOT NULL,
                    frame_type TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    state_digest TEXT NOT NULL,
                    commit_order INTEGER NOT NULL,
                    PRIMARY KEY (connection_id, patch_version, seq),
                    UNIQUE (connection_id, patch_version, stream_id, stream_seq),
                    UNIQUE (connection_id, patch_version, seq, event_digest)
                );
                CREATE TABLE IF NOT EXISTS patch_commits (
                    connection_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    parent_version INTEGER NOT NULL,
                    patch_id TEXT NOT NULL,
                    patch_digest TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    classified_kind TEXT NOT NULL,
                    applied_at_seq INTEGER NOT NULL,
                    applied_at_clock_ms INTEGER NOT NULL,
                    operations_json TEXT NOT NULL,
                    before_state_json TEXT NOT NULL,
                    before_state_digest TEXT NOT NULL,
                    after_state_json TEXT NOT NULL,
                    after_state_digest TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    commit_order INTEGER NOT NULL,
                    PRIMARY KEY (connection_id, version),
                    UNIQUE (connection_id, patch_id, patch_digest)
                );
                CREATE TABLE IF NOT EXISTS commit_intents (
                    intent_id TEXT PRIMARY KEY,
                    connection_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    patch_version INTEGER NOT NULL,
                    frame_seq INTEGER NOT NULL,
                    stream_id INTEGER,
                    stream_seq INTEGER,
                    event_digest TEXT,
                    phase TEXT NOT NULL,
                    created_order INTEGER NOT NULL
                );
                """
                )
                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            finally:
                connection.close()

    @contextlib.contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.rollback()
            connection.close()

    def _connection_row(
        self, connection: sqlite3.Connection, connection_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM connections WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()
        if row is None:
            raise error(
                CONNECTION_NOT_FOUND_001,
                "connection does not exist",
                details={"connection_id": connection_id},
                http_status=404,
                category="not_found",
            )
        return row

    def _scenario_row(
        self,
        connection: sqlite3.Connection,
        connection_id: str,
        version: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM scenario_versions WHERE connection_id = ? AND version = ?",
            (connection_id, version),
        ).fetchone()
        if row is None:
            raise error(
                CONNECTION_NOT_FOUND_001,
                "scenario version does not exist",
                details={"connection_id": connection_id, "version": version},
                http_status=404,
                category="not_found",
            )
        return row

    def initialize(
        self,
        scenario: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        self.ensure_schema()
        connection_id = scenario["connection_id"]
        scenario_json = canonical_json(scenario)
        scenario_digest = digest_json(scenario)
        with self.mutation_lock():
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM connections WHERE connection_id = ?",
                    (connection_id,),
                ).fetchone()
                if existing is not None:
                    current_scenario = self._scenario_row(
                        connection, connection_id, int(existing["current_version"])
                    )
                    if current_scenario["digest"] == scenario_digest:
                        connection.rollback()
                        return {
                            "connection_id": connection_id,
                            "created": False,
                            "duplicate": True,
                            "state": existing["state_json"],
                        }
                    raise error(
                        CONNECTION_EXISTS_001,
                        "connection already exists with different scenario content",
                        details={"connection_id": connection_id},
                        http_status=409,
                        category="version",
                    )
                commit_cursor = connection.execute(
                    """
                    INSERT INTO state_commits
                        (connection_id, kind, patch_version, frame_seq, clock_ms,
                         state_json, state_digest, event_digest)
                    VALUES (?, 'init', 1, 0, ?, ?, ?, NULL)
                    """,
                    (
                        connection_id,
                        state["clock_ms"],
                        canonical_json(state),
                        state["state_digest"],
                    ),
                )
                state_order = int(commit_cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO scenario_versions
                        (connection_id, version, parent_version, patch_id, mode,
                         digest, scenario_json, event_count, last_seq, created_order)
                    VALUES (?, 1, NULL, NULL, 'create', ?, ?, ?, ?, ?)
                    """,
                    (
                        connection_id,
                        scenario_digest,
                        scenario_json,
                        len(scenario["events"]),
                        len(scenario["events"]),
                        state_order,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO connections
                        (connection_id, scenario_id, epoch_version, current_version,
                         committed_seq, clock_ms, state_json, state_digest, state_order)
                    VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        connection_id,
                        scenario["scenario_id"],
                        state["committed_seq"],
                        state["clock_ms"],
                        canonical_json(state),
                        state["state_digest"],
                        state_order,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return {
            "connection_id": connection_id,
            "created": True,
            "duplicate": False,
        }

    def get_status_data(self, connection_id: str) -> dict[str, Any]:
        with self.read_snapshot() as connection:
            row = self._connection_row(connection, connection_id)
            scenario_row = self._scenario_row(
                connection, connection_id, int(row["current_version"])
            )
            return {
                "connection": dict(row),
                "scenario_version": dict(scenario_row),
            }

    def get_current(
        self, connection_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        data = self.get_status_data(connection_id)
        return dict(data["connection"]), dict(data["scenario_version"])

    def get_scenario_document(self, connection_id: str) -> dict[str, Any]:
        import json

        connection_row, scenario_row = self.get_current(connection_id)
        return json.loads(scenario_row["scenario_json"])

    def get_state(self, connection_id: str) -> dict[str, Any]:
        import json

        connection_row, _ = self.get_current(connection_id)
        return json.loads(connection_row["state_json"])

    def insert_intent(
        self,
        *,
        connection_id: str,
        operation: str,
        patch_version: int,
        frame_seq: int,
        stream_id: int | None,
        stream_seq: int | None,
        event_digest: str | None,
    ) -> str:
        intent_id = digest_json(
            {
                "connection_id": connection_id,
                "operation": operation,
                "patch_version": patch_version,
                "frame_seq": frame_seq,
                "stream_id": stream_id,
                "stream_seq": stream_seq,
                "event_digest": event_digest,
            }
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO commit_intents
                    (intent_id, connection_id, operation, patch_version, frame_seq,
                     stream_id, stream_seq, event_digest, phase, created_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'started',
                        (SELECT COALESCE(MAX(created_order), 0) + 1 FROM commit_intents))
                """,
                (
                    intent_id,
                    connection_id,
                    operation,
                    patch_version,
                    frame_seq,
                    stream_id,
                    stream_seq,
                    event_digest,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return intent_id

    def _mark_intent_in_transaction(
        self,
        connection: sqlite3.Connection,
        intent_id: str,
        phase: str,
    ) -> None:
        connection.execute(
            "UPDATE commit_intents SET phase = ? WHERE intent_id = ?",
            (phase, intent_id),
        )

    def mark_intent_failed(self, intent_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE commit_intents SET phase = 'failed' WHERE intent_id = ?",
                (intent_id,),
            )
        finally:
            connection.close()

    def commit_frame(
        self,
        *,
        connection_id: str,
        event: dict[str, Any],
        expected_patch_version: int | None = None,
    ) -> dict[str, Any]:
        event_digest = digest_json(event)
        stream_id = event.get("stream_id")
        intent_id: str | None = None
        with self.mutation_lock():
            current_row, scenario_row = self.get_current(connection_id)
            patch_version = int(current_row["current_version"])
            if expected_patch_version is not None and patch_version != expected_patch_version:
                raise error(
                    PATCH_BASE_001,
                    "frame was bound to a patch version that is no longer current",
                    details={
                        "expected": expected_patch_version,
                        "current": patch_version,
                    },
                    http_status=409,
                    category="version",
                )
            state_before = json.loads(current_row["state_json"])
            stream_seq = None
            if stream_id is not None:
                stream_seq = int(state_before["streams"][str(stream_id)]["stream_seq"]) + 1
            intent_id = self.insert_intent(
                connection_id=connection_id,
                operation="frame",
                patch_version=patch_version,
                frame_seq=int(event["seq"]),
                stream_id=int(stream_id) if stream_id is not None else None,
                stream_seq=stream_seq,
                event_digest=event_digest,
            )
            self.crash.hit("frame_intent", int(event["seq"]))
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._connection_row(connection, connection_id)
                if int(row["current_version"]) != patch_version:
                    raise error(
                        PATCH_BASE_001,
                        "patch version changed while frame was waiting for commit",
                        details={
                            "bound": patch_version,
                            "current": int(row["current_version"]),
                        },
                        http_status=409,
                        category="version",
                    )
                existing = connection.execute(
                    """
                    SELECT * FROM frames
                    WHERE connection_id = ? AND patch_version = ? AND seq = ?
                    """,
                    (connection_id, patch_version, int(event["seq"])),
                ).fetchone()
                if existing is not None:
                    if existing["event_digest"] != event_digest:
                        raise error(
                            IDEMPOTENCY_001,
                            "the same frame key was presented with different content",
                            details={
                                "seq": int(event["seq"]),
                                "stored_digest": existing["event_digest"],
                                "request_digest": event_digest,
                            },
                            http_status=409,
                            category="idempotency",
                        )
                    self._mark_intent_in_transaction(connection, intent_id, "completed")
                    connection.commit()
                    return {
                        "duplicate": True,
                        "state": json.loads(row["state_json"]),
                    }
                scenario = json.loads(scenario_row["scenario_json"])
                committed_seq = int(row["committed_seq"])
                if int(event["seq"]) <= committed_seq:
                    raise error(
                        IDEMPOTENCY_001,
                        "frame sequence is below the committed watermark without a matching record",
                        details={"seq": int(event["seq"]), "watermark": committed_seq},
                        http_status=409,
                        category="idempotency",
                    )
                if int(event["seq"]) != committed_seq + 1:
                    raise error(
                        IDEMPOTENCY_001,
                        "frame is out of order at the commit boundary",
                        details={
                            "seq": int(event["seq"]),
                            "expected": committed_seq + 1,
                        },
                        http_status=409,
                        category="idempotency",
                    )
                scenario_event = scenario["events"][committed_seq]
                if canonical_json(scenario_event) != canonical_json(event):
                    raise error(
                        IDEMPOTENCY_001,
                        "submitted frame does not match the active scenario event",
                        details={"seq": committed_seq + 1},
                        http_status=409,
                        category="idempotency",
                    )
                state_before = json.loads(row["state_json"])
                engine = Engine(scenario, state_before)
                state_after = engine.process(event)
                commit_cursor = connection.execute(
                    """
                    INSERT INTO state_commits
                        (connection_id, kind, patch_version, frame_seq, clock_ms,
                         state_json, state_digest, event_digest)
                    VALUES (?, 'frame', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        connection_id,
                        patch_version,
                        int(event["seq"]),
                        int(state_after["clock_ms"]),
                        canonical_json(state_after),
                        state_after["state_digest"],
                        event_digest,
                    ),
                )
                commit_order = int(commit_cursor.lastrowid)
                frame_stream_seq = state_after["last_frame"]["peer_stream_seq"]
                connection.execute(
                    """
                    INSERT INTO frames
                        (connection_id, patch_version, seq, stream_id, stream_seq,
                         direction, frame_type, event_digest, state_digest, commit_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        connection_id,
                        patch_version,
                        int(event["seq"]),
                        int(stream_id) if stream_id is not None else None,
                        frame_stream_seq,
                        event["direction"],
                        event["type"],
                        event_digest,
                        state_after["state_digest"],
                        commit_order,
                    ),
                )
                connection.execute(
                    """
                    UPDATE connections
                    SET committed_seq = ?, clock_ms = ?, state_json = ?,
                        state_digest = ?, state_order = ?
                    WHERE connection_id = ?
                    """,
                    (
                        int(state_after["committed_seq"]),
                        int(state_after["clock_ms"]),
                        canonical_json(state_after),
                        state_after["state_digest"],
                        commit_order,
                        connection_id,
                    ),
                )
                self._mark_intent_in_transaction(connection, intent_id, "completed")
                self.crash.hit("frame_mid_tx", int(event["seq"]))
                connection.commit()
            except Exception:
                connection.rollback()
                if intent_id:
                    self.mark_intent_failed(intent_id)
                raise
            finally:
                connection.close()
            self.crash.hit("frame_after_commit", int(event["seq"]))
            return {
                "duplicate": False,
                "state": state_after,
            }

    def commit_patch(
        self,
        *,
        connection_id: str,
        patch: dict[str, Any],
        scenario_after: dict[str, Any],
        state_after: dict[str, Any],
        manifest: dict[str, Any],
        classified_kind: str,
        expected_current_digest: str,
        expected_current_version: int,
        expected_committed_seq: int,
    ) -> dict[str, Any]:
        patch_digest = patch["_computed_digest"]
        intent_id: str | None = None
        with self.mutation_lock():
            intent_id = self.insert_intent(
                connection_id=connection_id,
                operation="patch",
                patch_version=int(patch["target_version"]),
                frame_seq=int(expected_committed_seq),
                stream_id=None,
                stream_seq=None,
                event_digest=patch_digest,
            )
            self.crash.hit("patch_intent", int(patch["target_version"]))
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._connection_row(connection, connection_id)
                if (
                    int(row["current_version"]) != expected_current_version
                    or int(row["committed_seq"]) != expected_committed_seq
                    or row["state_digest"] != expected_current_digest
                ):
                    raise error(
                        PATCH_BASE_001,
                        "connection changed while patch was being prepared",
                        details={
                            "expected_version": expected_current_version,
                            "current_version": int(row["current_version"]),
                            "expected_seq": expected_committed_seq,
                            "current_seq": int(row["committed_seq"]),
                            "expected_digest": expected_current_digest,
                            "current_digest": row["state_digest"],
                        },
                        http_status=409,
                        category="version",
                    )
                connection.execute("CREATE TEMP TABLE IF NOT EXISTS stage_events (seq INTEGER PRIMARY KEY, event_json TEXT NOT NULL)")
                connection.execute("CREATE TEMP TABLE IF NOT EXISTS stage_streams (stream_id INTEGER PRIMARY KEY, stream_json TEXT NOT NULL)")
                connection.execute("DELETE FROM stage_events")
                connection.execute("DELETE FROM stage_streams")
                connection.executemany(
                    "INSERT INTO stage_events(seq, event_json) VALUES(?, ?)",
                    [
                        (int(event["seq"]), canonical_json(event))
                        for event in scenario_after["events"]
                    ],
                )
                connection.executemany(
                    "INSERT INTO stage_streams(stream_id, stream_json) VALUES(?, ?)",
                    [
                        (int(stream["stream_id"]), canonical_json(stream))
                        for stream in scenario_after["streams"]
                    ],
                )
                staged = connection.execute(
                    "SELECT COUNT(*) AS count, COALESCE(MAX(seq), 0) AS last_seq FROM stage_events"
                ).fetchone()
                if int(staged["count"]) != len(scenario_after["events"]):
                    raise error(
                        DB_001,
                        "temporary event staging count mismatch",
                        http_status=500,
                        category="storage",
                    )
                self.crash.hit("patch_staged", int(patch["target_version"]))
                scenario_json = canonical_json(scenario_after)
                scenario_digest = digest_json(scenario_after)
                connection.execute(
                    """
                    INSERT INTO scenario_versions
                        (connection_id, version, parent_version, patch_id, mode,
                         digest, scenario_json, event_count, last_seq, created_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                            (SELECT COALESCE(MAX(commit_order), 0) + 1 FROM state_commits))
                    """,
                    (
                        connection_id,
                        int(patch["target_version"]),
                        int(row["current_version"]),
                        patch["patch_id"],
                        classified_kind,
                        scenario_digest,
                        scenario_json,
                        len(scenario_after["events"]),
                        int(staged["last_seq"]),
                    ),
                )
                self.crash.hit("patch_mid_tx", int(patch["target_version"]))
                commit_cursor = connection.execute(
                    """
                    INSERT INTO state_commits
                        (connection_id, kind, patch_version, frame_seq, clock_ms,
                         state_json, state_digest, event_digest)
                    VALUES (?, 'patch', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        connection_id,
                        int(patch["target_version"]),
                        int(state_after["committed_seq"]),
                        int(state_after["clock_ms"]),
                        canonical_json(state_after),
                        state_after["state_digest"],
                        patch_digest,
                    ),
                )
                commit_order = int(commit_cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO patch_commits
                        (connection_id, version, parent_version, patch_id, patch_digest,
                         mode, classified_kind, applied_at_seq, applied_at_clock_ms,
                         operations_json, before_state_json, before_state_digest,
                         after_state_json, after_state_digest, manifest_json, commit_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        connection_id,
                        int(patch["target_version"]),
                        int(row["current_version"]),
                        patch["patch_id"],
                        patch_digest,
                        patch["mode"],
                        classified_kind,
                        int(state_after["committed_seq"]),
                        int(state_after["clock_ms"]),
                        canonical_json(patch["operations"]),
                        row["state_json"],
                        row["state_digest"],
                        canonical_json(state_after),
                        state_after["state_digest"],
                        canonical_json(manifest),
                        commit_order,
                    ),
                )
                epoch_version = (
                    int(patch["target_version"])
                    if classified_kind == "replace"
                    else int(row["epoch_version"])
                )
                connection.execute(
                    """
                    UPDATE connections
                    SET current_version = ?, epoch_version = ?, state_json = ?,
                        state_digest = ?, state_order = ?, clock_ms = ?
                    WHERE connection_id = ?
                    """,
                    (
                        int(patch["target_version"]),
                        epoch_version,
                        canonical_json(state_after),
                        state_after["state_digest"],
                        commit_order,
                        int(state_after["clock_ms"]),
                        connection_id,
                    ),
                )
                self._mark_intent_in_transaction(connection, intent_id, "completed")
                connection.commit()
            except Exception:
                connection.rollback()
                if intent_id:
                    self.mark_intent_failed(intent_id)
                raise
            finally:
                connection.close()
            self.crash.hit("patch_after_commit", int(patch["target_version"]))
            return {
                "connection_id": connection_id,
                "version": int(patch["target_version"]),
                "kind": classified_kind,
                "state": state_after,
                "manifest": manifest,
            }

    def list_patch_commits(self, connection_id: str) -> list[dict[str, Any]]:
        with self.read_snapshot() as connection:
            rows = connection.execute(
                """
                SELECT * FROM patch_commits
                WHERE connection_id = ?
                ORDER BY version ASC
                """,
                (connection_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_state_commits(self, connection_id: str) -> list[dict[str, Any]]:
        with self.read_snapshot() as connection:
            rows = connection.execute(
                """
                SELECT * FROM state_commits
                WHERE connection_id = ?
                ORDER BY commit_order ASC
                """,
                (connection_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_frames(self, connection_id: str) -> list[dict[str, Any]]:
        with self.read_snapshot() as connection:
            rows = connection.execute(
                """
                SELECT * FROM frames
                WHERE connection_id = ?
                ORDER BY patch_version ASC, seq ASC
                """,
                (connection_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_scenario_versions(self, connection_id: str) -> list[dict[str, Any]]:
        with self.read_snapshot() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scenario_versions
                WHERE connection_id = ?
                ORDER BY version ASC
                """,
                (connection_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def verification_data(self, connection_id: str) -> dict[str, Any]:
        """Read every verification input from one WAL snapshot."""
        with self.read_snapshot() as connection:
            current = self._connection_row(connection, connection_id)
            scenario_versions = connection.execute(
                """
                SELECT * FROM scenario_versions
                WHERE connection_id = ?
                ORDER BY version ASC
                """,
                (connection_id,),
            ).fetchall()
            patch_commits = connection.execute(
                """
                SELECT * FROM patch_commits
                WHERE connection_id = ?
                ORDER BY version ASC
                """,
                (connection_id,),
            ).fetchall()
            state_commits = connection.execute(
                """
                SELECT * FROM state_commits
                WHERE connection_id = ?
                ORDER BY commit_order ASC
                """,
                (connection_id,),
            ).fetchall()
            frames = connection.execute(
                """
                SELECT * FROM frames
                WHERE connection_id = ?
                ORDER BY patch_version ASC, seq ASC
                """,
                (connection_id,),
            ).fetchall()
            return {
                "connection": dict(current),
                "scenario_versions": [dict(row) for row in scenario_versions],
                "patch_commits": [dict(row) for row in patch_commits],
                "state_commits": [dict(row) for row in state_commits],
                "frames": [dict(row) for row in frames],
            }

    def open_intents(self, connection_id: str) -> list[dict[str, Any]]:
        with self.read_snapshot() as connection:
            rows = connection.execute(
                """
                SELECT * FROM commit_intents
                WHERE connection_id = ? AND phase = 'started'
                ORDER BY created_order ASC
                """,
                (connection_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def abandon_intents(self, connection_id: str) -> int:
        with self.mutation_lock():
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE commit_intents
                    SET phase = 'abandoned'
                    WHERE connection_id = ? AND phase = 'started'
                    """,
                    (connection_id,),
                )
                connection.commit()
                return int(cursor.rowcount)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def health(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            temp_count = connection.execute(
                "SELECT COUNT(*) FROM sqlite_temp_master"
            ).fetchone()[0]
            return {
                "sqlite_version": sqlite3.sqlite_version,
                "journal_mode": journal,
                "temp_tables_supported": isinstance(temp_count, int),
                "path": str(self.path),
            }
        finally:
            connection.close()
