from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import AppError, ErrorCode
from .models import CheckpointVersion, MergeResult, SnapshotRow, WalRow
from .util import FileLock, canonical_json
from .wal import row_from_db

SCHEMA_VERSION = 1

ALL_TABLES = [
    "_meta",
    "_wal_rows",
    "_checkpoint_versions",
    "_checkpoint_snapshot",
    "_checkpoint_shards",
    "_checkpoint_pointer",
    "_rows",
    "_row_history",
    "_txn",
    "_delays",
    "_invalidations",
    "_edits",
    "_checkpoint_intervals",
    "_operations",
    "_recovery_events",
]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS _wal_rows (
    txn_id INTEGER PRIMARY KEY,
    segment TEXT NOT NULL,
    segment_no INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    op TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    checkpoint_gen INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    commit_ms INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    updated_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS _checkpoint_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation INTEGER NOT NULL,
    source_format TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    saved_at_ms INTEGER NOT NULL,
    replay_generation INTEGER NOT NULL,
    applied_watermark INTEGER NOT NULL,
    manifest_version INTEGER NOT NULL,
    base_generation INTEGER NOT NULL,
    base_manifest_hash TEXT,
    manifest_hash TEXT,
    selected INTEGER NOT NULL,
    invalidated INTEGER NOT NULL DEFAULT 0,
    decision_reason_json TEXT NOT NULL,
    conversion_json TEXT,
    manifest_json TEXT NOT NULL,
    UNIQUE(generation, source_format, source_hash)
);

CREATE TABLE IF NOT EXISTS _checkpoint_snapshot (
    version_id INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    txn_id INTEGER NOT NULL,
    deleted INTEGER NOT NULL,
    value_json TEXT,
    PRIMARY KEY (version_id, table_name, row_key),
    FOREIGN KEY (version_id) REFERENCES _checkpoint_versions(version_id)
);

CREATE TABLE IF NOT EXISTS _checkpoint_shards (
    version_id INTEGER NOT NULL,
    shard_name TEXT NOT NULL,
    shard_hash TEXT NOT NULL,
    table_name TEXT,
    row_count INTEGER,
    content_digest TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (version_id, shard_name),
    FOREIGN KEY (version_id) REFERENCES _checkpoint_versions(version_id)
);

CREATE TABLE IF NOT EXISTS _checkpoint_pointer (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL,
    version_id INTEGER NOT NULL,
    manifest_version INTEGER NOT NULL,
    replay_generation INTEGER NOT NULL,
    applied_watermark INTEGER NOT NULL,
    saved_at_ms INTEGER NOT NULL,
    manifest_hash TEXT,
    updated_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS _rows (
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    txn_id INTEGER NOT NULL,
    deleted INTEGER NOT NULL,
    value_json TEXT,
    PRIMARY KEY (table_name, row_key)
);

CREATE TABLE IF NOT EXISTS _row_history (
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    txn_id INTEGER NOT NULL,
    deleted INTEGER NOT NULL,
    value_json TEXT,
    commit_ms INTEGER NOT NULL,
    PRIMARY KEY (table_name, row_key, version)
);

CREATE TABLE IF NOT EXISTS _txn (
    txn_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    commit_ms INTEGER NOT NULL,
    checkpoint_gen INTEGER NOT NULL,
    segment TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    replay_ms INTEGER,
    latency_ms INTEGER,
    precision_ms INTEGER,
    updated_clock_ms INTEGER NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS _delays (
    txn_id INTEGER PRIMARY KEY,
    commit_ms INTEGER NOT NULL,
    replay_ms INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    precision_ms INTEGER NOT NULL,
    rounding TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    source_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS _invalidations (
    invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    edit_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    trigger_txn INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS _edits (
    edit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    fields_changed_json TEXT NOT NULL,
    before_row_json TEXT NOT NULL,
    after_row_json TEXT,
    clock_ms INTEGER NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS _checkpoint_intervals (
    new_generation INTEGER PRIMARY KEY,
    previous_generation INTEGER NOT NULL,
    previous_saved_at_ms INTEGER,
    new_saved_at_ms INTEGER NOT NULL,
    interval_ms INTEGER,
    precision_ms INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS _operations (
    op_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT
);

CREATE TABLE IF NOT EXISTS _recovery_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    clock_ms INTEGER NOT NULL,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: Path, lock_timeout_seconds: float = 30.0) -> None:
        self.db_path = Path(db_path)
        self.lock = FileLock(
            Path(str(self.db_path) + ".lock"), timeout_seconds=lock_timeout_seconds
        )
        self.connection: sqlite3.Connection | None = None

    def connect(self, create: bool = True) -> None:
        if self.connection is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not create and not self.db_path.exists():
            raise AppError(
                ErrorCode.STATE_NOT_INITIALIZED,
                "state database does not exist",
                {"db": str(self.db_path)},
            )
        self.connection = sqlite3.connect(
            str(self.db_path), timeout=30.0, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        if create:
            self.connection.executescript(SCHEMA_SQL)

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("store is not connected")
        return self.connection

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        if immediate:
            self.db.execute("BEGIN IMMEDIATE")
        else:
            self.db.execute("BEGIN")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def init_metadata(self, now_ms: int) -> None:
        defaults = {
            "schema_version": str(SCHEMA_VERSION),
            "initialized": "1",
            "last_clock_ms": str(now_ms),
            "applied_watermark": "0",
            "current_checkpoint_gen": "0",
            "current_checkpoint_version_id": "0",
            "current_manifest_version": "0",
            "current_replay_generation": "0",
            "needs_rebuild": "0",
            "rebuild_from_txn": "0",
            "op_counter": "0",
            "last_replay_clock_ms": "0",
            "last_checkpoint_interval_ms": "",
        }
        self.db.executemany(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES (?, ?)",
            sorted(defaults.items()),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute(
            "SELECT value FROM _meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else default

    def get_meta_int(self, key: str, default: int = 0) -> int:
        value = self.get_meta(key)
        return default if value in (None, "") else int(value)

    def set_meta(self, key: str, value: Any) -> None:
        if isinstance(value, bool):
            text = "1" if value else "0"
        elif value is None:
            text = ""
        else:
            text = str(value)
        self.db.execute(
            "INSERT INTO _meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, text),
        )

    def check_clock(
        self, now_ms: int, tolerance_ms: int, *, update: bool = True
    ) -> None:
        last = self.get_meta_int("last_clock_ms")
        if now_ms < last - tolerance_ms:
            raise AppError(
                ErrorCode.CLOCK_ROLLBACK,
                "injected logical clock moved backwards beyond tolerance",
                {
                    "last_clock_ms": last,
                    "now_ms": now_ms,
                    "tolerance_ms": tolerance_ms,
                    "rollback_ms": last - now_ms,
                },
            )
        if update and now_ms > last:
            self.set_meta("last_clock_ms", now_ms)

    def ensure_initialized(self) -> None:
        if self.db_path.exists():
            row = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_meta'"
            ).fetchone()
            if row is None:
                raise AppError(
                    ErrorCode.STATE_NOT_INITIALIZED,
                    "state database schema is missing",
                    {"db": str(self.db_path)},
                )
        else:
            raise AppError(
                ErrorCode.STATE_NOT_INITIALIZED,
                "state database does not exist",
                {"db": str(self.db_path)},
            )
        if self.get_meta("initialized") != "1":
            raise AppError(
                ErrorCode.STATE_NOT_INITIALIZED,
                "state database is not initialized",
                {"db": str(self.db_path)},
            )

    def reset_domain(self) -> None:
        for table in reversed(ALL_TABLES):
            self.db.execute(f"DELETE FROM {table}")
        self.db.execute(
            "DELETE FROM sqlite_sequence WHERE name IN "
            "('_checkpoint_versions','_invalidations','_edits','_recovery_events')"
        )

    def replace_with_checkpoint(
        self,
        merge: MergeResult,
        wal_rows: list[WalRow],
        now_ms: int,
    ) -> dict[str, int]:
        self.reset_domain()
        self.init_metadata(now_ms)
        self.db.executemany(
            """
            INSERT INTO _wal_rows
            (txn_id, segment, segment_no, line_no, table_name, row_key, op,
             before_json, after_json, checkpoint_gen, content_hash, commit_ms,
             deleted, updated_clock_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    row.txn_id,
                    row.segment,
                    row.segment_no,
                    row.line_no,
                    row.table,
                    row.key,
                    row.op,
                    (
                        canonical_json(row.before)
                        if row.before is not None
                        else None
                    ),
                    (
                        canonical_json(row.after)
                        if row.after is not None
                        else None
                    ),
                    row.checkpoint_gen,
                    row.content_hash,
                    row.commit_ms,
                    now_ms,
                )
                for row in wal_rows
            ],
        )
        for version in merge.versions:
            cursor = self.db.execute(
                """
                INSERT INTO _checkpoint_versions
                (generation, source_format, source_path, source_hash,
                 saved_at_ms, replay_generation, applied_watermark,
                 manifest_version, base_generation, base_manifest_hash,
                 manifest_hash, selected, invalidated,
                 decision_reason_json, conversion_json, manifest_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    version.generation,
                    version.source_format,
                    version.source_path,
                    version.source_hash,
                    version.saved_at_ms,
                    version.replay_generation,
                    version.applied_watermark,
                    version.manifest_version,
                    version.base_generation,
                    version.base_manifest_hash,
                    version.manifest_hash,
                    1 if version.selected else 0,
                    canonical_json(version.decision_reason),
                    (
                        canonical_json(version.conversion)
                        if version.conversion is not None
                        else None
                    ),
                    canonical_json(version.manifest),
                ),
            )
            version_id = int(cursor.lastrowid)
            version.version_id = version_id
            for row in version.snapshot_rows:
                self.db.execute(
                    """
                    INSERT INTO _checkpoint_snapshot
                    (version_id, table_name, row_key, version, txn_id,
                     deleted, value_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        row.table,
                        row.key,
                        row.version,
                        row.txn_id,
                        1 if row.deleted else 0,
                        (
                            canonical_json(row.value)
                            if row.value is not None
                            else None
                        ),
                    ),
                )
            for shard in version.shards:
                self.db.execute(
                    """
                    INSERT INTO _checkpoint_shards
                    (version_id, shard_name, shard_hash, table_name,
                     row_count, content_digest, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        shard["name"],
                        shard["shard_hash"],
                        shard.get("table"),
                        shard.get("row_count"),
                        shard.get("content_digest"),
                        canonical_json(shard.get("payload", {})),
                    ),
                )
        baseline = merge.baseline
        if baseline is not None:
            if baseline.version_id is None:
                raise RuntimeError("baseline checkpoint was not persisted")
            self._set_pointer(baseline, now_ms)
            self._seed_baseline(baseline)
            self.set_meta("applied_watermark", baseline.applied_watermark)
            self.set_meta("current_checkpoint_gen", baseline.generation)
            self.set_meta("current_checkpoint_version_id", baseline.version_id)
            self.set_meta("current_manifest_version", baseline.manifest_version)
            self.set_meta("current_replay_generation", baseline.replay_generation)
        else:
            self.set_meta("applied_watermark", 0)
            self.set_meta("current_checkpoint_gen", 0)
            self.set_meta("current_checkpoint_version_id", 0)
            self.set_meta("current_manifest_version", 0)
            self.set_meta("current_replay_generation", 0)
        for row in wal_rows:
            if row.txn_id <= self.get_meta_int("applied_watermark"):
                self.db.execute(
                    """
                    INSERT INTO _txn
                    (txn_id, status, commit_ms, checkpoint_gen, segment,
                     line_no, input_hash, replay_ms, latency_ms, precision_ms,
                     updated_clock_ms, note)
                    VALUES (?, 'checkpointed', ?, ?, ?, ?, ?, NULL, NULL,
                            NULL, ?, ?)
                    ON CONFLICT(txn_id) DO UPDATE SET
                        status='checkpointed',
                        commit_ms=excluded.commit_ms,
                        checkpoint_gen=excluded.checkpoint_gen,
                        segment=excluded.segment,
                        line_no=excluded.line_no,
                        input_hash=excluded.input_hash,
                        updated_clock_ms=excluded.updated_clock_ms,
                        note=excluded.note
                    """,
                    (
                        row.txn_id,
                        row.commit_ms,
                        row.checkpoint_gen,
                        row.segment,
                        row.line_no,
                        row.content_hash,
                        now_ms,
                        "loaded from selected checkpoint baseline",
                    ),
                )
        self.set_meta("needs_rebuild", 0)
        self.set_meta("rebuild_from_txn", 0)
        self.set_meta("last_clock_ms", max(now_ms, self.get_meta_int("last_clock_ms")))
        return {
            "checkpoint_versions": len(merge.versions),
            "selected_generations": len(merge.selected),
            "wal_rows": len(wal_rows),
        }

    def _set_pointer(self, version: CheckpointVersion, now_ms: int) -> None:
        if version.version_id is None:
            raise RuntimeError("checkpoint pointer requires a persisted version")
        self.db.execute("DELETE FROM _checkpoint_pointer")
        self.db.execute(
            """
            INSERT INTO _checkpoint_pointer
            (singleton, generation, version_id, manifest_version,
             replay_generation, applied_watermark, saved_at_ms,
             manifest_hash, updated_clock_ms)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.generation,
                version.version_id,
                version.manifest_version,
                version.replay_generation,
                version.applied_watermark,
                version.saved_at_ms,
                version.manifest_hash,
                now_ms,
            ),
        )

    def _seed_baseline(self, version: CheckpointVersion) -> None:
        self.db.execute("DELETE FROM _rows")
        self.db.execute("DELETE FROM _row_history")
        for row in version.snapshot_rows:
            value_json = (
                canonical_json(row.value) if row.value is not None else None
            )
            self.db.execute(
                """
                INSERT INTO _rows
                (table_name, row_key, version, txn_id, deleted, value_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row.table,
                    row.key,
                    row.version,
                    row.txn_id,
                    1 if row.deleted else 0,
                    value_json,
                ),
            )
            commit_ms = 0
            if row.txn_id:
                commit_row = self.db.execute(
                    "SELECT commit_ms FROM _wal_rows WHERE txn_id = ?",
                    (row.txn_id,),
                ).fetchone()
                if commit_row is not None:
                    commit_ms = int(commit_row["commit_ms"])
            self.db.execute(
                """
                INSERT INTO _row_history
                (table_name, row_key, version, txn_id, deleted, value_json,
                 commit_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.table,
                    row.key,
                    row.version,
                    row.txn_id,
                    1 if row.deleted else 0,
                    value_json,
                    commit_ms,
                ),
            )

    def wal_rows(self, include_deleted: bool = False) -> list[WalRow]:
        where = "" if include_deleted else "WHERE deleted = 0"
        rows = self.db.execute(
            f"SELECT * FROM _wal_rows {where} "
            "ORDER BY segment_no, line_no, txn_id"
        ).fetchall()
        return [row_from_db(dict(row)) for row in rows]

    def wal_record(self, txn_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM _wal_rows WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def checkpoint_generations(self) -> set[int]:
        return {
            int(row["generation"])
            for row in self.db.execute(
                "SELECT DISTINCT generation FROM _checkpoint_versions"
            )
        }

    def current_pointer(self) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM _checkpoint_pointer WHERE singleton = 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def current_version(self) -> CheckpointVersion | None:
        pointer = self.current_pointer()
        if pointer is None:
            return None
        row = self.db.execute(
            "SELECT * FROM _checkpoint_versions WHERE version_id = ?",
            (pointer["version_id"],),
        ).fetchone()
        if row is None:
            raise AppError(
                ErrorCode.CHECKPOINT_CHAIN_BROKEN,
                "checkpoint pointer references a missing version",
                {"version_id": pointer["version_id"]},
            )
        return self._version_from_row(dict(row))

    def _version_from_row(self, row: dict[str, Any]) -> CheckpointVersion:
        version_id = int(row["version_id"])
        snapshot = []
        for snap in self.db.execute(
            "SELECT * FROM _checkpoint_snapshot WHERE version_id = ? "
            "ORDER BY table_name, row_key",
            (version_id,),
        ):
            snapshot.append(
                SnapshotRow(
                    table=snap["table_name"],
                    key=snap["row_key"],
                    version=int(snap["version"]),
                    txn_id=int(snap["txn_id"]),
                    deleted=bool(snap["deleted"]),
                    value=(
                        json.loads(snap["value_json"])
                        if snap["value_json"] is not None
                        else None
                    ),
                )
            )
        shards = []
        for shard in self.db.execute(
            "SELECT * FROM _checkpoint_shards WHERE version_id = ? "
            "ORDER BY shard_name",
            (version_id,),
        ):
            shards.append(
                {
                    "name": shard["shard_name"],
                    "shard_hash": shard["shard_hash"],
                    "table": shard["table_name"],
                    "row_count": shard["row_count"],
                    "content_digest": shard["content_digest"],
                    "payload": json.loads(shard["payload_json"]),
                }
            )
        return CheckpointVersion(
            generation=int(row["generation"]),
            source_format=row["source_format"],
            source_path=row["source_path"],
            source_hash=row["source_hash"],
            saved_at_ms=int(row["saved_at_ms"]),
            replay_generation=int(row["replay_generation"]),
            applied_watermark=int(row["applied_watermark"]),
            snapshot_rows=snapshot,
            tables={},
            manifest_version=int(row["manifest_version"]),
            base_generation=int(row["base_generation"]),
            base_manifest_hash=row["base_manifest_hash"],
            manifest_hash=row["manifest_hash"],
            manifest=json.loads(row["manifest_json"]),
            conversion=(
                json.loads(row["conversion_json"])
                if row["conversion_json"]
                else None
            ),
            shards=shards,
            selected=bool(row["selected"]),
            decision_reason=json.loads(row["decision_reason_json"]),
            invalidated=bool(row["invalidated"]),
            version_id=version_id,
        )

    def checkpoint_versions(self, selected_only: bool = False) -> list[CheckpointVersion]:
        where = "WHERE selected = 1" if selected_only else ""
        rows = self.db.execute(
            f"SELECT * FROM _checkpoint_versions {where} "
            "ORDER BY generation, source_format, source_hash"
        ).fetchall()
        return [self._version_from_row(dict(row)) for row in rows]

    def current_rows_map(self) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self.db.execute(
            "SELECT * FROM _rows ORDER BY table_name, row_key"
        ):
            result[(row["table_name"], row["row_key"])] = {
                "table": row["table_name"],
                "key": row["row_key"],
                "version": int(row["version"]),
                "txn_id": int(row["txn_id"]),
                "deleted": bool(row["deleted"]),
                "value": (
                    json.loads(row["value_json"])
                    if row["value_json"] is not None
                    else None
                ),
            }
        return result

    def txn_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM _txn ORDER BY txn_id"
            )
        ]

    def delay_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM _delays ORDER BY txn_id"
            )
        ]

    def operation(self, op_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM _operations WHERE op_id = ?", (op_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def pending_operations(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM _operations WHERE status = 'pending' "
                "ORDER BY op_id"
            )
        ]

    def next_operation_id(self, kind: str) -> str:
        counter = self.get_meta_int("op_counter") + 1
        self.set_meta("op_counter", counter)
        return f"{kind}:{counter:08d}"

    def begin_operation(
        self,
        kind: str,
        payload: dict[str, Any],
        now_ms: int,
        tolerance_ms: int = 0,
    ) -> str:
        with self.transaction():
            self.check_clock(now_ms, tolerance_ms)
            op_id = self.next_operation_id(kind)
            self.db.execute(
                """
                INSERT INTO _operations
                (op_id, kind, status, created_clock_ms, updated_clock_ms,
                 payload_json, result_json)
                VALUES (?, ?, 'pending', ?, ?, ?, NULL)
                """,
                (op_id, kind, now_ms, now_ms, canonical_json(payload)),
            )
        return op_id

    def insert_checkpoint_version(
        self, version: CheckpointVersion, *, invalidated: bool = False
    ) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO _checkpoint_versions
            (generation, source_format, source_path, source_hash,
             saved_at_ms, replay_generation, applied_watermark,
             manifest_version, base_generation, base_manifest_hash,
             manifest_hash, selected, invalidated,
             decision_reason_json, conversion_json, manifest_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.generation,
                version.source_format,
                version.source_path,
                version.source_hash,
                version.saved_at_ms,
                version.replay_generation,
                version.applied_watermark,
                version.manifest_version,
                version.base_generation,
                version.base_manifest_hash,
                version.manifest_hash,
                1 if version.selected else 0,
                1 if invalidated or version.invalidated else 0,
                canonical_json(version.decision_reason),
                (
                    canonical_json(version.conversion)
                    if version.conversion is not None
                    else None
                ),
                canonical_json(version.manifest),
            ),
        )
        version_id = int(cursor.lastrowid)
        version.version_id = version_id
        for row in version.snapshot_rows:
            self.db.execute(
                """
                INSERT INTO _checkpoint_snapshot
                (version_id, table_name, row_key, version, txn_id,
                 deleted, value_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    row.table,
                    row.key,
                    row.version,
                    row.txn_id,
                    1 if row.deleted else 0,
                    (
                        canonical_json(row.value)
                        if row.value is not None
                        else None
                    ),
                ),
            )
        for shard in version.shards:
            self.db.execute(
                """
                INSERT INTO _checkpoint_shards
                (version_id, shard_name, shard_hash, table_name,
                 row_count, content_digest, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    shard["name"],
                    shard["shard_hash"],
                    shard.get("table"),
                    shard.get("row_count"),
                    shard.get("content_digest"),
                    canonical_json(shard.get("payload", {})),
                ),
            )
        return version_id

    def finish_operation(
        self, op_id: str, status: str, now_ms: int, result: dict[str, Any]
    ) -> None:
        self.db.execute(
            """
            UPDATE _operations
            SET status = ?, updated_clock_ms = ?, result_json = ?
            WHERE op_id = ?
            """,
            (status, now_ms, canonical_json(result), op_id),
        )

    def record_recovery(
        self, now_ms: int, action: str, details: dict[str, Any]
    ) -> None:
        self.db.execute(
            """
            INSERT INTO _recovery_events(clock_ms, action, details_json)
            VALUES (?, ?, ?)
            """,
            (now_ms, action, canonical_json(details)),
        )

    def replace_pointer_in_transaction(
        self,
        version: CheckpointVersion,
        now_ms: int,
    ) -> None:
        self.db.execute("DROP TABLE IF EXISTS temp.checkpoint_pointer_stage")
        self.db.execute(
            """
            CREATE TEMP TABLE checkpoint_pointer_stage (
                singleton INTEGER PRIMARY KEY,
                generation INTEGER NOT NULL,
                version_id INTEGER NOT NULL,
                manifest_version INTEGER NOT NULL,
                replay_generation INTEGER NOT NULL,
                applied_watermark INTEGER NOT NULL,
                saved_at_ms INTEGER NOT NULL,
                manifest_hash TEXT,
                updated_clock_ms INTEGER NOT NULL
            )
            """
        )
        if version.version_id is None:
            raise RuntimeError("checkpoint version must be persisted")
        self.db.execute(
            """
            INSERT INTO checkpoint_pointer_stage
            (singleton, generation, version_id, manifest_version,
             replay_generation, applied_watermark, saved_at_ms,
             manifest_hash, updated_clock_ms)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.generation,
                version.version_id,
                version.manifest_version,
                version.replay_generation,
                version.applied_watermark,
                version.saved_at_ms,
                version.manifest_hash,
                now_ms,
            ),
        )
        self.db.execute("DELETE FROM _checkpoint_pointer")
        self.db.execute(
            "INSERT INTO _checkpoint_pointer "
            "SELECT * FROM checkpoint_pointer_stage"
        )
        self.db.execute("DROP TABLE checkpoint_pointer_stage")
        self.set_meta("current_checkpoint_gen", version.generation)
        self.set_meta("current_checkpoint_version_id", version.version_id)
        self.set_meta("current_manifest_version", version.manifest_version)
        self.set_meta("current_replay_generation", version.replay_generation)

    def clear_pointer(self) -> None:
        self.db.execute("DELETE FROM _checkpoint_pointer")
        self.set_meta("current_checkpoint_gen", 0)
        self.set_meta("current_checkpoint_version_id", 0)
        self.set_meta("current_manifest_version", 0)
        self.set_meta("current_replay_generation", 0)
