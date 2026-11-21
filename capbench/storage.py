from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .decision import judge_gradient
from .errors import WorkbenchError
from .scenario import BatchPlan, GradientSpec, Scenario, SLO
from .workload import count_window_requests

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def summary_hash(summary: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(summary).encode("utf-8")).hexdigest()


_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


class FileLock:
    """Small cross-platform advisory lock with a process-local mutex."""

    def __init__(self, path: str | Path, timeout_s: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self._thread_lock = _process_lock(self.path)
        self._file = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock.acquire()
        try:
            self._file = self.path.open("a+b")
            self._file.seek(0, os.SEEK_END)
            if self._file.tell() == 0:
                self._file.write(b"\0")
                self._file.flush()
            deadline = time.monotonic() + self.timeout_s
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    elif msvcrt is not None:  # pragma: no cover - Windows
                        self._file.seek(0)
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                    return self
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise WorkbenchError(
                            "E_LOCK_TIMEOUT",
                            f"timed out acquiring file lock {self.path}",
                        )
                    time.sleep(0.01)
        except Exception:
            self._file.close()
            self._file = None
            self._thread_lock.release()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._file is not None
        try:
            if fcntl is not None:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._file.close()
            self._file = None
            self._thread_lock.release()


def _pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class ClaimResult:
    status: str
    detail: dict[str, Any]


class Store:
    """SQLite-backed run store.

    Each operation opens a fresh connection so worker threads never share a
    SQLite connection object.  All mutating operations hold a run-scoped file
    lock and an immediate SQLite transaction.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            uri = f"file:{self.path.resolve()}?mode=ro"
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
        else:
            conn = sqlite3.connect(
                str(self.path),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _locked(self, timeout_s: float = 30.0) -> FileLock:
        return FileLock(self.lock_path, timeout_s=timeout_s)

    def ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        run_id TEXT PRIMARY KEY,
                        schema_version INTEGER NOT NULL,
                        scenario_hash TEXT NOT NULL,
                        scenario_json TEXT NOT NULL,
                        seed INTEGER NOT NULL,
                        global_budget_ms INTEGER NOT NULL,
                        logical_clock_ms INTEGER NOT NULL DEFAULT 0,
                        request_seq_watermark INTEGER NOT NULL DEFAULT 0,
                        budget_exhausted INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        created_utc TEXT NOT NULL,
                        updated_utc TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gradients (
                        run_id TEXT NOT NULL,
                        tenant TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        gradient_index INTEGER NOT NULL,
                        concurrency INTEGER NOT NULL,
                        duration_ms INTEGER NOT NULL,
                        pace_ms INTEGER NOT NULL,
                        window_ms INTEGER NOT NULL,
                        request_timeout_ms INTEGER NOT NULL,
                        total_windows INTEGER NOT NULL,
                        committed_windows INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL,
                        verdict TEXT,
                        outcome TEXT,
                        termination_json TEXT,
                        evidence_json TEXT,
                        error_text TEXT,
                        logical_start_ms INTEGER NOT NULL,
                        logical_end_ms INTEGER NOT NULL,
                        PRIMARY KEY (run_id, tenant, gradient_index),
                        UNIQUE (run_id, batch_id, gradient_index),
                        FOREIGN KEY (run_id) REFERENCES meta(run_id)
                    );
                    CREATE TABLE IF NOT EXISTS windows (
                        run_id TEXT NOT NULL,
                        tenant TEXT NOT NULL,
                        gradient_index INTEGER NOT NULL,
                        window_index INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        planned_requests INTEGER NOT NULL,
                        request_seq_start INTEGER NOT NULL,
                        request_seq_end INTEGER NOT NULL,
                        clock_start_ms INTEGER NOT NULL,
                        clock_end_ms INTEGER NOT NULL,
                        summary_json TEXT,
                        summary_sha256 TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        owner_pid INTEGER,
                        owner_token TEXT,
                        last_error TEXT,
                        committed_at_utc TEXT,
                        PRIMARY KEY (run_id, tenant, gradient_index, window_index),
                        FOREIGN KEY (run_id, tenant, gradient_index)
                            REFERENCES gradients(run_id, tenant, gradient_index)
                    );
                    CREATE TABLE IF NOT EXISTS budget_entries (
                        run_id TEXT NOT NULL,
                        tenant TEXT NOT NULL,
                        gradient_index INTEGER NOT NULL,
                        window_index INTEGER NOT NULL,
                        reserved_ms INTEGER NOT NULL,
                        claimed_at_utc TEXT NOT NULL,
                        PRIMARY KEY (run_id, tenant, gradient_index, window_index),
                        FOREIGN KEY (run_id) REFERENCES meta(run_id)
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        tenant TEXT,
                        gradient_index INTEGER,
                        window_index INTEGER,
                        detail_json TEXT,
                        created_utc TEXT NOT NULL,
                        FOREIGN KEY (run_id) REFERENCES meta(run_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_windows_state
                        ON windows(run_id, state);
                    CREATE INDEX IF NOT EXISTS idx_events_run
                        ON events(run_id, id);
                    """
                )
            finally:
                conn.close()

    def initialize(self, scenario: Scenario, *, run_id: str, seed: int) -> bool:
        """Create all fixed units and request-sequence ranges atomically.

        Returns ``True`` when a new run row was created, ``False`` for an
        identical existing run.  Conflicting scenario/seed data is rejected
        without changing existing rows.
        """

        self.ensure_schema()
        scenario_json = scenario.canonical_json()
        expected_hash = scenario.scenario_hash(seed)
        created = False
        with self._locked():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM meta WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["scenario_hash"] != expected_hash
                        or int(existing["seed"]) != int(seed)
                    ):
                        raise WorkbenchError(
                            "E_RUN_CONFLICT",
                            "run_id already exists with a different scenario or seed",
                            {
                                "run_id": run_id,
                                "existing_hash": existing["scenario_hash"],
                                "requested_hash": expected_hash,
                            },
                        )
                    conn.execute("COMMIT")
                    return False

                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO meta(
                        run_id, schema_version, scenario_hash, scenario_json, seed,
                        global_budget_ms, logical_clock_ms, request_seq_watermark,
                        budget_exhausted, status, created_utc, updated_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 'initialized', ?, ?)
                    """,
                    (
                        run_id,
                        SCHEMA_VERSION,
                        expected_hash,
                        scenario_json,
                        seed,
                        scenario.global_time_budget_ms,
                        now,
                        now,
                    ),
                )

                next_seq = 1
                for plan in sorted(scenario.plans, key=lambda item: item.tenant):
                    for gradient in plan.gradients:
                        spans = [
                            gradient.window_span(index)
                            for index in range(gradient.window_count)
                        ]
                        conn.execute(
                            """
                            INSERT INTO gradients(
                                run_id, tenant, batch_id, gradient_index, concurrency,
                                duration_ms, pace_ms, window_ms, request_timeout_ms,
                                total_windows, committed_windows, state, verdict,
                                outcome, termination_json, evidence_json, error_text,
                                logical_start_ms, logical_end_ms
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending',
                                      NULL, NULL, NULL, NULL, NULL, ?, ?)
                            """,
                            (
                                run_id,
                                plan.tenant,
                                plan.batch_id,
                                gradient.index,
                                gradient.concurrency,
                                gradient.duration_ms,
                                gradient.pace_ms,
                                gradient.window_ms,
                                gradient.request_timeout_ms,
                                gradient.window_count,
                                gradient.logical_start_ms,
                                gradient.logical_start_ms + gradient.duration_ms,
                            ),
                        )
                        for window_index, (clock_start, clock_end) in enumerate(spans):
                            request_count = count_window_requests(
                                gradient,
                                window_index,
                            )
                            seq_start = next_seq
                            seq_end = seq_start + request_count - 1
                            next_seq = seq_end + 1
                            conn.execute(
                                """
                                INSERT INTO windows(
                                    run_id, tenant, gradient_index, window_index,
                                    state, planned_requests, request_seq_start,
                                    request_seq_end, clock_start_ms, clock_end_ms
                                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                                """,
                                (
                                    run_id,
                                    plan.tenant,
                                    gradient.index,
                                    window_index,
                                    request_count,
                                    seq_start,
                                    seq_end,
                                    clock_start,
                                    clock_end,
                                ),
                            )
                conn.execute(
                    """
                    INSERT INTO events(
                        run_id, event_type, detail_json, created_utc
                    ) VALUES (?, 'run_initialized', ?, ?)
                    """,
                    (
                        run_id,
                        canonical_json(
                            {
                                "scenario_hash": expected_hash,
                                "seed": seed,
                                "planned_requests": next_seq - 1,
                            }
                        ),
                        now,
                    ),
                )
                conn.execute("COMMIT")
                created = True
                return created
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def load_scenario(self, run_id: str) -> Scenario:
        from .scenario import parse_scenario

        with self._connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT scenario_json FROM meta WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise WorkbenchError("E_RUN_NOT_FOUND", f"run not found: {run_id}")
        return parse_scenario(json.loads(row["scenario_json"]))

    def meta(self, run_id: str) -> dict[str, Any]:
        with self._connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM meta WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise WorkbenchError("E_RUN_NOT_FOUND", f"run not found: {run_id}")
        return dict(row)

    def run_ids(self) -> list[str]:
        if not self.exists:
            return []
        with self._connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT run_id FROM meta ORDER BY created_utc, run_id"
            ).fetchall()
        return [row["run_id"] for row in rows]

    def recover_orphans(self, run_id: str) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        with self._locked():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT * FROM windows
                    WHERE run_id = ? AND state = 'running'
                    ORDER BY tenant, gradient_index, window_index
                    """,
                    (run_id,),
                ).fetchall()
                now = utc_now()
                for row in rows:
                    owner = row["owner_pid"]
                    if owner == os.getpid() or not _pid_alive(owner):
                        conn.execute(
                            """
                            UPDATE windows
                            SET state = 'pending', owner_pid = NULL,
                                owner_token = NULL, last_error = NULL
                            WHERE run_id = ? AND tenant = ?
                              AND gradient_index = ? AND window_index = ?
                              AND state = 'running'
                            """,
                            (
                                run_id,
                                row["tenant"],
                                row["gradient_index"],
                                row["window_index"],
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO events(
                                run_id, event_type, tenant, gradient_index,
                                window_index, detail_json, created_utc
                            ) VALUES (?, 'orphaned_window_recovered', ?, ?, ?, ?, ?)
                            """,
                            (
                                run_id,
                                row["tenant"],
                                row["gradient_index"],
                                row["window_index"],
                                canonical_json(
                                    {
                                        "previous_owner_pid": owner,
                                        "committed_watermark_preserved": True,
                                    }
                                ),
                                now,
                            ),
                        )
                        recovered.append(
                            {
                                "tenant": row["tenant"],
                                "gradient_index": row["gradient_index"],
                                "window_index": row["window_index"],
                                "previous_owner_pid": owner,
                            }
                        )
                conn.execute("COMMIT")
                return recovered
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def reserve_and_claim(
        self,
        *,
        run_id: str,
        tenant: str,
        gradient_index: int,
        window_index: int,
        owner_token: str,
        max_parallel_gradients: int,
    ) -> ClaimResult:
        with self._locked():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                meta = conn.execute(
                    "SELECT * FROM meta WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if meta is None:
                    raise WorkbenchError("E_RUN_NOT_FOUND", f"run not found: {run_id}")
                window = conn.execute(
                    """
                    SELECT * FROM windows
                    WHERE run_id = ? AND tenant = ?
                      AND gradient_index = ? AND window_index = ?
                    """,
                    (run_id, tenant, gradient_index, window_index),
                ).fetchone()
                if window is None:
                    raise WorkbenchError(
                        "E_UNIT_NOT_FOUND",
                        "window unit not found",
                        {
                            "tenant": tenant,
                            "gradient_index": gradient_index,
                            "window_index": window_index,
                        },
                    )
                gradient_row = conn.execute(
                    """
                    SELECT * FROM gradients
                    WHERE run_id = ? AND tenant = ? AND gradient_index = ?
                    """,
                    (run_id, tenant, gradient_index),
                ).fetchone()
                if gradient_row is None:
                    raise WorkbenchError("E_UNIT_NOT_FOUND", "gradient unit not found")
                if gradient_row["state"] in (
                    "terminated",
                    "skipped_after_knee",
                    "budget_abandoned",
                    "failed",
                ):
                    conn.execute("COMMIT")
                    return ClaimResult(
                        "terminal",
                        {"gradient_state": gradient_row["state"]},
                    )
                if window["state"] == "committed":
                    conn.execute("COMMIT")
                    return ClaimResult("already_committed", {})
                if window["state"] == "running":
                    conn.execute("COMMIT")
                    return ClaimResult(
                        "busy",
                        {
                            "owner_pid": window["owner_pid"],
                            "owner_token": window["owner_token"],
                        },
                    )
                if window["state"] == "failed":
                    conn.execute("COMMIT")
                    return ClaimResult("failed", {"reason": "unit already failed"})
                if window["state"] not in ("pending",):
                    conn.execute("COMMIT")
                    return ClaimResult("terminal", {"state": window["state"]})

                active_gradients = int(
                    conn.execute(
                        """
                        SELECT COUNT(DISTINCT gradient_index) AS count
                        FROM windows
                        WHERE run_id = ? AND tenant = ? AND state = 'running'
                          AND gradient_index != ?
                        """,
                        (run_id, tenant, gradient_index),
                    ).fetchone()["count"]
                )
                if active_gradients >= max_parallel_gradients:
                    conn.execute("COMMIT")
                    return ClaimResult(
                        "tenant_budget_busy",
                        {
                            "reason": "per_tenant_parallel_gradient_limit",
                            "active_gradients": active_gradients,
                            "limit": max_parallel_gradients,
                        },
                    )

                earlier = conn.execute(
                    """
                    SELECT MIN(window_index) AS first_pending
                    FROM windows
                    WHERE run_id = ? AND tenant = ? AND gradient_index = ?
                      AND state IN ('pending', 'running')
                    """,
                    (run_id, tenant, gradient_index),
                ).fetchone()
                if (
                    earlier is not None
                    and earlier["first_pending"] is not None
                    and int(earlier["first_pending"]) != window_index
                ):
                    conn.execute("COMMIT")
                    return ClaimResult(
                        "out_of_order",
                        {"expected_window": int(earlier["first_pending"])},
                    )

                budget_row = conn.execute(
                    """
                    SELECT * FROM budget_entries
                    WHERE run_id = ? AND tenant = ?
                      AND gradient_index = ? AND window_index = ?
                    """,
                    (run_id, tenant, gradient_index, window_index),
                ).fetchone()
                span_ms = int(window["clock_end_ms"]) - int(window["clock_start_ms"])
                if budget_row is None:
                    used = int(
                        conn.execute(
                            "SELECT COALESCE(SUM(reserved_ms), 0) AS used "
                            "FROM budget_entries WHERE run_id = ?",
                            (run_id,),
                        ).fetchone()["used"]
                    )
                    budget = int(meta["global_budget_ms"])
                    if used + span_ms > budget:
                        detail = {
                            "reason": "insufficient_global_time_budget",
                            "used_ms": used,
                            "required_ms": span_ms,
                            "budget_ms": budget,
                            "remaining_ms": max(0, budget - used),
                        }
                        conn.execute(
                            """
                            UPDATE windows
                            SET state = 'not_started_budget', last_error = ?
                            WHERE run_id = ? AND tenant = ? AND state = 'pending'
                            """,
                            (canonical_json(detail), run_id, tenant),
                        )
                        conn.execute(
                            """
                            UPDATE gradients
                            SET state = 'budget_abandoned',
                                outcome = CASE
                                    WHEN committed_windows > 0
                                        THEN 'partial_budget_abandoned'
                                    ELSE 'not_started_budget'
                                END,
                                termination_json = ?
                            WHERE run_id = ? AND tenant = ?
                              AND state IN ('pending', 'running')
                            """,
                            (canonical_json(detail), run_id, tenant),
                        )
                        conn.execute(
                            "UPDATE meta SET budget_exhausted = 1, updated_utc = ? "
                            "WHERE run_id = ?",
                            (utc_now(), run_id),
                        )
                        conn.execute(
                            """
                            INSERT INTO events(
                                run_id, event_type, tenant, gradient_index,
                                window_index, detail_json, created_utc
                            ) VALUES (?, 'unit_budget_unstarted', ?, ?, ?, ?, ?)
                            """,
                            (
                                run_id,
                                tenant,
                                gradient_index,
                                window_index,
                                canonical_json(detail),
                                utc_now(),
                            ),
                        )
                        conn.execute("COMMIT")
                        return ClaimResult("budget_unstarted", detail)
                    conn.execute(
                        """
                        INSERT INTO budget_entries(
                            run_id, tenant, gradient_index, window_index,
                            reserved_ms, claimed_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            tenant,
                            gradient_index,
                            window_index,
                            span_ms,
                            utc_now(),
                        ),
                    )

                now = utc_now()
                conn.execute(
                    """
                    UPDATE windows
                    SET state = 'running', attempt_count = attempt_count + 1,
                        owner_pid = ?, owner_token = ?, last_error = NULL
                    WHERE run_id = ? AND tenant = ?
                      AND gradient_index = ? AND window_index = ?
                      AND state = 'pending'
                    """,
                    (
                        os.getpid(),
                        owner_token,
                        run_id,
                        tenant,
                        gradient_index,
                        window_index,
                    ),
                )
                conn.execute(
                    """
                    UPDATE gradients
                    SET state = CASE WHEN state = 'pending' THEN 'running' ELSE state END
                    WHERE run_id = ? AND tenant = ? AND gradient_index = ?
                    """,
                    (run_id, tenant, gradient_index),
                )
                conn.execute(
                    "UPDATE meta SET status = 'running', updated_utc = ? WHERE run_id = ?",
                    (now, run_id),
                )
                conn.execute("COMMIT")
                return ClaimResult(
                    "claimed",
                    {
                        "clock_start_ms": int(window["clock_start_ms"]),
                        "clock_end_ms": int(window["clock_end_ms"]),
                        "planned_requests": int(window["planned_requests"]),
                        "request_seq_start": int(window["request_seq_start"]),
                        "request_seq_end": int(window["request_seq_end"]),
                    },
                )
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def commit_window(
        self,
        *,
        run_id: str,
        tenant: str,
        gradient_index: int,
        window_index: int,
        summary: dict[str, Any],
        owner_token: str,
        slo: SLO,
        allow_early_termination: bool,
    ) -> dict[str, Any]:
        digest = summary_hash(summary)
        with self._locked():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                window = conn.execute(
                    """
                    SELECT * FROM windows
                    WHERE run_id = ? AND tenant = ?
                      AND gradient_index = ? AND window_index = ?
                    """,
                    (run_id, tenant, gradient_index, window_index),
                ).fetchone()
                if window is None:
                    raise WorkbenchError("E_UNIT_NOT_FOUND", "window unit not found")
                if window["state"] == "committed":
                    if window["summary_sha256"] != digest:
                        raise WorkbenchError(
                            "E_IDEMPOTENCY_CONFLICT",
                            "window was already committed with a different summary",
                            {
                                "tenant": tenant,
                                "gradient_index": gradient_index,
                                "window_index": window_index,
                                "existing_sha256": window["summary_sha256"],
                                "new_sha256": digest,
                            },
                        )
                    conn.execute("COMMIT")
                    return {
                        "status": "already_committed",
                        "tenant": tenant,
                        "gradient_index": gradient_index,
                        "window_index": window_index,
                    }
                if window["state"] != "running":
                    raise WorkbenchError(
                        "E_UNIT_STATE",
                        "only a running window can be committed",
                        {
                            "state": window["state"],
                            "tenant": tenant,
                            "gradient_index": gradient_index,
                            "window_index": window_index,
                        },
                    )
                if (
                    window["owner_pid"] not in (None, os.getpid())
                    or window["owner_token"] != owner_token
                ):
                    raise WorkbenchError(
                        "E_LEASE_CONFLICT",
                        "window ownership changed before commit",
                        {
                            "owner_pid": window["owner_pid"],
                            "owner_token": window["owner_token"],
                        },
                    )

                now = utc_now()
                conn.execute(
                    """
                    UPDATE windows
                    SET state = 'committed', summary_json = ?,
                        summary_sha256 = ?, committed_at_utc = ?
                    WHERE run_id = ? AND tenant = ?
                      AND gradient_index = ? AND window_index = ?
                    """,
                    (
                        canonical_json(summary),
                        digest,
                        now,
                        run_id,
                        tenant,
                        gradient_index,
                        window_index,
                    ),
                )
                conn.execute(
                    """
                    UPDATE meta
                    SET request_seq_watermark = MAX(request_seq_watermark, ?),
                        logical_clock_ms = MAX(logical_clock_ms, ?),
                        updated_utc = ?
                    WHERE run_id = ?
                    """,
                    (
                        int(summary["request_seq_end"]),
                        int(summary["window_end_ms"]),
                        now,
                        run_id,
                    ),
                )
                committed_rows = conn.execute(
                    """
                    SELECT summary_json FROM windows
                    WHERE run_id = ? AND tenant = ? AND gradient_index = ?
                      AND state = 'committed'
                    ORDER BY window_index
                    """,
                    (run_id, tenant, gradient_index),
                ).fetchall()
                summaries = [json.loads(row["summary_json"]) for row in committed_rows]
                gradient = conn.execute(
                    """
                    SELECT * FROM gradients
                    WHERE run_id = ? AND tenant = ? AND gradient_index = ?
                    """,
                    (run_id, tenant, gradient_index),
                ).fetchone()
                decision = judge_gradient(
                    summaries=summaries,
                    slo=slo,
                    total_windows=int(gradient["total_windows"]),
                    allow_early_termination=allow_early_termination,
                )

                state = "running"
                outcome = None
                termination = None
                if decision["terminated_early"]:
                    state = "terminated"
                    outcome = "early_terminated"
                    termination = {
                        "trigger": decision["trigger"],
                        "reason": "gradient_abort_with_early_termination",
                    }
                elif decision["verdict"] in ("pass", "degraded_pass"):
                    state = "completed"
                    outcome = decision["verdict"]
                elif decision["verdict"] == "abort":
                    state = "completed"
                    outcome = "abort"

                conn.execute(
                    """
                    UPDATE gradients
                    SET state = ?, verdict = ?, outcome = ?,
                        committed_windows = ?, termination_json = ?,
                        evidence_json = ?, logical_end_ms = ?
                    WHERE run_id = ? AND tenant = ? AND gradient_index = ?
                    """,
                    (
                        state,
                        decision["verdict"],
                        outcome,
                        len(summaries),
                        canonical_json(termination) if termination else None,
                        canonical_json(decision["evidence"]),
                        int(summary["window_end_ms"]),
                        run_id,
                        tenant,
                        gradient_index,
                    ),
                )
                if decision["terminated_early"]:
                    conn.execute(
                        """
                        UPDATE windows
                        SET state = 'skipped_early_termination',
                            last_error = ?
                        WHERE run_id = ? AND tenant = ? AND gradient_index = ?
                          AND state = 'pending'
                        """,
                        (
                            canonical_json(termination),
                            run_id,
                            tenant,
                            gradient_index,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE windows
                        SET state = 'skipped_after_knee',
                            last_error = ?
                        WHERE run_id = ? AND tenant = ? AND gradient_index > ?
                          AND state = 'pending'
                        """,
                        (
                            canonical_json(
                                {
                                    "trigger_gradient": gradient_index,
                                    "trigger_window": window_index,
                                    "reason": "tenant capacity knee reached",
                                }
                            ),
                            run_id,
                            tenant,
                            gradient_index,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE gradients
                        SET state = 'skipped_after_knee',
                            outcome = 'not_started_after_knee',
                            termination_json = ?
                        WHERE run_id = ? AND tenant = ? AND gradient_index > ?
                          AND state = 'pending'
                        """,
                        (
                            canonical_json(
                                {
                                    "trigger_gradient": gradient_index,
                                    "trigger_window": window_index,
                                    "reason": "tenant capacity knee reached",
                                }
                            ),
                            run_id,
                            tenant,
                            gradient_index,
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO events(
                        run_id, event_type, tenant, gradient_index, window_index,
                        detail_json, created_utc
                    ) VALUES (?, 'window_committed', ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        tenant,
                        gradient_index,
                        window_index,
                        canonical_json(
                            {
                                "summary_sha256": digest,
                                "verdict": decision["verdict"],
                                "gradient_state": state,
                                "request_seq_start": summary["request_seq_start"],
                                "request_seq_end": summary["request_seq_end"],
                            }
                        ),
                        now,
                    ),
                )
                conn.execute("COMMIT")
                return {
                    "status": "committed",
                    "summary_sha256": digest,
                    "gradient_state": state,
                    "gradient_verdict": decision["verdict"],
                    "terminated_early": decision["terminated_early"],
                    "trigger": decision["trigger"],
                }
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def fail_window(
        self,
        *,
        run_id: str,
        tenant: str,
        gradient_index: int,
        window_index: int,
        owner_token: str,
        error: str,
    ) -> None:
        detail = {"reason": "unit_execution_failed", "error": error[:1000]}
        with self._locked():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                window = conn.execute(
                    """
                    SELECT * FROM windows
                    WHERE run_id = ? AND tenant = ?
                      AND gradient_index = ? AND window_index = ?
                    """,
                    (run_id, tenant, gradient_index, window_index),
                ).fetchone()
                if window is None or window["state"] != "running":
                    conn.execute("COMMIT")
                    return
                if window["owner_token"] != owner_token:
                    conn.execute("COMMIT")
                    return
                now = utc_now()
                conn.execute(
                    """
                    UPDATE windows
                    SET state = 'failed', last_error = ?
                    WHERE run_id = ? AND tenant = ?
                      AND gradient_index = ? AND window_index = ?
                    """,
                    (
                        canonical_json(detail),
                        run_id,
                        tenant,
                        gradient_index,
                        window_index,
                    ),
                )
                conn.execute(
                    """
                    UPDATE gradients
                    SET state = 'failed', outcome = 'started_failed',
                        error_text = ?
                    WHERE run_id = ? AND tenant = ? AND gradient_index = ?
                    """,
                    (error[:1000], run_id, tenant, gradient_index),
                )
                conn.execute(
                    """
                    INSERT INTO events(
                        run_id, event_type, tenant, gradient_index, window_index,
                        detail_json, created_utc
                    ) VALUES (?, 'unit_started_failed', ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        tenant,
                        gradient_index,
                        window_index,
                        canonical_json(detail),
                        now,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def mark_remaining_budget_unstarted(
        self,
        run_id: str,
        *,
        reason: str,
    ) -> int:
        with self._locked():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT tenant, gradient_index, window_index
                    FROM windows
                    WHERE run_id = ? AND state = 'pending'
                    ORDER BY tenant, gradient_index, window_index
                    """,
                    (run_id,),
                ).fetchall()
                detail = canonical_json(
                    {"reason": reason, "classification": "budget_unstarted"}
                )
                now = utc_now()
                for row in rows:
                    conn.execute(
                        """
                        UPDATE windows
                        SET state = 'not_started_budget', last_error = ?
                        WHERE run_id = ? AND tenant = ?
                          AND gradient_index = ? AND window_index = ?
                        """,
                        (
                            detail,
                            run_id,
                            row["tenant"],
                            row["gradient_index"],
                            row["window_index"],
                        ),
                    )
                if rows:
                    conn.execute(
                        "UPDATE meta SET budget_exhausted = 1, updated_utc = ? "
                        "WHERE run_id = ?",
                        (now, run_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO events(
                            run_id, event_type, detail_json, created_utc
                        ) VALUES (?, 'remaining_units_budget_unstarted', ?, ?)
                        """,
                        (
                            run_id,
                            canonical_json(
                                {"reason": reason, "unit_count": len(rows)}
                            ),
                            now,
                        ),
                    )
                conn.execute("COMMIT")
                return len(rows)
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def mark_remaining_not_started(
        self,
        run_id: str,
        *,
        reason: str,
    ) -> int:
        """Classify units that had budget headroom but were not started."""

        with self._locked():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT tenant, gradient_index, window_index
                    FROM windows
                    WHERE run_id = ? AND state = 'pending'
                    ORDER BY tenant, gradient_index, window_index
                    """,
                    (run_id,),
                ).fetchall()
                detail = canonical_json(
                    {
                        "reason": reason,
                        "classification": "not_started_within_budget",
                    }
                )
                now = utc_now()
                for row in rows:
                    conn.execute(
                        """
                        UPDATE windows
                        SET state = 'not_started_incomplete', last_error = ?
                        WHERE run_id = ? AND tenant = ?
                          AND gradient_index = ? AND window_index = ?
                        """,
                        (
                            detail,
                            run_id,
                            row["tenant"],
                            row["gradient_index"],
                            row["window_index"],
                        ),
                    )
                conn.execute(
                    """
                    UPDATE gradients
                    SET state = 'incomplete',
                        outcome = CASE
                            WHEN committed_windows > 0
                                THEN 'partial_not_started'
                            ELSE 'not_started_within_budget'
                        END
                    WHERE run_id = ? AND state IN ('pending', 'running')
                    """,
                    (run_id,),
                )
                if rows:
                    conn.execute(
                        """
                        INSERT INTO events(
                            run_id, event_type, detail_json, created_utc
                        ) VALUES (?, 'remaining_units_not_started', ?, ?)
                        """,
                        (
                            run_id,
                            canonical_json(
                                {"reason": reason, "unit_count": len(rows)}
                            ),
                            now,
                        ),
                    )
                conn.execute("COMMIT")
                return len(rows)
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def finalize_run(self, run_id: str) -> str:
        with self._locked():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                counts = {
                    row["state"]: int(row["count"])
                    for row in conn.execute(
                        """
                        SELECT state, COUNT(*) AS count FROM windows
                        WHERE run_id = ? GROUP BY state
                        """,
                        (run_id,),
                    ).fetchall()
                }
                if counts.get("running", 0) or counts.get("pending", 0):
                    status = "running"
                elif counts.get("failed", 0):
                    status = "incomplete_failed"
                elif counts.get("not_started_budget", 0):
                    status = "completed_budget_exhausted"
                elif counts.get("not_started_incomplete", 0):
                    status = "incomplete"
                else:
                    status = "completed"
                conn.execute(
                    """
                    UPDATE gradients
                    SET state = 'budget_abandoned',
                        outcome = CASE
                            WHEN committed_windows > 0
                                THEN 'partial_budget_abandoned'
                            ELSE 'not_started_budget'
                        END
                    WHERE run_id = ? AND state IN ('pending', 'running')
                      AND EXISTS (
                          SELECT 1 FROM windows w
                          WHERE w.run_id = gradients.run_id
                            AND w.tenant = gradients.tenant
                            AND w.gradient_index = gradients.gradient_index
                            AND w.state = 'not_started_budget'
                      )
                    """,
                    (run_id,),
                )
                conn.execute(
                    """
                    UPDATE gradients
                    SET state = 'incomplete',
                        outcome = CASE
                            WHEN committed_windows > 0
                                THEN 'partial_not_started'
                            ELSE 'not_started_within_budget'
                        END
                    WHERE run_id = ? AND state IN ('pending', 'running')
                    """,
                    (run_id,),
                )
                conn.execute(
                    "UPDATE meta SET status = ?, updated_utc = ? WHERE run_id = ?",
                    (status, utc_now(), run_id),
                )
                conn.execute("COMMIT")
                return status
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def gradients(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM gradients WHERE run_id = ?
                ORDER BY tenant, gradient_index
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def windows(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM windows WHERE run_id = ?
                ORDER BY tenant, gradient_index, window_index
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def budget_used(self, run_id: str) -> int:
        with self._connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(reserved_ms), 0) AS used "
                "FROM budget_entries WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["used"])

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_live_workers(self, run_id: str) -> bool:
        with self._connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT owner_pid FROM windows WHERE run_id = ? AND state = 'running'",
                (run_id,),
            ).fetchall()
        return any(
            int(row["owner_pid"]) != os.getpid() and _pid_alive(row["owner_pid"])
            for row in rows
            if row["owner_pid"] is not None
        )

    def new_owner_token(self) -> str:
        return f"{os.getpid()}-{uuid.uuid4().hex}"

