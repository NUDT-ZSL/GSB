"""SQLite-backed ledger for node states, stage watermarks and the logical clock,
plus atomic result-file writes guarded by a cross-process file lock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .errors import E_CLOCK_SKEW, E_STATE_CONFLICT, PipelineError
from .operators import canonical_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    name TEXT PRIMARY KEY,
    stage INTEGER NOT NULL,
    state TEXT NOT NULL,
    memory_mib INTEGER NOT NULL,
    digest TEXT,
    output_json TEXT,
    trace_json TEXT,
    reason TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    updated_clock INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stage_watermarks (
    stage INTEGER PRIMARY KEY,
    clock INTEGER NOT NULL,
    committed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    abandoned INTEGER NOT NULL,
    memory_peak_mib INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

STATE_PENDING = "PENDING"
STATE_RUNNING = "RUNNING"
STATE_COMMITTED = "COMMITTED"
STATE_FAILED = "FAILED"
STATE_ABANDONED = "ABANDONED"


class FileLock:
    """Cross-process advisory lock via O_CREAT|O_EXCL (works on Windows and POSIX)."""

    def __init__(self, path: str | Path, timeout_s: float = 30.0):
        self.path = Path(path)
        self.timeout_s = timeout_s
        self._fd: int | None = None

    def __enter__(self):
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise PipelineError(E_STATE_CONFLICT, "timed out acquiring file lock", lock=str(self.path))
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class Ledger:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)

    def close(self):
        self._conn.close()

    def _read(self, fn):
        # All connection access is serialized: SQLite connections must not be
        # used concurrently from multiple threads, and readers must never
        # observe another thread's in-flight transaction on this connection.
        with self._lock:
            return fn()

    def _write(self, fn):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn()
                self._conn.execute("COMMIT")
                return result
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def init_run(self, pipeline) -> None:
        def fn():
            for node in pipeline.nodes.values():
                self._conn.execute(
                    "INSERT OR IGNORE INTO nodes(name, stage, state, memory_mib) VALUES (?,?,?,?)",
                    (node.name, node.stage, STATE_PENDING, node.memory_mib),
                )
            self._conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('clock', '0')")

        self._write(fn)

    def get_node(self, name: str) -> dict | None:
        def fn():
            row = self._conn.execute(
                "SELECT name, stage, state, memory_mib, digest, output_json, trace_json, reason, attempt, updated_clock"
                " FROM nodes WHERE name=?",
                (name,),
            ).fetchone()
            if row is None:
                return None
            return {
                "name": row[0], "stage": row[1], "state": row[2], "memory_mib": row[3],
                "digest": row[4], "output_json": row[5], "trace_json": row[6],
                "reason": row[7], "attempt": row[8], "updated_clock": row[9],
            }

        return self._read(fn)

    def all_nodes(self) -> list[dict]:
        def fn():
            rows = self._conn.execute(
                "SELECT name, stage, state, memory_mib, digest, reason, attempt FROM nodes ORDER BY stage, name"
            ).fetchall()
            return [
                {"name": r[0], "stage": r[1], "state": r[2], "memory_mib": r[3],
                 "digest": r[4], "reason": r[5], "attempt": r[6]}
                for r in rows
            ]

        return self._read(fn)

    def committed_outputs(self) -> dict[str, dict]:
        def fn():
            rows = self._conn.execute(
                "SELECT name, digest, output_json, trace_json FROM nodes WHERE state=? ORDER BY name",
                (STATE_COMMITTED,),
            ).fetchall()
            return {
                r[0]: {"digest": r[1], "records": json.loads(r[2]), "trace": json.loads(r[3])}
                for r in rows
            }

        return self._read(fn)

    def get_clock(self) -> int:
        def fn():
            row = self._conn.execute("SELECT value FROM meta WHERE key='clock'").fetchone()
            return int(row[0]) if row else 0

        return self._read(fn)

    def watermarks(self) -> list[dict]:
        def fn():
            rows = self._conn.execute(
                "SELECT stage, clock, committed, failed, abandoned, memory_peak_mib"
                " FROM stage_watermarks ORDER BY stage"
            ).fetchall()
            return [
                {"stage": r[0], "clock": r[1], "committed": r[2], "failed": r[3],
                 "abandoned": r[4], "memory_peak_mib": r[5]}
                for r in rows
            ]

        return self._read(fn)

    def mark_running(self, name: str) -> int:
        def fn():
            row = self._conn.execute("SELECT state, attempt FROM nodes WHERE name=?", (name,)).fetchone()
            if row is None:
                raise PipelineError(E_STATE_CONFLICT, "unknown node", node=name)
            if row[0] == STATE_COMMITTED:
                return -1  # already done; caller must skip
            attempt = row[1] + 1
            self._conn.execute(
                "UPDATE nodes SET state=?, attempt=?, reason=NULL WHERE name=?",
                (STATE_RUNNING, attempt, name),
            )
            return attempt

        return self._write(fn)

    def commit_node(self, name: str, digest: str, records: list[dict], trace: dict, clock: int) -> bool:
        """Idempotent commit: committing the same digest twice is a no-op."""
        payload = canonical_json(records).decode("utf-8")
        trace_payload = canonical_json(trace).decode("utf-8")

        def fn():
            row = self._conn.execute("SELECT state, digest FROM nodes WHERE name=?", (name,)).fetchone()
            if row is None:
                raise PipelineError(E_STATE_CONFLICT, "unknown node", node=name)
            if row[0] == STATE_COMMITTED:
                if row[1] == digest:
                    return False  # idempotent no-op
                raise PipelineError(
                    E_STATE_CONFLICT, "conflicting re-commit of committed node", node=name,
                    committed_digest=row[1], new_digest=digest,
                )
            self._conn.execute(
                "UPDATE nodes SET state=?, digest=?, output_json=?, trace_json=?, reason=NULL, updated_clock=?"
                " WHERE name=?",
                (STATE_COMMITTED, digest, payload, trace_payload, clock, name),
            )
            return True

        return self._write(fn)

    def mark_terminal(self, name: str, state: str, reason: str, clock: int) -> None:
        assert state in (STATE_FAILED, STATE_ABANDONED)

        def fn():
            self._conn.execute(
                "UPDATE nodes SET state=?, reason=?, updated_clock=? WHERE name=? AND state != ?",
                (state, reason, clock, name, STATE_COMMITTED),
            )

        self._write(fn)

    def reset_running_to_pending(self) -> list[str]:
        """Recovery: nodes left RUNNING by a crash are half-committed; requeue them."""
        def fn():
            rows = self._conn.execute(
                "SELECT name FROM nodes WHERE state=?", (STATE_RUNNING,)
            ).fetchall()
            self._conn.execute(
                "UPDATE nodes SET state=?, reason='requeued after crash' WHERE state=?",
                (STATE_PENDING, STATE_RUNNING),
            )
            return [r[0] for r in rows]

        return self._write(fn)

    def advance_clock(self, stage: int, window_ms: int, tolerance_ms: int, expected_base: int,
                      committed: int, failed: int, abandoned: int, memory_peak: int) -> int:
        """Advance the logical clock for a finished stage, rejecting rollbacks."""
        def fn():
            current = self.get_clock()
            if abs(current - expected_base) > tolerance_ms:
                raise PipelineError(
                    E_CLOCK_SKEW,
                    "persisted logical clock diverges from expected stage base beyond tolerance",
                    stage=stage, persisted_clock=current, expected_base=expected_base,
                    tolerance_ms=tolerance_ms,
                )
            new_clock = current + window_ms
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('clock', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_clock),),
            )
            self._conn.execute(
                "INSERT INTO stage_watermarks(stage, clock, committed, failed, abandoned, memory_peak_mib)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(stage) DO UPDATE SET clock=excluded.clock, committed=excluded.committed,"
                " failed=excluded.failed, abandoned=excluded.abandoned,"
                " memory_peak_mib=excluded.memory_peak_mib",
                (stage, new_clock, committed, failed, abandoned, memory_peak),
            )
            return new_clock

        return self._write(fn)


def write_results_atomic(out_path: str | Path, committed: dict[str, dict]) -> None:
    """Rewrite the result file atomically so readers never see partial output."""
    out = Path(out_path)
    payload = {
        "nodes": {
            name: {"digest": entry["digest"], "records": entry["records"], "trace": entry["trace"]}
            for name, entry in sorted(committed.items())
        }
    }
    tmp = out.with_suffix(out.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)
