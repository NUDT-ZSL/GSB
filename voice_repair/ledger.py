from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import numpy as np

from .errors import EXIT_FORCED_TEST_TERMINATION, LedgerError


SCHEMA_VERSION = "1"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def payload_digest(payload: bytes | np.ndarray) -> str:
    data = payload.tobytes(order="C") if isinstance(payload, np.ndarray) else payload
    return hashlib.sha256(data).hexdigest()


def encode_float64(values: np.ndarray) -> bytes:
    return np.ascontiguousarray(values, dtype="<f8").tobytes(order="C")


def decode_float64(payload: bytes) -> np.ndarray:
    if len(payload) % 8 != 0:
        raise LedgerError("E_LEDGER_PAYLOAD", "block payload length is not a multiple of 8")
    return np.frombuffer(payload, dtype="<f8").astype(np.float64, copy=True)


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    is_resume: bool
    previous_block_rows: int
    started_at: str


@dataclass(frozen=True)
class BlockRecord:
    recording_id: str
    stage: int
    block_index: int
    start_sample: int
    end_sample: int
    sample_count: int
    content_sha256: str
    logical_cost_ns: int
    run_id: str


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._hook_lock = threading.Lock()
        self._crash_after_commits: int | None = None
        self._post_commit_count = 0
        self._connection = sqlite3.connect(
            str(path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=30000")
            self._connection.execute("PRAGMA wal_autocheckpoint=64")
            self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS block_ledger (
                recording_id TEXT NOT NULL,
                stage INTEGER NOT NULL,
                block_index INTEGER NOT NULL,
                start_sample INTEGER NOT NULL,
                end_sample INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                payload BLOB NOT NULL,
                logical_cost_ns INTEGER NOT NULL,
                created_run_id TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                PRIMARY KEY (recording_id, stage, block_index)
            );
            CREATE INDEX IF NOT EXISTS idx_block_ledger_run
                ON block_ledger(created_run_id);
            CREATE TABLE IF NOT EXISTS stage_metrics (
                recording_id TEXT NOT NULL,
                stage INTEGER NOT NULL,
                metric_key TEXT NOT NULL,
                metric_value TEXT NOT NULL,
                created_run_id TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                PRIMARY KEY (recording_id, stage, metric_key)
            );
            CREATE TABLE IF NOT EXISTS run_sessions (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                mode TEXT NOT NULL,
                workers INTEGER NOT NULL,
                parallelism INTEGER NOT NULL,
                is_resume INTEGER NOT NULL,
                previous_block_rows INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS interruptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                signal_number INTEGER,
                detail TEXT NOT NULL,
                created_utc TEXT NOT NULL
            );
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        row = self._connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None or row["value"] != SCHEMA_VERSION:
            raise LedgerError("E_LEDGER_SCHEMA", "unsupported ledger schema version")

    def initialize_task(self, task_sha256: str, input_digests: dict[str, str]) -> None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._set_or_check_meta("task_sha256", task_sha256)
                for recording_id in sorted(input_digests):
                    self._set_or_check_meta(
                        f"input_sha256:{recording_id}",
                        input_digests[recording_id],
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _set_or_check_meta(self, key: str, value: str) -> None:
        row = self._connection.execute(
            "SELECT value FROM schema_meta WHERE key=?",
            (key,),
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                (key, value),
            )
        elif row["value"] != value:
            code = "E_LEDGER_TASK_MISMATCH" if key == "task_sha256" else "E_LEDGER_INPUT_MISMATCH"
            raise LedgerError(
                code,
                f"ledger metadata {key} differs; use a fresh state database",
            )

    def begin_run(self, *, mode: str, workers: int, parallelism: int) -> RunInfo:
        with self._lock:
            previous = int(
                self._connection.execute("SELECT COUNT(*) FROM block_ledger").fetchone()[0]
            )
            run_id = uuid.uuid4().hex
            started_at = utc_now()
            is_resume = previous > 0
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO run_sessions(
                        run_id, started_at, finished_at, mode, workers, parallelism,
                        is_resume, previous_block_rows, status
                    ) VALUES(?, ?, NULL, ?, ?, ?, ?, ?, 'running')
                    """,
                    (
                        run_id,
                        started_at,
                        mode,
                        workers,
                        parallelism,
                        1 if is_resume else 0,
                        previous,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            return RunInfo(
                run_id=run_id,
                is_resume=is_resume,
                previous_block_rows=previous,
                started_at=started_at,
            )

    def finish_run(self, run_id: str, status: str) -> None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "UPDATE run_sessions SET finished_at=?, status=? WHERE run_id=?",
                    (utc_now(), status, run_id),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def record_interruption(
        self,
        run_id: str,
        *,
        kind: str,
        signal_number: int | None,
        detail: str,
    ) -> None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO interruptions(
                        run_id, kind, signal_number, detail, created_utc
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (run_id, kind, signal_number, detail, utc_now()),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def total_blocks(self) -> int:
        with self._lock:
            return int(
                self._connection.execute("SELECT COUNT(*) FROM block_ledger").fetchone()[0]
            )

    def block_indices(self, recording_id: str, stage: int) -> set[int]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT block_index FROM block_ledger
                WHERE recording_id=? AND stage=?
                ORDER BY block_index
                """,
                (recording_id, stage),
            ).fetchall()
            return {int(row["block_index"]) for row in rows}

    def block_counts(self, recording_id: str, stage: int) -> int:
        with self._lock:
            return int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM block_ledger
                    WHERE recording_id=? AND stage=?
                    """,
                    (recording_id, stage),
                ).fetchone()[0]
            )

    def count_run_blocks(self, run_id: str) -> int:
        with self._lock:
            return int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM block_ledger WHERE created_run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )

    def count_run_blocks_by_stage(self, run_id: str, recording_id: str) -> dict[int, int]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT stage, COUNT(*) AS count FROM block_ledger
                WHERE created_run_id=? AND recording_id=?
                GROUP BY stage
                """,
                (run_id, recording_id),
            ).fetchall()
            return {int(row["stage"]): int(row["count"]) for row in rows}

    def put_block(
        self,
        *,
        recording_id: str,
        stage: int,
        block_index: int,
        start_sample: int,
        end_sample: int,
        values: np.ndarray,
        logical_cost_ns: int,
        run_id: str,
    ) -> bool:
        payload = encode_float64(values)
        digest = payload_digest(payload)
        sample_count = int(values.size)
        if sample_count != end_sample - start_sample:
            raise LedgerError(
                "E_LEDGER_RANGE",
                f"block payload has {sample_count} samples for [{start_sample}, {end_sample})",
            )
        inserted = False
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO block_ledger(
                        recording_id, stage, block_index, start_sample, end_sample,
                        sample_count, content_sha256, payload, logical_cost_ns,
                        created_run_id, created_utc
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recording_id,
                        stage,
                        block_index,
                        start_sample,
                        end_sample,
                        sample_count,
                        digest,
                        sqlite3.Binary(payload),
                        logical_cost_ns,
                        run_id,
                        utc_now(),
                    ),
                )
                inserted = cursor.rowcount == 1
                if not inserted:
                    existing = self._connection.execute(
                        """
                        SELECT start_sample, end_sample, sample_count,
                               content_sha256, payload
                        FROM block_ledger
                        WHERE recording_id=? AND stage=? AND block_index=?
                        """,
                        (recording_id, stage, block_index),
                    ).fetchone()
                    if existing is None:
                        raise LedgerError("E_LEDGER_WRITE", "insert was ignored but row is absent")
                    if (
                        int(existing["start_sample"]) != start_sample
                        or int(existing["end_sample"]) != end_sample
                        or int(existing["sample_count"]) != sample_count
                        or str(existing["content_sha256"]) != digest
                        or payload_digest(bytes(existing["payload"])) != digest
                    ):
                        raise LedgerError(
                            "E_LEDGER_DIGEST_CONFLICT",
                            "attempted to replace a committed block with different content",
                        )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

        if inserted and self._crash_after_commits is not None:
            with self._hook_lock:
                self._post_commit_count += 1
                should_exit = self._post_commit_count >= self._crash_after_commits
            if should_exit:
                os._exit(EXIT_FORCED_TEST_TERMINATION)
        return inserted

    def fetch_block(self, recording_id: str, stage: int, block_index: int) -> np.ndarray:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT start_sample, end_sample, sample_count, content_sha256, payload
                FROM block_ledger
                WHERE recording_id=? AND stage=? AND block_index=?
                """,
                (recording_id, stage, block_index),
            ).fetchone()
        if row is None:
            raise LedgerError(
                "E_LEDGER_MISSING",
                f"missing block {recording_id}/{stage}/{block_index}",
            )
        payload = bytes(row["payload"])
        if payload_digest(payload) != str(row["content_sha256"]):
            raise LedgerError(
                "E_LEDGER_CORRUPT",
                f"digest mismatch for block {recording_id}/{stage}/{block_index}",
            )
        values = decode_float64(payload)
        expected = int(row["sample_count"])
        if values.size != expected or expected != int(row["end_sample"]) - int(row["start_sample"]):
            raise LedgerError(
                "E_LEDGER_CORRUPT",
                f"range mismatch for block {recording_id}/{stage}/{block_index}",
            )
        return values

    def block_record(self, recording_id: str, stage: int, block_index: int) -> BlockRecord:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT recording_id, stage, block_index, start_sample, end_sample,
                       sample_count, content_sha256, logical_cost_ns, created_run_id
                FROM block_ledger
                WHERE recording_id=? AND stage=? AND block_index=?
                """,
                (recording_id, stage, block_index),
            ).fetchone()
        if row is None:
            raise LedgerError(
                "E_LEDGER_MISSING",
                f"missing block {recording_id}/{stage}/{block_index}",
            )
        return BlockRecord(
            recording_id=str(row["recording_id"]),
            stage=int(row["stage"]),
            block_index=int(row["block_index"]),
            start_sample=int(row["start_sample"]),
            end_sample=int(row["end_sample"]),
            sample_count=int(row["sample_count"]),
            content_sha256=str(row["content_sha256"]),
            logical_cost_ns=int(row["logical_cost_ns"]),
            run_id=str(row["created_run_id"]),
        )

    def all_block_records(self, recording_id: str, stage: int) -> list[BlockRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT recording_id, stage, block_index, start_sample, end_sample,
                       sample_count, content_sha256, logical_cost_ns, created_run_id
                FROM block_ledger
                WHERE recording_id=? AND stage=?
                ORDER BY block_index
                """,
                (recording_id, stage),
            ).fetchall()
        return [
            BlockRecord(
                recording_id=str(row["recording_id"]),
                stage=int(row["stage"]),
                block_index=int(row["block_index"]),
                start_sample=int(row["start_sample"]),
                end_sample=int(row["end_sample"]),
                sample_count=int(row["sample_count"]),
                content_sha256=str(row["content_sha256"]),
                logical_cost_ns=int(row["logical_cost_ns"]),
                run_id=str(row["created_run_id"]),
            )
            for row in rows
        ]

    def put_stage_metric(
        self,
        *,
        recording_id: str,
        stage: int,
        metric_key: str,
        value: Any,
        run_id: str,
    ) -> None:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO stage_metrics(
                        recording_id, stage, metric_key, metric_value,
                        created_run_id, created_utc
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (recording_id, stage, metric_key, encoded, run_id, utc_now()),
                )
                row = self._connection.execute(
                    """
                    SELECT metric_value FROM stage_metrics
                    WHERE recording_id=? AND stage=? AND metric_key=?
                    """,
                    (recording_id, stage, metric_key),
                ).fetchone()
                if row is None or row["metric_value"] != encoded:
                    raise LedgerError(
                        "E_LEDGER_METRIC_CONFLICT",
                        f"stage metric conflict for {recording_id}/{stage}/{metric_key}",
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def get_stage_metric(
        self,
        recording_id: str,
        stage: int,
        metric_key: str,
    ) -> Any | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT metric_value FROM stage_metrics
                WHERE recording_id=? AND stage=? AND metric_key=?
                """,
                (recording_id, stage, metric_key),
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["metric_value"]))

    def set_crash_after_commits(self, count: int | None) -> None:
        if count is not None and count <= 0:
            raise ValueError("crash_after_commits must be positive")
        self._crash_after_commits = count

    def checkpoint(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
