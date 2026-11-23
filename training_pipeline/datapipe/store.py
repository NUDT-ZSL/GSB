"""SQLite-backed durable state with cross-process file locking.

Every state transition happens inside an ``IMMEDIATE`` transaction so readers
only ever observe fully committed rounds.  A separate advisory file lock
(fcntl on POSIX, msvcrt on Windows) serialises scheduler instances that need to
coordinate shard ownership, while SQLite's own locking protects the tables.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .errors import E_BATCH_DIGEST_CONFLICT, E_CORRUPT_STATE, E_LOCK_TIMEOUT, fail

try:  # POSIX
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # Windows
    _HAVE_FCNTL = False
    import msvcrt  # type: ignore


SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    round_index INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    clock_now_ms INTEGER NOT NULL,
    opened_at_ms INTEGER NOT NULL,
    committed_at_ms INTEGER,
    snapshot_ptr TEXT
);
CREATE TABLE IF NOT EXISTS committed_shards (
    round_index INTEGER NOT NULL,
    dataset TEXT NOT NULL,
    shard_index INTEGER NOT NULL,
    algebra INTEGER NOT NULL,
    cache_identity TEXT NOT NULL,
    digest TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    PRIMARY KEY (dataset, shard_index, algebra)
);
CREATE TABLE IF NOT EXISTS cache_entries (
    dataset TEXT NOT NULL,
    shard_index INTEGER NOT NULL,
    algebra INTEGER NOT NULL,
    cache_identity TEXT NOT NULL,
    digest TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    last_used_clock INTEGER NOT NULL,
    PRIMARY KEY (dataset, shard_index, algebra)
);
CREATE TABLE IF NOT EXISTS batches (
    epoch INTEGER NOT NULL,
    batch_index INTEGER NOT NULL,
    digest TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    sources TEXT NOT NULL,
    PRIMARY KEY (epoch, batch_index)
);
CREATE TABLE IF NOT EXISTS memory_ledger (
    round_index INTEGER NOT NULL,
    dataset TEXT NOT NULL,
    shard_index INTEGER NOT NULL,
    algebra INTEGER NOT NULL,
    bytes_delta INTEGER NOT NULL,
    PRIMARY KEY (round_index, dataset, shard_index, algebra)
);
CREATE TABLE IF NOT EXISTS fault_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_index INTEGER,
    stage TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    code TEXT NOT NULL,
    diagnostics TEXT NOT NULL
);
"""


class FileLock:
    """Advisory exclusive lock with a bounded busy wait, portable to Windows."""

    def __init__(self, path: str, timeout_seconds: float = 10.0):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle: Optional[Any] = None

    def acquire(self) -> None:
        self._handle = open(self.path, "a+")
        if _HAVE_FCNTL:
            import time

            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise fail(E_LOCK_TIMEOUT, "could not acquire file lock", path=self.path)
                    time.sleep(0.05)
        else:  # Windows
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if _HAVE_FCNTL:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            else:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # Scheduler worker threads share this connection; an RLock serialises
        # access while SQLite's own locking coordinates separate processes.
        self._thread_lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, timeout=60, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.transaction():
            for statement in [stmt.strip() for stmt in SCHEMA.split(";") if stmt.strip()]:
                self.conn.execute(statement)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        with self._thread_lock:
            import time

            deadline = time.monotonic() + 60.0
            while True:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # --- kv state -----------------------------------------------------------
    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._thread_lock:
            row = self.conn.execute("SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set_kv(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, sort_keys=True)),
        )

    # --- rounds -------------------------------------------------------------
    def open_round(self, round_index: int, clock_now_ms: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rounds(round_index, status, clock_now_ms, opened_at_ms) "
                "VALUES(?, 'open', ?, ?)",
                (round_index, clock_now_ms, clock_now_ms),
            )

    def get_open_round(self) -> Optional[sqlite3.Row]:
        with self._thread_lock:
            return self.conn.execute(
                "SELECT * FROM rounds WHERE status='open' ORDER BY round_index"
            ).fetchone()

    def commit_round(self, round_index: int, clock_now_ms: int, snapshot_ptr: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE rounds SET status='committed', committed_at_ms=?, clock_now_ms=?, snapshot_ptr=? "
                "WHERE round_index=?",
                (clock_now_ms, clock_now_ms, snapshot_ptr, round_index),
            )
            # Replace the current scheduler snapshot pointer atomically.
            conn.execute(
                "INSERT INTO kv_state(key, value) VALUES('current_snapshot', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({"round_index": round_index, "snapshot_ptr": snapshot_ptr}, sort_keys=True),),
            )

    def committed_rounds(self) -> List[sqlite3.Row]:
        with self._thread_lock:
            return list(self.conn.execute(
                "SELECT * FROM rounds WHERE status='committed' ORDER BY round_index"))

    # --- shards -------------------------------------------------------------
    def shard_committed(self, dataset: str, shard_index: int, algebra: int) -> Optional[sqlite3.Row]:
        with self._thread_lock:
            return self.conn.execute(
                "SELECT * FROM committed_shards WHERE dataset=? AND shard_index=? AND algebra=?",
                (dataset, shard_index, algebra),
            ).fetchone()

    def commit_shard(
        self,
        round_index: int,
        dataset: str,
        shard_index: int,
        algebra: int,
        cache_identity: str,
        digest: str,
        byte_size: int,
        outcome: str,
    ) -> bool:
        """Idempotent shard commit.

        Returns ``True`` when this call inserted the row, ``False`` when an
        identical commit already existed (replay / duplicate submission).
        """
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT digest, byte_size FROM committed_shards WHERE dataset=? AND shard_index=? AND algebra=?",
                (dataset, shard_index, algebra),
            ).fetchone()
            if existing is not None:
                if existing["digest"] != digest or existing["byte_size"] != byte_size:
                    raise fail(
                        E_CORRUPT_STATE,
                        "idempotent shard commit conflicts with stored digest/size",
                        dataset=dataset,
                        shard_index=shard_index,
                        algebra=algebra,
                    )
                return False
            conn.execute(
                "INSERT INTO committed_shards(round_index, dataset, shard_index, algebra, cache_identity, "
                "digest, byte_size, outcome) VALUES(?,?,?,?,?,?,?,?)",
                (round_index, dataset, shard_index, algebra, cache_identity, digest, byte_size, outcome),
            )
            return True

    def committed_shard_rows(self) -> List[sqlite3.Row]:
        with self._thread_lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM committed_shards ORDER BY dataset, shard_index, algebra"
                )
            )

    # --- cache --------------------------------------------------------------
    def upsert_cache_entry(
        self,
        dataset: str,
        shard_index: int,
        algebra: int,
        cache_identity: str,
        digest: str,
        byte_size: int,
        last_used_clock: int,
    ) -> bool:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT cache_identity FROM cache_entries WHERE dataset=? AND shard_index=? AND algebra=?",
                (dataset, shard_index, algebra),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE cache_entries SET last_used_clock=? WHERE dataset=? AND shard_index=? AND algebra=?",
                    (last_used_clock, dataset, shard_index, algebra),
                )
                return False
            conn.execute(
                "INSERT INTO cache_entries(dataset, shard_index, algebra, cache_identity, digest, byte_size, "
                "last_used_clock) VALUES(?,?,?,?,?,?,?)",
                (dataset, shard_index, algebra, cache_identity, digest, byte_size, last_used_clock),
            )
            return True

    def cache_entry(self, dataset: str, shard_index: int, algebra: int) -> Optional[sqlite3.Row]:
        with self._thread_lock:
            return self.conn.execute(
                "SELECT * FROM cache_entries WHERE dataset=? AND shard_index=? AND algebra=?",
                (dataset, shard_index, algebra),
            ).fetchone()

    def cache_rows(self) -> List[sqlite3.Row]:
        with self._thread_lock:
            return list(self.conn.execute(
                "SELECT * FROM cache_entries ORDER BY dataset, shard_index, algebra"))

    # --- memory ledger ------------------------------------------------------
    def add_memory_charge(
        self, round_index: int, dataset: str, shard_index: int, algebra: int, bytes_delta: int
    ) -> bool:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM memory_ledger WHERE round_index=? AND dataset=? AND shard_index=? AND algebra=?",
                (round_index, dataset, shard_index, algebra),
            ).fetchone()
            if existing is not None:
                return False
            conn.execute(
                "INSERT INTO memory_ledger(round_index, dataset, shard_index, algebra, bytes_delta) "
                "VALUES(?,?,?,?,?)",
                (round_index, dataset, shard_index, algebra, bytes_delta),
            )
            return True

    def total_memory_charged(self) -> int:
        with self._thread_lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(bytes_delta), 0) AS total FROM memory_ledger"
            ).fetchone()
        return int(row["total"])

    def memory_rows(self) -> List[sqlite3.Row]:
        with self._thread_lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM memory_ledger ORDER BY round_index, dataset, shard_index, algebra"
                )
            )

    # --- batches ------------------------------------------------------------
    def record_batch(
        self, epoch: int, batch_index: int, digest: str, sample_count: int, sources: List[str]
    ) -> bool:
        """Idempotent batch commit with digest-conflict detection."""
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT digest FROM batches WHERE epoch=? AND batch_index=?", (epoch, batch_index)
            ).fetchone()
            if existing is not None:
                if existing["digest"] != digest:
                    raise fail(
                        E_BATCH_DIGEST_CONFLICT,
                        "batch digest conflict for same epoch/batch",
                        epoch=epoch,
                        batch_index=batch_index,
                        stored=existing["digest"],
                        incoming=digest,
                    )
                return False
            conn.execute(
                "INSERT INTO batches(epoch, batch_index, digest, sample_count, sources) VALUES(?,?,?,?,?)",
                (epoch, batch_index, digest, sample_count, json.dumps(sources, sort_keys=True)),
            )
            return True

    def batch_rows(self) -> List[sqlite3.Row]:
        with self._thread_lock:
            return list(self.conn.execute("SELECT * FROM batches ORDER BY epoch, batch_index"))

    # --- faults -------------------------------------------------------------
    def log_fault(
        self, round_index: Optional[int], stage: str, target: str, kind: str, code: str, diagnostics: dict
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO fault_log(round_index, stage, target, kind, code, diagnostics) "
                "VALUES(?,?,?,?,?,?)",
                (round_index, stage, target, kind, code, json.dumps(diagnostics, sort_keys=True)),
            )

    def fault_rows(self) -> List[sqlite3.Row]:
        with self._thread_lock:
            return list(self.conn.execute("SELECT * FROM fault_log ORDER BY id"))

    # --- watermarks ---------------------------------------------------------
    def set_watermark(self, name: str, value: Any) -> None:
        self.set_kv(f"watermark:{name}", value)

    def get_watermark(self, name: str, default: Any = None) -> Any:
        return self.get_kv(f"watermark:{name}", default)
