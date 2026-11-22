"""SQLite schema, connection management and cross-process locking."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import DomainError, ErrorCode
from .manifest import StoreConfig, normalize_checksum

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buckets (
    name TEXT PRIMARY KEY,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS objects (
    bucket TEXT NOT NULL,
    object_key TEXT NOT NULL,
    digest_algorithm TEXT NOT NULL,
    expected_digest TEXT NOT NULL,
    completion_claim_session_id TEXT,
    completed_session_id TEXT,
    completed_version INTEGER,
    final_digest TEXT,
    final_path TEXT,
    final_size INTEGER,
    completed_at_ms INTEGER,
    PRIMARY KEY (bucket, object_key)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    bucket TEXT NOT NULL,
    object_key TEXT NOT NULL,
    part_size_limit INTEGER NOT NULL,
    max_concurrency INTEGER NOT NULL,
    declared_parts_json TEXT NOT NULL,
    phase TEXT NOT NULL,
    visible_version INTEGER NOT NULL,
    visible_set_digest TEXT NOT NULL,
    clock_watermark_ms INTEGER NOT NULL,
    completion_bound_version INTEGER,
    decision TEXT,
    decision_version INTEGER,
    decision_at_ms INTEGER,
    aborted_trigger TEXT,
    aborted_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS part_versions (
    session_id TEXT NOT NULL,
    part_number INTEGER NOT NULL,
    version INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    checksum TEXT NOT NULL,
    checksum_algorithm TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    logical_time_ms INTEGER NOT NULL,
    declared_overwrite INTEGER NOT NULL,
    actual_overwrite INTEGER NOT NULL,
    previous_version INTEGER,
    previous_digest TEXT,
    reused_existing_file INTEGER NOT NULL,
    acknowledged_by_digest INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (session_id, part_number, version)
);

CREATE TABLE IF NOT EXISTS visible_parts (
    session_id TEXT NOT NULL,
    part_number INTEGER NOT NULL,
    version INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    checksum TEXT NOT NULL,
    checksum_algorithm TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    PRIMARY KEY (session_id, part_number)
);

CREATE TABLE IF NOT EXISTS overwrite_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    part_number INTEGER NOT NULL,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    previous_digest TEXT NOT NULL,
    new_digest TEXT NOT NULL,
    source_event_id INTEGER NOT NULL,
    same_content INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    part_number INTEGER NOT NULL,
    content_base64 TEXT NOT NULL,
    checksum TEXT NOT NULL,
    logical_time_ms INTEGER NOT NULL,
    declared_overwrite INTEGER NOT NULL,
    status TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason TEXT,
    round_no INTEGER,
    visible_version_before INTEGER,
    visible_version_after INTEGER,
    result_json TEXT,
    started INTEGER NOT NULL DEFAULT 0,
    started_at_ms INTEGER,
    finished_at_ms INTEGER,
    is_direct INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS late_uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    session_id TEXT NOT NULL,
    part_number INTEGER NOT NULL,
    logical_time_ms INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    reason TEXT NOT NULL,
    decision_phase TEXT NOT NULL,
    decision_version INTEGER NOT NULL,
    content_digest TEXT,
    size_bytes INTEGER
);

CREATE TABLE IF NOT EXISTS completion_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    bucket TEXT NOT NULL,
    object_key TEXT NOT NULL,
    bound_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    expected_digest TEXT NOT NULL,
    actual_digest TEXT,
    first_bad_part INTEGER,
    failure_code TEXT,
    failure_json TEXT,
    final_path TEXT,
    final_size INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS aborts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    trigger_version INTEGER,
    reason TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS gc_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    reason TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    digest TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, event_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, event_id);
CREATE INDEX IF NOT EXISTS idx_visible_session ON visible_parts(session_id, part_number);
CREATE INDEX IF NOT EXISTS idx_attempt_object
    ON completion_attempts(bucket, object_key, status);
CREATE INDEX IF NOT EXISTS idx_late_session ON late_uploads(session_id, id);
CREATE INDEX IF NOT EXISTS idx_part_versions_path ON part_versions(storage_path);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_attempt_object
    ON completion_attempts(bucket, object_key)
    WHERE status IN ('prepared', 'validated', 'materialized');
"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def visible_set_digest(version: int, parts: list[sqlite3.Row] | list[dict[str, Any]]) -> str:
    payload = {
        "version": version,
        "parts": [
            {
                "part_number": int(row["part_number"]),
                "version": int(row["version"]),
                "content_digest": str(row["content_digest"]),
                "size_bytes": int(row["size_bytes"]),
            }
            for row in sorted(parts, key=lambda item: int(item["part_number"]))
        ],
    }
    return sha256_text(canonical_json(payload))


class CrossProcessFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._handle: Any = None

    def __enter__(self) -> "CrossProcessFileLock":
        self._thread_lock.acquire()
        if self._depth == 0:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                self._handle.write(b"\0")
                self._handle.flush()
                os.fsync(self._handle.fileno())
            self._acquire_os_lock(self._handle)
        self._depth += 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._depth -= 1
        if self._depth == 0 and self._handle is not None:
            try:
                self._release_os_lock(self._handle)
            finally:
                self._handle.close()
                self._handle = None
        self._thread_lock.release()

    @staticmethod
    def _acquire_os_lock(handle: Any) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _release_os_lock(handle: Any) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StoreDatabase:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.db_path = self.data_dir / "state.sqlite3"
        self.lock_path = self.data_dir / ".store.lock"
        self.file_lock = CrossProcessFileLock(self.lock_path)
        self._ensure_directories()
        with self.file_lock:
            conn = self.connect()
            try:
                conn.executescript(SCHEMA_SQL)
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('clock_watermark_ms', '0')"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('round_no', '0')"
                )
                session_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(sessions)")
                }
                if "decision_version" not in session_columns:
                    conn.execute(
                        "ALTER TABLE sessions ADD COLUMN decision_version INTEGER"
                    )
            finally:
                conn.close()

    def _ensure_directories(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            probe = self.data_dir / f".write-probe-{os.getpid()}-{uuid.uuid4().hex}"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise DomainError(
                ErrorCode.STORAGE_DIR_INVALID,
                "data directory is not writable",
                {"path": str(self.data_dir), "cause": str(exc)},
            ) from exc

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self.file_lock:
            yield

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_config(self) -> StoreConfig | None:
        raw = self.get_meta("config_json")
        if raw is None:
            return None
        from .manifest import parse_checksum_policy

        data = json.loads(raw)
        return StoreConfig(
            round_time_budget_ms=int(data["round_time_budget_ms"]),
            clock_step_ms=int(data["clock_step_ms"]),
            clock_tolerance_ms=int(data["clock_tolerance_ms"]),
            part_temp_dir=str(data["part_temp_dir"]),
            checksum_policy=parse_checksum_policy(data["checksum_policy"]),
        )

    def require_config(self) -> StoreConfig:
        config = self.get_config()
        if config is None:
            raise DomainError(
                ErrorCode.STORE_NOT_LOADED,
                "store has no manifest loaded",
                {"data_dir": str(self.data_dir)},
            )
        return config

    def current_clock(self, conn: sqlite3.Connection | None = None) -> int:
        if conn is None:
            with self.connection() as local:
                row = local.execute(
                    "SELECT value FROM meta WHERE key = 'clock_watermark_ms'"
                ).fetchone()
                return int(row["value"]) if row else 0
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'clock_watermark_ms'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def bump_clock(self, conn: sqlite3.Connection, candidate: int) -> int:
        current = self.current_clock(conn)
        value = max(current, candidate)
        self.set_meta(conn, "clock_watermark_ms", str(value))
        return value

    def parts_root(self, config: StoreConfig) -> Path:
        root = Path(config.part_temp_dir).expanduser()
        if not root.is_absolute():
            root = self.data_dir / root
        return root.resolve()

    def staging_root(self) -> Path:
        return (self.data_dir / "staging").resolve()

    def objects_root(self) -> Path:
        return (self.data_dir / "objects").resolve()

    def storage_reference(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.data_dir).as_posix()
        except ValueError:
            return str(resolved)

    def resolve_reference(self, reference: str) -> Path:
        path = Path(reference)
        if path.is_absolute():
            return path
        return (self.data_dir / path).resolve()


def checksum_valid(
    checksum: str,
    algorithm: str,
    actual_digest: str,
    config: StoreConfig,
) -> bool:
    try:
        normalized = normalize_checksum(
            checksum, algorithm, config.checksum_policy, "stored_checksum"
        )
    except DomainError:
        return False
    return normalized == actual_digest.lower()
