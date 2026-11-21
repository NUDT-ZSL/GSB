from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .canonical import (
    canonical_json,
    row_digest,
    sha256_json,
    summarize_tables,
)
from .errors import (
    apply_conflict,
    state_error,
    version_rollback,
)
from .models import Checkpoint, TableRow, WalRecord

SCHEMA_VERSION = 1


class FileLock:
    """Cross-platform advisory process lock backed by a dedicated file."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(f"{db_path}.lock")
        self._handle: Any = None
        self._windows_locked = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            import msvcrt
            import time

            self._handle.seek(0, 2)
            if self._handle.tell() == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            while True:
                try:
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
                    self._windows_locked = True
                    break
                except OSError:
                    time.sleep(0.01)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._handle is None:
            return
        if self._windows_locked:
            import msvcrt

            try:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass
        self._handle.close()
        self._handle = None
        self._windows_locked = False


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).resolve()
        self.lock = FileLock(self.path)

    def exists(self) -> bool:
        return self.path.is_file()

    def connect(self, *, writable: bool = True) -> sqlite3.Connection:
        if not writable and not self.path.is_file():
            raise state_error("state database does not exist", database=self.path.name)
        if writable:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if writable:
                connection = sqlite3.connect(self.path, timeout=30.0)
            else:
                uri = f"file:{self.path.as_posix()}?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        except sqlite3.Error as exc:
            raise state_error(
                "cannot open SQLite state database",
                database=self.path.name,
                reason=str(exc),
            ) from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if writable:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
            except sqlite3.Error as exc:
                connection.close()
                raise state_error(
                    "cannot configure SQLite WAL mode",
                    database=self.path.name,
                    reason=str(exc),
                ) from exc
        return connection

    @contextlib.contextmanager
    def connection(self, *, writable: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect(writable=writable)
        try:
            yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect(writable=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, *, reset: bool = False) -> None:
        if reset:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        with self.lock, self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_rows (
                    logical_table TEXT NOT NULL,
                    row_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    source_tx INTEGER NOT NULL,
                    tombstone INTEGER NOT NULL CHECK (tombstone IN (0, 1)),
                    row_hash TEXT NOT NULL,
                    PRIMARY KEY (logical_table, row_key)
                );
                CREATE TABLE IF NOT EXISTS applied_txs (
                    txid INTEGER PRIMARY KEY,
                    tx_hash TEXT NOT NULL,
                    checkpoint_gen INTEGER NOT NULL,
                    applied_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS in_flight (
                    txid INTEGER PRIMARY KEY,
                    tx_hash TEXT NOT NULL,
                    started_at_ms INTEGER NOT NULL,
                    lease_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tx_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round_id TEXT NOT NULL,
                    seq_in_round INTEGER NOT NULL,
                    txid INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    started_at_ms INTEGER,
                    completed_at_ms INTEGER,
                    UNIQUE (round_id, txid)
                );
                CREATE TABLE IF NOT EXISTS checkpoint_pointer (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    pointer_version INTEGER NOT NULL,
                    checkpoint_gen INTEGER NOT NULL,
                    manifest_version INTEGER NOT NULL,
                    replay_generation INTEGER NOT NULL,
                    applied_watermark INTEGER NOT NULL,
                    chain_hash TEXT NOT NULL,
                    logical_clock_ms INTEGER NOT NULL,
                    commit_ts_ms INTEGER NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    format_version TEXT NOT NULL,
                    parent_checkpoint_gen INTEGER,
                    table_summaries_json TEXT NOT NULL,
                    conversion_json TEXT
                );
                CREATE TABLE IF NOT EXISTS checkpoint_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pointer_version INTEGER NOT NULL,
                    checkpoint_gen INTEGER NOT NULL UNIQUE,
                    manifest_version INTEGER NOT NULL,
                    replay_generation INTEGER NOT NULL,
                    applied_watermark INTEGER NOT NULL,
                    chain_hash TEXT NOT NULL,
                    logical_clock_ms INTEGER NOT NULL,
                    commit_ts_ms INTEGER NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    format_version TEXT NOT NULL,
                    parent_checkpoint_gen INTEGER,
                    table_summaries_json TEXT NOT NULL,
                    conversion_json TEXT,
                    decision TEXT NOT NULL,
                    installed_at_ms INTEGER NOT NULL
                );
                """
            )
            existing = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                self.set_meta(connection, "schema_version", str(SCHEMA_VERSION))
                self.set_meta(connection, "applied_watermark", "0")
                self.set_meta(connection, "logical_clock_ms", "0")
                self.set_meta(connection, "last_checkpoint_clock_ms", "0")
                self.set_meta(connection, "round_counter", "0")
                self.set_meta(connection, "last_applied_tx", "0")
                self.set_meta(connection, "state_table_summaries", "{}")
                self.set_meta(connection, "state_rows_digest", sha256_json({}))
                self.set_meta(connection, "checkpoint_base_watermark", "0")
            elif int(existing["value"]) != SCHEMA_VERSION:
                raise state_error(
                    "unsupported state schema version",
                    expected=SCHEMA_VERSION,
                    actual=int(existing["value"]),
                )

    def require_initialized(self) -> None:
        if not self.exists():
            raise state_error("state database does not exist; run load first")
        with self.connection(writable=False) as connection:
            try:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
            except sqlite3.Error as exc:
                raise state_error("state database is not initialized", reason=str(exc)) from exc
        if row is None:
            raise state_error("state database is not initialized")

    @staticmethod
    def get_meta(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def get_meta_int(connection: sqlite3.Connection, key: str, default: int = 0) -> int:
        value = Store.get_meta(connection, key)
        return default if value is None else int(value)

    @staticmethod
    def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @staticmethod
    def set_meta_json(connection: sqlite3.Connection, key: str, value: Any) -> None:
        Store.set_meta(connection, key, canonical_json(value))

    def rows_snapshot(
        self,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        owned = connection is None
        conn = connection or self.connect(writable=False)
        try:
            rows = conn.execute(
                """
                SELECT logical_table, row_key, payload_json, version,
                       source_tx, tombstone, row_hash
                FROM data_rows
                ORDER BY logical_table, row_key
                """
            ).fetchall()
        finally:
            if owned:
                conn.close()
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            table = str(row["logical_table"])
            result.setdefault(table, []).append(
                {
                    "key": str(row["row_key"]),
                    "payload": json.loads(str(row["payload_json"])),
                    "row_hash": str(row["row_hash"]),
                    "source_tx": int(row["source_tx"]),
                    "tombstone": bool(row["tombstone"]),
                    "version": int(row["version"]),
                }
            )
        return {table: result[table] for table in sorted(result)}

    def summaries_snapshot(
        self,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, dict[str, Any]]:
        return summarize_tables(self.rows_snapshot(connection))

    @staticmethod
    def _row_tuple(
        row: TableRow | Mapping[str, Any],
        *,
        table_override: str | None = None,
    ) -> tuple[Any, ...]:
        if isinstance(row, TableRow):
            table = row.table
            key = row.key
            payload = row.payload
            version = row.version
            source_tx = row.source_tx
            tombstone = row.tombstone
            row_hash = row.row_hash
        else:
            table = str(row.get("table", table_override))
            if table == "None":
                raise state_error("seed row is missing its table name")
            key = str(row["key"])
            payload = row["payload"]
            version = int(row["version"])
            source_tx = int(row["source_tx"])
            tombstone = bool(row["tombstone"])
            row_hash = str(row["row_hash"])
        return (
            table,
            key,
            canonical_json(payload),
            version,
            source_tx,
            int(tombstone),
            row_hash,
        )

    def seed_state(
        self,
        checkpoint: Checkpoint,
        records: Sequence[WalRecord],
        rows_by_table: Mapping[str, Sequence[TableRow | Mapping[str, Any]]],
        *,
        installed_at_ms: int,
    ) -> None:
        watermark = checkpoint.applied_watermark
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM checkpoint_pointer WHERE singleton = 1"
            ).fetchone()
            if existing is not None:
                raise state_error("state database already contains a checkpoint pointer")
            for table, rows in rows_by_table.items():
                for row in rows:
                    tuple_row = self._row_tuple(row, table_override=table)
                    if tuple_row[0] != table:
                        raise state_error("seed row table differs from its shard", table=table)
                    connection.execute(
                        """
                        INSERT INTO data_rows(
                            logical_table, row_key, payload_json, version,
                            source_tx, tombstone, row_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        tuple_row,
                    )
            record_by_id = {
                record.logical_tx_id: record
                for record in records
                if record.logical_tx_id <= watermark
            }
            for txid in range(1, watermark + 1):
                record = record_by_id.get(txid)
                if record is not None:
                    tx_hash = record.tx_hash
                    checkpoint_gen = record.checkpoint_gen
                else:
                    tx_hash = (
                        checkpoint.chain_hash
                        if txid == watermark
                        else sha256_json(
                            {
                                "checkpoint_gen": checkpoint.checkpoint_gen,
                                "txid": txid,
                            }
                        )
                    )
                    checkpoint_gen = checkpoint.checkpoint_gen
                connection.execute(
                    """
                    INSERT INTO applied_txs(
                        txid, tx_hash, checkpoint_gen, applied_at_ms
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        txid,
                        tx_hash,
                        checkpoint_gen,
                        checkpoint.logical_clock_ms,
                    ),
                )
            self.set_meta(connection, "applied_watermark", str(watermark))
            self.set_meta(connection, "last_applied_tx", str(watermark))
            self.set_meta(connection, "checkpoint_base_watermark", str(watermark))
            self.set_meta(connection, "logical_clock_ms", str(checkpoint.logical_clock_ms))
            self.set_meta(connection, "last_checkpoint_clock_ms", str(checkpoint.logical_clock_ms))
            self.set_meta(connection, "round_counter", "0")
            summaries = summarize_tables(
                {
                    table: [
                        row.as_dict() if isinstance(row, TableRow) else dict(row)
                        for row in table_rows
                    ]
                    for table, table_rows in rows_by_table.items()
                }
            )
            self.set_meta_json(connection, "state_table_summaries", summaries)
            self.set_meta(
                connection,
                "state_rows_digest",
                sha256_json(summaries),
            )
            self.replace_checkpoint_pointer(
                connection,
                checkpoint,
                decision="load",
                installed_at_ms=installed_at_ms,
            )

    def replace_checkpoint_pointer(
        self,
        connection: sqlite3.Connection,
        checkpoint: Checkpoint,
        *,
        decision: str,
        installed_at_ms: int,
    ) -> int:
        current = connection.execute(
            "SELECT pointer_version, checkpoint_gen FROM checkpoint_pointer WHERE singleton = 1"
        ).fetchone()
        pointer_version = 1 if current is None else int(current["pointer_version"]) + 1
        if current is not None and checkpoint.checkpoint_gen < int(current["checkpoint_gen"]):
            raise version_rollback(
                "checkpoint pointer replacement would move to an older generation",
                current_generation=int(current["checkpoint_gen"]),
                incoming_generation=checkpoint.checkpoint_gen,
            )
        conversion_json = None
        if checkpoint.v1_conversion is not None:
            conversion_json = canonical_json(
                {
                    "absolute_ms": checkpoint.v1_conversion.absolute_ms,
                    "base_logical_ms": checkpoint.v1_conversion.base_logical_ms,
                    "relative_ms": checkpoint.v1_conversion.relative_ms,
                    "rounding": checkpoint.v1_conversion.rounding,
                    "scale": checkpoint.v1_conversion.scale,
                }
            )
        values = (
            pointer_version,
            checkpoint.checkpoint_gen,
            checkpoint.manifest_version,
            checkpoint.replay_generation,
            checkpoint.applied_watermark,
            checkpoint.chain_hash,
            checkpoint.logical_clock_ms,
            checkpoint.commit_ts_ms,
            checkpoint.manifest_hash,
            checkpoint.path.name,
            checkpoint.format_version,
            checkpoint.parent_checkpoint_gen,
            canonical_json(checkpoint.table_summaries),
            conversion_json,
        )
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS _checkpoint_pointer_stage (
                pointer_version INTEGER NOT NULL,
                checkpoint_gen INTEGER NOT NULL,
                manifest_version INTEGER NOT NULL,
                replay_generation INTEGER NOT NULL,
                applied_watermark INTEGER NOT NULL,
                chain_hash TEXT NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                commit_ts_ms INTEGER NOT NULL,
                manifest_hash TEXT NOT NULL,
                source_path TEXT NOT NULL,
                format_version TEXT NOT NULL,
                parent_checkpoint_gen INTEGER,
                table_summaries_json TEXT NOT NULL,
                conversion_json TEXT
            )
            """
        )
        connection.execute("DELETE FROM _checkpoint_pointer_stage")
        connection.execute(
            """
            INSERT INTO _checkpoint_pointer_stage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        try:
            connection.execute(
                """
                INSERT INTO checkpoint_history(
                    pointer_version, checkpoint_gen, manifest_version,
                    replay_generation, applied_watermark, chain_hash, logical_clock_ms,
                    commit_ts_ms, manifest_hash, source_path, format_version,
                    parent_checkpoint_gen, table_summaries_json,
                    conversion_json, decision, installed_at_ms
                )
                SELECT pointer_version, checkpoint_gen, manifest_version,
                       replay_generation, applied_watermark, chain_hash, logical_clock_ms,
                       commit_ts_ms, manifest_hash, source_path, format_version,
                       parent_checkpoint_gen, table_summaries_json,
                       conversion_json, ?, ?
                FROM _checkpoint_pointer_stage
                """,
                (decision, installed_at_ms),
            )
        except sqlite3.IntegrityError as exc:
            raise version_rollback(
                "checkpoint generation has already been committed",
                checkpoint_gen=checkpoint.checkpoint_gen,
            ) from exc
        connection.execute("DELETE FROM checkpoint_pointer")
        connection.execute(
            """
            INSERT INTO checkpoint_pointer(
                singleton, pointer_version, checkpoint_gen, manifest_version,
                replay_generation, applied_watermark, chain_hash, logical_clock_ms,
                commit_ts_ms, manifest_hash, source_path, format_version,
                parent_checkpoint_gen, table_summaries_json, conversion_json
            )
            SELECT 1, pointer_version, checkpoint_gen, manifest_version,
                   replay_generation, applied_watermark, chain_hash, logical_clock_ms,
                   commit_ts_ms, manifest_hash, source_path, format_version,
                   parent_checkpoint_gen, table_summaries_json, conversion_json
            FROM _checkpoint_pointer_stage
            """
        )
        return pointer_version

    def get_pointer(self, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        owned = connection is None
        conn = connection or self.connect(writable=False)
        try:
            row = conn.execute(
                "SELECT * FROM checkpoint_pointer WHERE singleton = 1"
            ).fetchone()
        finally:
            if owned:
                conn.close()
        if row is None:
            return None
        result = dict(row)
        result["table_summaries"] = json.loads(result.pop("table_summaries_json"))
        conversion = result.pop("conversion_json")
        result["conversion"] = None if conversion is None else json.loads(conversion)
        return result

    def applied_txids(self, connection: sqlite3.Connection | None = None) -> list[int]:
        owned = connection is None
        conn = connection or self.connect(writable=False)
        try:
            rows = conn.execute("SELECT txid FROM applied_txs ORDER BY txid").fetchall()
        finally:
            if owned:
                conn.close()
        return [int(row["txid"]) for row in rows]

    def applied_tx_map(
        self,
        connection: sqlite3.Connection | None = None,
    ) -> dict[int, dict[str, Any]]:
        owned = connection is None
        conn = connection or self.connect(writable=False)
        try:
            rows = conn.execute(
                """
                SELECT txid, tx_hash, checkpoint_gen, applied_at_ms
                FROM applied_txs ORDER BY txid
                """
            ).fetchall()
        finally:
            if owned:
                conn.close()
        return {
            int(row["txid"]): {
                "tx_hash": str(row["tx_hash"]),
                "checkpoint_gen": int(row["checkpoint_gen"]),
                "applied_at_ms": int(row["applied_at_ms"]),
            }
            for row in rows
        }

    @staticmethod
    def contiguous_watermark(connection: sqlite3.Connection) -> int:
        rows = connection.execute("SELECT txid FROM applied_txs ORDER BY txid").fetchall()
        watermark = 0
        for row in rows:
            txid = int(row["txid"])
            if txid == watermark + 1:
                watermark = txid
            elif txid > watermark + 1:
                break
        return watermark

    def repair_watermark(self, connection: sqlite3.Connection) -> tuple[int, int]:
        old = self.get_meta_int(connection, "applied_watermark")
        watermark = self.contiguous_watermark(connection)
        self.set_meta(connection, "applied_watermark", str(watermark))
        applied = connection.execute("SELECT COUNT(*) AS count FROM applied_txs").fetchone()
        self.set_meta(connection, "last_applied_tx", str(watermark))
        self.set_meta(connection, "applied_tx_count", str(int(applied["count"])))
        return old, watermark

    def in_flight(self, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        owned = connection is None
        conn = connection or self.connect(writable=False)
        try:
            rows = conn.execute(
                "SELECT txid, tx_hash, started_at_ms, lease_id FROM in_flight ORDER BY txid"
            ).fetchall()
        finally:
            if owned:
                conn.close()
        return [dict(row) for row in rows]

    def register_in_flight(
        self,
        record: WalRecord,
        *,
        started_at_ms: int,
        lease_id: str,
    ) -> bool:
        with self.transaction() as connection:
            applied = connection.execute(
                "SELECT 1 FROM applied_txs WHERE txid = ?",
                (record.logical_tx_id,),
            ).fetchone()
            if applied is not None:
                return False
            connection.execute(
                """
                INSERT INTO in_flight(txid, tx_hash, started_at_ms, lease_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(txid) DO UPDATE SET
                    tx_hash = excluded.tx_hash,
                    started_at_ms = excluded.started_at_ms,
                    lease_id = excluded.lease_id
                """,
                (
                    record.logical_tx_id,
                    record.tx_hash,
                    started_at_ms,
                    lease_id,
                ),
            )
            return True

    def clear_in_flight(self, connection: sqlite3.Connection, txid: int) -> None:
        connection.execute("DELETE FROM in_flight WHERE txid = ?", (txid,))

    def cleanup_in_flight(self) -> list[int]:
        removed: list[int] = []
        with self.transaction() as connection:
            rows = connection.execute("SELECT txid FROM in_flight ORDER BY txid").fetchall()
            for row in rows:
                txid = int(row["txid"])
                applied = connection.execute(
                    "SELECT 1 FROM applied_txs WHERE txid = ?",
                    (txid,),
                ).fetchone()
                if applied is not None:
                    connection.execute("DELETE FROM in_flight WHERE txid = ?", (txid,))
                    removed.append(txid)
            connection.execute("DELETE FROM in_flight")
        return removed

    def next_round_id(self) -> tuple[int, str]:
        with self.transaction() as connection:
            counter = self.get_meta_int(connection, "round_counter") + 1
            self.set_meta(connection, "round_counter", str(counter))
            return counter, f"round-{counter:08d}"

    def record_outcome(
        self,
        *,
        round_id: str,
        sequence: int,
        txid: int,
        status: str,
        reason: str,
        started_at_ms: int | None,
        completed_at_ms: int | None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO tx_outcomes(
                    round_id, seq_in_round, txid, status, reason,
                    started_at_ms, completed_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(round_id, txid) DO UPDATE SET
                    seq_in_round = excluded.seq_in_round,
                    status = excluded.status,
                    reason = excluded.reason,
                    started_at_ms = excluded.started_at_ms,
                    completed_at_ms = excluded.completed_at_ms
                """,
                (
                    round_id,
                    sequence,
                    txid,
                    status,
                    reason,
                    started_at_ms,
                    completed_at_ms,
                ),
            )

    def current_row(
        self,
        connection: sqlite3.Connection,
        table: str,
        key: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT logical_table, row_key, payload_json, version,
                   source_tx, tombstone, row_hash
            FROM data_rows WHERE logical_table = ? AND row_key = ?
            """,
            (table, key),
        ).fetchone()
        if row is None:
            return None
        return {
            "table": str(row["logical_table"]),
            "key": str(row["row_key"]),
            "payload": json.loads(str(row["payload_json"])),
            "version": int(row["version"]),
            "source_tx": int(row["source_tx"]),
            "tombstone": bool(row["tombstone"]),
            "row_hash": str(row["row_hash"]),
        }

    def apply_record(
        self,
        record: WalRecord,
        *,
        applied_at_ms: int,
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        existing = connection.execute(
            "SELECT 1 FROM applied_txs WHERE txid = ?",
            (record.logical_tx_id,),
        ).fetchone()
        if existing is not None:
            return {"status": "duplicate", "txid": record.logical_tx_id}

        current = self.current_row(connection, record.table, record.row_key)
        if record.operation == "INSERT":
            if current is not None and not current["tombstone"]:
                raise apply_conflict(
                    "INSERT targets a live row",
                    txid=record.logical_tx_id,
                    table=record.table,
                    row_key=record.row_key,
                )
            version = 1 if current is None else current["version"] + 1
            payload = record.after
            tombstone = False
        elif record.operation == "UPDATE":
            if current is None or current["tombstone"]:
                raise apply_conflict(
                    "UPDATE targets a missing or tombstoned row",
                    txid=record.logical_tx_id,
                    table=record.table,
                    row_key=record.row_key,
                )
            if current["payload"] != record.before:
                raise apply_conflict(
                    "UPDATE before image differs from the persisted row",
                    txid=record.logical_tx_id,
                    table=record.table,
                    row_key=record.row_key,
                )
            version = current["version"] + 1
            payload = record.after
            tombstone = False
        elif record.operation == "DELETE":
            if current is None or current["tombstone"]:
                raise apply_conflict(
                    "DELETE targets a missing or tombstoned row",
                    txid=record.logical_tx_id,
                    table=record.table,
                    row_key=record.row_key,
                )
            if current["payload"] != record.before:
                raise apply_conflict(
                    "DELETE before image differs from the persisted row",
                    txid=record.logical_tx_id,
                    table=record.table,
                    row_key=record.row_key,
                )
            version = current["version"] + 1
            payload = None
            tombstone = True
        else:  # pragma: no cover - parser closes the enum
            raise apply_conflict("unsupported operation", txid=record.logical_tx_id)

        row_hash = row_digest(
            record.table,
            record.row_key,
            payload,
            version,
            record.logical_tx_id,
            tombstone,
        )
        connection.execute(
            """
            INSERT INTO data_rows(
                logical_table, row_key, payload_json, version,
                source_tx, tombstone, row_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(logical_table, row_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                version = excluded.version,
                source_tx = excluded.source_tx,
                tombstone = excluded.tombstone,
                row_hash = excluded.row_hash
            """,
            (
                record.table,
                record.row_key,
                canonical_json(payload),
                version,
                record.logical_tx_id,
                int(tombstone),
                row_hash,
            ),
        )
        connection.execute(
            """
            INSERT INTO applied_txs(txid, tx_hash, checkpoint_gen, applied_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            (
                record.logical_tx_id,
                record.tx_hash,
                record.checkpoint_gen,
                applied_at_ms,
            ),
        )
        self.clear_in_flight(connection, record.logical_tx_id)
        watermark, _ = self.repair_watermark(connection)
        self.set_meta(connection, "logical_clock_ms", str(applied_at_ms))
        summaries = self.summaries_snapshot(connection)
        self.set_meta_json(connection, "state_table_summaries", summaries)
        self.set_meta(connection, "state_rows_digest", sha256_json(summaries))
        return {
            "status": "applied",
            "txid": record.logical_tx_id,
            "version": version,
            "watermark": watermark,
        }

    def status(self, *, pending_tx_ids: Sequence[int] | None = None) -> dict[str, Any]:
        with self.connection(writable=False) as connection:
            pointer = self.get_pointer(connection)
            applied_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM applied_txs").fetchone()["count"]
            )
            watermark = self.get_meta_int(connection, "applied_watermark")
            summaries = self.summaries_snapshot(connection)
            in_flight = [
                {
                    "started_at_ms": int(row["started_at_ms"]),
                    "tx_hash": str(row["tx_hash"]),
                    "txid": int(row["txid"]),
                }
                for row in self.in_flight(connection)
            ]
            logical_clock = self.get_meta_int(connection, "logical_clock_ms")
            last_checkpoint_clock = self.get_meta_int(connection, "last_checkpoint_clock_ms")
        public_pointer = None
        if pointer is not None:
            public_pointer = {
                "checkpoint_gen": pointer["checkpoint_gen"],
                "commit_ts_ms": pointer["commit_ts_ms"],
                "conversion": pointer["conversion"],
                "format_version": pointer["format_version"],
                "logical_clock_ms": pointer["logical_clock_ms"],
                "manifest_version": pointer["manifest_version"],
                "parent_checkpoint_gen": pointer["parent_checkpoint_gen"],
                "pointer_version": pointer["pointer_version"],
                "replay_generation": pointer["replay_generation"],
            }
        result = {
            "applied_tx_count": applied_count,
            "applied_watermark": watermark,
            "checkpoint": public_pointer,
            "in_flight": in_flight,
            "last_checkpoint_clock_ms": last_checkpoint_clock,
            "logical_clock_ms": logical_clock,
            "state_table_summaries": summaries,
            "schema_version": SCHEMA_VERSION,
        }
        if pending_tx_ids is not None:
            result["pending_tx_ids"] = sorted(int(txid) for txid in pending_tx_ids)
        return result

    def snapshot_pair(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        with self.connection(writable=False) as connection:
            rows = self.rows_snapshot(connection)
            pointer_row = self.get_pointer(connection)
        if pointer_row is not None:
            pointer = {
                key: pointer_row[key]
                for key in (
                    "pointer_version",
                    "checkpoint_gen",
                    "manifest_version",
                    "replay_generation",
                    "applied_watermark",
                    "chain_hash",
                    "logical_clock_ms",
                    "commit_ts_ms",
                    "manifest_hash",
                    "format_version",
                    "parent_checkpoint_gen",
                    "table_summaries",
                    "conversion",
                )
            }
        else:
            pointer = None
        return rows, pointer


def compare_row_snapshots(
    left: Mapping[str, Sequence[Mapping[str, Any]]],
    right: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for table in sorted(set(left) | set(right)):
        left_rows = {str(row["key"]): row for row in left.get(table, [])}
        right_rows = {str(row["key"]): row for row in right.get(table, [])}
        for key in sorted(set(left_rows) | set(right_rows)):
            lrow = left_rows.get(key)
            rrow = right_rows.get(key)
            if lrow == rrow:
                continue
            candidates = [
                int(row["source_tx"])
                for row in (lrow, rrow)
                if row is not None and "source_tx" in row
            ]
            differences.append(
                {
                    "key": key,
                    "left": lrow,
                    "right": rrow,
                    "table": table,
                    "txid": max(candidates, default=0),
                }
            )
    return differences
