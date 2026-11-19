from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    E_CONFLICT,
    E_DB,
    E_DIGEST_MISMATCH,
    E_RUN_NOT_FOUND,
    E_TRACE_CORRUPT,
    E_TRACE_INCOMPLETE,
    EngineError,
)
from .scene import digest_json


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _chain_digest(previous: str, result_digest: str) -> str:
    payload = f"{previous}\0{result_digest}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class Store:
    """Transactional SQLite store with file-lock protected version allocation."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        parent = Path(self.db_path).resolve().parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EngineError(
                E_DB,
                f"cannot create database directory: {exc}",
                {"path": str(parent)},
            ) from exc
        self.lock_path = f"{self.db_path}.lock"
        try:
            with self._file_lock():
                self.connection = sqlite3.connect(
                    self.db_path,
                    timeout=30.0,
                    isolation_level=None,
                )
                self.connection.row_factory = sqlite3.Row
                self.connection.execute("PRAGMA busy_timeout=30000")
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=FULL")
                self.connection.execute("PRAGMA foreign_keys=ON")
                self._create_schema()
        except sqlite3.Error as exc:
            raise EngineError(
                E_DB,
                f"cannot open SQLite database: {exc}",
                {"path": self.db_path},
            ) from exc

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            scene_name TEXT NOT NULL,
            scene_digest TEXT NOT NULL,
            current_version INTEGER
        );

        CREATE TABLE IF NOT EXISTS versions (
            run_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            scene_digest TEXT NOT NULL,
            observations_json TEXT NOT NULL,
            status TEXT NOT NULL,
            total_events INTEGER NOT NULL,
            terminal_events INTEGER,
            error_code TEXT,
            trace_digest TEXT,
            PRIMARY KEY (run_id, version),
            FOREIGN KEY (run_id) REFERENCES runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS staging_tags (
            run_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            version INTEGER NOT NULL,
            PRIMARY KEY (run_id, tag),
            FOREIGN KEY (run_id, version) REFERENCES versions(run_id, version)
        );

        CREATE TABLE IF NOT EXISTS trace_events (
            run_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            event_index INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            event_digest TEXT NOT NULL,
            event_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            result_digest TEXT NOT NULL,
            PRIMARY KEY (run_id, version, event_index),
            FOREIGN KEY (run_id, version) REFERENCES versions(run_id, version)
        );

        CREATE TABLE IF NOT EXISTS version_heads (
            run_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            contiguous_count INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            trace_digest TEXT NOT NULL,
            PRIMARY KEY (run_id, version),
            FOREIGN KEY (run_id, version) REFERENCES versions(run_id, version)
        );

        CREATE INDEX IF NOT EXISTS idx_versions_status
            ON versions(run_id, status, version);
        CREATE INDEX IF NOT EXISTS idx_trace_events_order
            ON trace_events(run_id, version, event_index);
        """
        self.connection.executescript(schema)
        self.connection.execute("PRAGMA user_version=1")

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        try:
            handle = open(self.lock_path, "a+b")
        except OSError as exc:
            raise EngineError(
                E_DB,
                f"cannot open database lock file: {exc}",
                {"path": self.lock_path},
            ) from exc
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.execute("COMMIT")
        except Exception:
            try:
                self.connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _run_row(self, run_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise EngineError(
                E_RUN_NOT_FOUND,
                f"run_id {run_id!r} does not exist",
                {"run_id": run_id},
            )
        return row

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self._run_row(run_id)
        return dict(row)

    def ensure_run(self, run_id: str, scene_name: str, scene_digest: str) -> None:
        with self._file_lock():
            with self._transaction():
                row = self.connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    self.connection.execute(
                        """
                        INSERT INTO runs(run_id, scene_name, scene_digest, current_version)
                        VALUES (?, ?, ?, NULL)
                        """,
                        (run_id, scene_name, scene_digest),
                    )
                    return
                if row["scene_digest"] != scene_digest:
                    raise EngineError(
                        E_DIGEST_MISMATCH,
                        "source scene digest does not match the persisted run",
                        {
                            "run_id": run_id,
                            "persisted_digest": row["scene_digest"],
                            "current_digest": scene_digest,
                        },
                    )

    def check_digest(self, run_id: str, scene_digest: str) -> dict[str, Any]:
        row = self.get_run(run_id)
        if row["scene_digest"] != scene_digest:
            raise EngineError(
                E_DIGEST_MISMATCH,
                "source scene digest does not match the persisted run",
                {
                    "run_id": run_id,
                    "persisted_digest": row["scene_digest"],
                    "current_digest": scene_digest,
                },
            )
        return row

    def allocate_version(
        self,
        *,
        run_id: str,
        scene_name: str,
        scene_digest: str,
        total_events: int,
        initial_state: dict[str, Any],
        observations: dict[str, Any],
        tag: str | None = None,
    ) -> dict[str, Any]:
        with self._file_lock():
            with self._transaction():
                run = self.connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    self.connection.execute(
                        """
                        INSERT INTO runs(run_id, scene_name, scene_digest, current_version)
                        VALUES (?, ?, ?, NULL)
                        """,
                        (run_id, scene_name, scene_digest),
                    )
                elif run["scene_digest"] != scene_digest:
                    raise EngineError(
                        E_DIGEST_MISMATCH,
                        "source scene digest does not match the persisted run",
                        {
                            "run_id": run_id,
                            "persisted_digest": run["scene_digest"],
                            "current_digest": scene_digest,
                        },
                    )

                if tag is not None:
                    tagged = self.connection.execute(
                        """
                        SELECT v.* FROM staging_tags t
                        JOIN versions v
                          ON v.run_id = t.run_id AND v.version = t.version
                        WHERE t.run_id = ? AND t.tag = ?
                        """,
                        (run_id, tag),
                    ).fetchone()
                    if tagged is not None:
                        if (
                            tagged["scene_digest"] != scene_digest
                            or tagged["total_events"] != total_events
                        ):
                            raise EngineError(
                                E_CONFLICT,
                                "staging tag was allocated for a different scene shape",
                                {
                                    "run_id": run_id,
                                    "tag": tag,
                                    "version": tagged["version"],
                                },
                            )
                        return {
                            "version": tagged["version"],
                            "created": False,
                            "status": tagged["status"],
                        }

                row = self.connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS max_version "
                    "FROM versions WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                version = int(row["max_version"]) + 1
                self.connection.execute(
                    """
                    INSERT INTO versions(
                        run_id, version, scene_digest, observations_json,
                        status, total_events, terminal_events, error_code,
                        trace_digest
                    ) VALUES (?, ?, ?, ?, 'STAGING', ?, NULL, NULL, NULL)
                    """,
                    (
                        run_id,
                        version,
                        scene_digest,
                        _canonical(observations),
                        total_events,
                    ),
                )
                initial_trace_digest = hashlib.sha256(
                    f"trace:{scene_digest}".encode("utf-8")
                ).hexdigest()
                self.connection.execute(
                    """
                    INSERT INTO version_heads(
                        run_id, version, contiguous_count, state_json, trace_digest
                    ) VALUES (?, ?, 0, ?, ?)
                    """,
                    (run_id, version, _canonical(initial_state), initial_trace_digest),
                )
                if tag is not None:
                    self.connection.execute(
                        """
                        INSERT INTO staging_tags(run_id, tag, version)
                        VALUES (?, ?, ?)
                        """,
                        (run_id, tag, version),
                    )
                return {
                    "version": version,
                    "created": True,
                    "status": "STAGING",
                }

    def get_version(self, run_id: str, version: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM versions WHERE run_id = ? AND version = ?",
            (run_id, version),
        ).fetchone()
        if row is None:
            raise EngineError(
                E_RUN_NOT_FOUND,
                "trajectory version does not exist",
                {"run_id": run_id, "version": version},
            )
        return dict(row)

    def get_head(self, run_id: str, version: int) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT * FROM version_heads
            WHERE run_id = ? AND version = ?
            """,
            (run_id, version),
        ).fetchone()
        if row is None:
            raise EngineError(
                E_TRACE_CORRUPT,
                "version head is missing",
                {"run_id": run_id, "version": version},
            )
        return {
            "contiguous_count": int(row["contiguous_count"]),
            "state": json.loads(row["state_json"]),
            "trace_digest": row["trace_digest"],
        }

    def get_event_result(
        self, run_id: str, version: int, event_index: int
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT result_json FROM trace_events
            WHERE run_id = ? AND version = ? AND event_index = ?
            """,
            (run_id, version, event_index),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["result_json"])

    def latest_incomplete(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM versions
            WHERE run_id = ? AND status IN ('STAGING', 'RUNNING')
            ORDER BY version DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def _refresh_head(
        self,
        run_id: str,
        version: int,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        head = self.get_head(run_id, version)
        count = head["contiguous_count"]
        state = head["state"]
        trace_digest = head["trace_digest"]
        last_result: dict[str, Any] | None = None
        while True:
            row = self.connection.execute(
                """
                SELECT result_json, result_digest
                FROM trace_events
                WHERE run_id = ? AND version = ? AND event_index = ?
                """,
                (run_id, version, count),
            ).fetchone()
            if row is None:
                break
            result = json.loads(row["result_json"])
            if "state" not in result:
                raise EngineError(
                    E_TRACE_CORRUPT,
                    "committed event is missing its state snapshot",
                    {"run_id": run_id, "version": version, "event_index": count},
                )
            state = result["state"]
            trace_digest = _chain_digest(trace_digest, row["result_digest"])
            last_result = result
            count += 1
        self.connection.execute(
            """
            UPDATE version_heads
            SET contiguous_count = ?, state_json = ?, trace_digest = ?
            WHERE run_id = ? AND version = ?
            """,
            (count, _canonical(state), trace_digest, run_id, version),
        )
        return {
            "contiguous_count": count,
            "state": state,
            "trace_digest": trace_digest,
        }, last_result

    def commit_event(
        self,
        *,
        run_id: str,
        version: int,
        event_index: int,
        event_id: str,
        event_digest: str,
        event: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        result_digest = digest_json(result)
        event_json = _canonical(event)
        result_json = _canonical(result)
        with self._file_lock():
            with self._transaction():
                meta = self.connection.execute(
                    """
                    SELECT * FROM versions
                    WHERE run_id = ? AND version = ?
                    """,
                    (run_id, version),
                ).fetchone()
                if meta is None:
                    raise EngineError(
                        E_RUN_NOT_FOUND,
                        "trajectory version does not exist",
                        {"run_id": run_id, "version": version},
                    )
                if not (0 <= event_index < meta["total_events"]):
                    raise EngineError(
                        E_CONFLICT,
                        "event index is outside the allocated version",
                        {
                            "run_id": run_id,
                            "version": version,
                            "event_index": event_index,
                            "total_events": meta["total_events"],
                        },
                    )
                existing = self.connection.execute(
                    """
                    SELECT * FROM trace_events
                    WHERE run_id = ? AND version = ? AND event_index = ?
                    """,
                    (run_id, version, event_index),
                ).fetchone()
                if existing is None and meta["status"] in {"COMPLETE", "FAILED"}:
                    raise EngineError(
                        E_CONFLICT,
                        "cannot append an event to a finished version",
                        {
                            "run_id": run_id,
                            "version": version,
                            "status": meta["status"],
                            "event_index": event_index,
                        },
                    )
                if existing is not None:
                    same = (
                        existing["event_id"] == event_id
                        and existing["event_digest"] == event_digest
                        and existing["event_json"] == event_json
                        and existing["result_digest"] == result_digest
                        and existing["result_json"] == result_json
                    )
                    if not same:
                        raise EngineError(
                            E_CONFLICT,
                            "same event/version was already committed with different content",
                            {
                                "run_id": run_id,
                                "version": version,
                                "event_index": event_index,
                                "event_id": event_id,
                            },
                        )
                    idempotent = True
                else:
                    self.connection.execute(
                        """
                        INSERT INTO trace_events(
                            run_id, version, event_index, event_id, event_digest,
                            event_json, result_json, result_digest
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            version,
                            event_index,
                            event_id,
                            event_digest,
                            event_json,
                            result_json,
                            result_digest,
                        ),
                    )
                    idempotent = False

                head, final_result = self._refresh_head(run_id, version)
                finalized = False
                final_status = meta["status"]
                fatal = (
                    final_result.get("fatal_error")
                    if final_result is not None
                    else None
                )
                should_finish = False
                terminal_events: int | None = None
                if meta["status"] in {"STAGING", "RUNNING"}:
                    if fatal is not None:
                        should_finish = True
                        terminal_events = head["contiguous_count"]
                    elif head["contiguous_count"] == meta["total_events"]:
                        should_finish = True
                        terminal_events = meta["total_events"]
                if should_finish:
                    if fatal is None:
                        final_status = "COMPLETE"
                        error_code = None
                    else:
                        final_status = "FAILED"
                        error_code = fatal.get("code")
                    self.connection.execute(
                        """
                        UPDATE versions
                        SET status = ?, error_code = ?, trace_digest = ?,
                            terminal_events = ?
                        WHERE run_id = ? AND version = ?
                        """,
                        (
                            final_status,
                            error_code,
                            head["trace_digest"],
                            terminal_events,
                            run_id,
                            version,
                        ),
                    )
                    self.connection.execute(
                        """
                        UPDATE runs
                        SET current_version = ?
                        WHERE run_id = ?
                          AND (current_version IS NULL OR current_version < ?)
                        """,
                        (version, run_id, version),
                    )
                    finalized = True
                return {
                    "idempotent": idempotent,
                    "finalized": finalized,
                    "status": final_status,
                    "contiguous_count": head["contiguous_count"],
                    "trace_digest": head["trace_digest"],
                }

    def get_active_trace(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        version = run["current_version"]
        if version is None:
            raise EngineError(
                E_TRACE_INCOMPLETE,
                "no complete trajectory has been committed for this run",
                {"run_id": run_id},
            )
        meta = self.get_version(run_id, version)
        if meta["status"] not in {"COMPLETE", "FAILED"}:
            raise EngineError(
                E_TRACE_INCOMPLETE,
                "current trajectory pointer does not reference a finished version",
                {
                    "run_id": run_id,
                    "version": version,
                    "status": meta["status"],
                },
            )
        expected_events = (
            meta["terminal_events"]
            if meta["status"] == "FAILED"
            else meta["total_events"]
        )
        if expected_events is None:
            raise EngineError(
                E_TRACE_CORRUPT,
                "failed trajectory is missing its terminal event count",
                {"run_id": run_id, "version": version},
            )
        rows = self.connection.execute(
            """
            SELECT * FROM trace_events
            WHERE run_id = ? AND version = ?
            ORDER BY event_index ASC
            """,
            (run_id, version),
        ).fetchall()
        if len(rows) != expected_events:
            raise EngineError(
                E_TRACE_INCOMPLETE,
                "active trajectory does not contain every event",
                {
                    "run_id": run_id,
                    "version": version,
                    "committed": len(rows),
                    "expected": expected_events,
                },
            )
        expected_index = 0
        reconstructed_digest = hashlib.sha256(
            f"trace:{meta['scene_digest']}".encode("utf-8")
        ).hexdigest()
        events: list[dict[str, Any]] = []
        for row in rows:
            if row["event_index"] != expected_index:
                raise EngineError(
                    E_TRACE_CORRUPT,
                    "active trajectory has a hole or duplicate event index",
                    {
                        "expected_event_index": expected_index,
                        "actual_event_index": row["event_index"],
                    },
                )
            result = json.loads(row["result_json"])
            if digest_json(result) != row["result_digest"]:
                raise EngineError(
                    E_TRACE_CORRUPT,
                    "stored event result digest does not match its content",
                    {
                        "run_id": run_id,
                        "version": version,
                        "event_index": row["event_index"],
                    },
                )
            reconstructed_digest = _chain_digest(
                reconstructed_digest, row["result_digest"]
            )
            events.append(
                {
                    "event": json.loads(row["event_json"]),
                    "event_digest": row["event_digest"],
                    "result": result,
                    "result_digest": row["result_digest"],
                }
            )
            expected_index += 1
        if reconstructed_digest != meta["trace_digest"]:
            raise EngineError(
                E_TRACE_CORRUPT,
                "active trajectory chain digest does not match the version record",
                {
                    "run_id": run_id,
                    "version": version,
                    "expected": meta["trace_digest"],
                    "recomputed": reconstructed_digest,
                },
            )
        return {
            "run": run,
            "version": meta,
            "events": events,
            "trace_digest": reconstructed_digest,
        }

    def finalize_if_complete(self, run_id: str, version: int) -> dict[str, Any]:
        with self._file_lock():
            with self._transaction():
                meta = self.connection.execute(
                    """
                    SELECT * FROM versions
                    WHERE run_id = ? AND version = ?
                    """,
                    (run_id, version),
                ).fetchone()
                if meta is None:
                    raise EngineError(
                        E_RUN_NOT_FOUND,
                        "trajectory version does not exist",
                        {"run_id": run_id, "version": version},
                    )
                head = self.get_head(run_id, version)
                if (
                    meta["status"] in {"STAGING", "RUNNING"}
                    and head["contiguous_count"] == meta["total_events"]
                ):
                    self.connection.execute(
                        """
                        UPDATE versions
                        SET status = 'COMPLETE', error_code = NULL,
                            terminal_events = ?, trace_digest = ?
                        WHERE run_id = ? AND version = ?
                        """,
                        (
                            meta["total_events"],
                            head["trace_digest"],
                            run_id,
                            version,
                        ),
                    )
                    self.connection.execute(
                        """
                        UPDATE runs SET current_version = ?
                        WHERE run_id = ?
                          AND (current_version IS NULL OR current_version < ?)
                        """,
                        (version, run_id, version),
                    )
                    return {
                        "status": "COMPLETE",
                        "contiguous_count": head["contiguous_count"],
                        "trace_digest": head["trace_digest"],
                    }
                return {
                    "status": meta["status"],
                    "contiguous_count": head["contiguous_count"],
                    "trace_digest": head["trace_digest"],
                }

    def status(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        active_version = run["current_version"]
        active = None
        if active_version is not None:
            meta = self.get_version(run_id, active_version)
            head = self.get_head(run_id, active_version)
            active = {
                "version": active_version,
                "status": meta["status"],
                "error_code": meta["error_code"],
                "total_events": meta["total_events"],
                "terminal_events": meta["terminal_events"],
                "trace_digest": meta["trace_digest"],
                "state": head["state"],
            }
        latest = self.connection.execute(
            """
            SELECT * FROM versions
            WHERE run_id = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        latest_info = None
        if latest is not None:
            head = self.get_head(run_id, latest["version"])
            latest_info = {
                "version": latest["version"],
                "status": latest["status"],
                "error_code": latest["error_code"],
                "committed_events": head["contiguous_count"],
                "total_events": latest["total_events"],
                "terminal_events": latest["terminal_events"],
                "trace_digest": latest["trace_digest"],
                "state": head["state"],
            }
        return {
            "run_id": run_id,
            "scene_name": run["scene_name"],
            "source_digest": run["scene_digest"],
            "active": active,
            "latest": latest_info,
        }

