from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .errors import BizError, ErrorCode, fail
from .util import FileLock, canonical_json

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    watermark_ms INTEGER NOT NULL,
    tolerance_ms INTEGER NOT NULL,
    last_requested_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    revision TEXT NOT NULL,
    digest TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    params_json TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_versions (
    version INTEGER PRIMARY KEY,
    raw_json TEXT NOT NULL,
    layers_json TEXT NOT NULL,
    preprocess TEXT NOT NULL,
    postprocess TEXT NOT NULL,
    feature_table TEXT NOT NULL,
    upstream_model TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_bindings (
    version INTEGER PRIMARY KEY,
    binding_json TEXT NOT NULL,
    binding_digest TEXT NOT NULL,
    committed INTEGER NOT NULL CHECK (committed IN (0, 1)),
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS shards (
    version INTEGER NOT NULL,
    layer TEXT NOT NULL,
    shard_index INTEGER NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    digest TEXT NOT NULL,
    declared_path TEXT,
    PRIMARY KEY (version, layer, shard_index)
);

CREATE TABLE IF NOT EXISTS materializations (
    digest TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS replicas (
    replica_id TEXT PRIMARY KEY,
    loaded_version INTEGER NOT NULL,
    loaded_shards_json TEXT NOT NULL,
    baseline_state TEXT NOT NULL,
    baseline_ratio REAL NOT NULL,
    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1))
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    patch_json TEXT NOT NULL,
    baseline_version INTEGER NOT NULL,
    target_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    current_level INTEGER NOT NULL DEFAULT 0,
    current_ratio REAL NOT NULL DEFAULT 0.0,
    failure_at_ms INTEGER,
    rollback_deadline_ms INTEGER,
    rollback_reason TEXT,
    logical_watermark_ms INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS round_replicas (
    round_id TEXT NOT NULL,
    replica_id TEXT NOT NULL,
    state TEXT NOT NULL,
    serving_version INTEGER NOT NULL,
    stage_version INTEGER NOT NULL,
    gray_ratio REAL NOT NULL DEFAULT 0.0,
    warmup_started_at_ms INTEGER,
    warmup_expected_ms INTEGER,
    warmup_deadline_ms INTEGER,
    warmup_completed_at_ms INTEGER,
    failure_reason TEXT,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (round_id, replica_id)
);

CREATE TABLE IF NOT EXISTS round_steps (
    round_id TEXT NOT NULL,
    step_key TEXT NOT NULL,
    status TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (round_id, step_key)
);

CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    replica_id TEXT NOT NULL,
    round_id TEXT,
    version INTEGER NOT NULL,
    level INTEGER NOT NULL,
    binding_json TEXT NOT NULL,
    binding_digest TEXT NOT NULL,
    input_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    result_bytes_hex TEXT,
    result_digest TEXT,
    started_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT,
    replica_id TEXT NOT NULL,
    cache_type TEXT NOT NULL,
    version INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('invalidated', 'reused')),
    trigger TEXT NOT NULL,
    path_json TEXT NOT NULL,
    source_json TEXT NOT NULL DEFAULT '{}',
    at_ms INTEGER NOT NULL,
    UNIQUE (round_id, replica_id, cache_type, version, trigger, path_json)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT,
    at_ms INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL,
    old_digest TEXT NOT NULL,
    new_digest TEXT NOT NULL,
    round_id TEXT,
    at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT,
    at_ms INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shards_digest ON shards(digest);
CREATE INDEX IF NOT EXISTS idx_events_round_at ON events(round_id, at_ms, id);
CREATE INDEX IF NOT EXISTS idx_invalidations_round ON invalidations(round_id, replica_id, cache_type);
CREATE INDEX IF NOT EXISTS idx_requests_replica_status ON requests(replica_id, status);
CREATE INDEX IF NOT EXISTS idx_config_changes_resource ON config_changes(resource_id, at_ms);
CREATE INDEX IF NOT EXISTS idx_diagnostics_round ON diagnostics(round_id, id);
"""


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


class Store:
    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path).resolve()
        self.workspace = self.db_path.parent
        self.lock_path = self.workspace / "hotgray.lock"

    @property
    def exists(self) -> bool:
        return self.db_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            fail(
                ErrorCode.PERSISTENCE_ERROR,
                f"无法打开 SQLite 数据库: {exc}",
                path=str(self.db_path),
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        except sqlite3.Error as exc:
            connection.close()
            fail(
                ErrorCode.PERSISTENCE_ERROR,
                f"无法配置 SQLite: {exc}",
                path=str(self.db_path),
            )
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with FileLock(self.lock_path):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except BizError:
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise BizError(
                    ErrorCode.PERSISTENCE_ERROR,
                    f"SQLite 事务失败: {exc}",
                    {"path": str(self.db_path)},
                ) from exc
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.rollback()
        finally:
            connection.close()

    def initialize(self, *, meta: Mapping[str, Any], statements: Sequence[tuple[str, Sequence[Any]]]) -> None:
        """Create a new state database atomically.

        The caller must validate all input before invoking this method. SQLite
        commits the complete initial state before the database is exposed at
        its final path.
        """
        if self.db_path.exists():
            fail(
                ErrorCode.CONFLICT,
                f"持久化数据库已存在: {self.db_path}",
                path=str(self.db_path),
            )
        self.workspace.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            if self.db_path.exists():
                fail(
                    ErrorCode.CONFLICT,
                    f"持久化数据库已存在: {self.db_path}",
                    path=str(self.db_path),
                )
            temp_path = self.db_path.with_name(f".{self.db_path.name}.init-{os.getpid()}")
            if temp_path.exists():
                temp_path.unlink()
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(temp_path, timeout=30.0, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(SCHEMA_SQL)
                connection.execute("BEGIN IMMEDIATE")
                for key, value in meta.items():
                    connection.execute(
                        "INSERT INTO meta(key, value) VALUES (?, ?)",
                        (key, canonical_json(value) if not isinstance(value, str) else value),
                    )
                for sql, parameters in statements:
                    connection.execute(sql, parameters)
                connection.commit()
                connection.close()
                connection = None
                os.replace(temp_path, self.db_path)
                self._fsync_directory()
            except BizError:
                if connection is not None:
                    connection.rollback()
                    connection.close()
                self._cleanup_temp(temp_path)
                raise
            except sqlite3.Error as exc:
                if connection is not None:
                    connection.rollback()
                    connection.close()
                self._cleanup_temp(temp_path)
                raise BizError(
                    ErrorCode.PERSISTENCE_ERROR,
                    f"初始化 SQLite 失败: {exc}",
                    {"path": str(self.db_path)},
                ) from exc
            except BaseException:
                if connection is not None:
                    connection.rollback()
                    connection.close()
                self._cleanup_temp(temp_path)
                raise

    def _cleanup_temp(self, temp_path: Path) -> None:
        for path in (
            temp_path,
            temp_path.with_name(temp_path.name + "-wal"),
            temp_path.with_name(temp_path.name + "-shm"),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        directory_fd = os.open(str(self.workspace), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.read() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return row["value"]

    def get_meta_json(self, key: str, default: Any = None) -> Any:
        value = self.get_meta(key)
        if value is None:
            return default
        import json

        return json.loads(value)

    def set_meta(self, connection: sqlite3.Connection, key: str, value: Any) -> None:
        encoded = value if isinstance(value, str) else canonical_json(value)
        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, encoded),
        )

    def schema_version(self) -> str | None:
        if not self.exists:
            return None
        return self.get_meta("schema_version")

    def require_initialized(self) -> None:
        if not self.exists:
            fail(
                ErrorCode.NOT_FOUND,
                f"持久化数据库不存在，请先执行 init: {self.db_path}",
                path=str(self.db_path),
            )
        version = self.schema_version()
        if version != SCHEMA_VERSION:
            fail(
                ErrorCode.CONFLICT,
                f"数据库 schema 版本不兼容: {version!r}",
                path=str(self.db_path),
                expected=SCHEMA_VERSION,
                actual=version,
            )
