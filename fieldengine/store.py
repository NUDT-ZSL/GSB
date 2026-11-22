from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .errors import ErrorCode, EngineError, fail
from .model import Experiment, Injection, canonical_json
from .planning import RunPlan, ShardPlan


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _proc_start_ticks(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].strip().split()
    if len(fields) < 20:
        return None
    return fields[19]


def _owner_alive(owner: str | None) -> bool:
    if not owner:
        return False
    parts = owner.split(":")
    if len(parts) < 4:
        return False
    try:
        pid = int(parts[1])
    except ValueError:
        return False
    ticks = parts[2]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    current = _proc_start_ticks(pid)
    return current is None or current == ticks


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._thread_lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._schema()
        self.worker_id = (
            f"{socket.gethostname()}:{os.getpid()}:{_proc_start_ticks(os.getpid()) or '0'}:"
            f"{uuid.uuid4().hex[:8]}"
        )

    @classmethod
    def open_readonly(cls, path: str | Path) -> "ReadonlyStore":
        return ReadonlyStore(path)

    def close(self) -> None:
        self.conn.close()

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run (
                run_id INTEGER PRIMARY KEY CHECK (run_id = 1),
                status TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                query_digest TEXT NOT NULL,
                config_digest TEXT NOT NULL,
                plan_digest TEXT NOT NULL,
                mode TEXT NOT NULL,
                mode_decision_json TEXT NOT NULL,
                logical_clock INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error_code TEXT,
                error_message TEXT
            );
            CREATE TABLE IF NOT EXISTS shard (
                shard_id TEXT PRIMARY KEY,
                batch_index INTEGER NOT NULL,
                shard_index INTEGER NOT NULL,
                shard_count INTEGER NOT NULL,
                row_start INTEGER NOT NULL,
                row_end INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                shard_digest TEXT NOT NULL,
                content_digest TEXT NOT NULL,
                estimated_bytes INTEGER NOT NULL,
                estimate_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                interrupted_attempts INTEGER NOT NULL DEFAULT 0,
                result_digest TEXT,
                result_blob BLOB,
                error_code TEXT,
                error_message TEXT,
                started_at REAL,
                committed_at REAL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS shard_order_idx
                ON shard(batch_index, shard_index, shard_id);
            CREATE TABLE IF NOT EXISTS lease (
                shard_id TEXT PRIMARY KEY REFERENCES shard(shard_id),
                owner TEXT NOT NULL,
                reserved_bytes INTEGER NOT NULL,
                acquired_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operator (
                operator_id TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                status TEXT NOT NULL,
                expected_shards INTEGER NOT NULL,
                committed_shards INTEGER NOT NULL DEFAULT 0,
                result_digest TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS window_plan (
                window_id TEXT PRIMARY KEY,
                ordinal INTEGER NOT NULL,
                partition_key TEXT NOT NULL,
                partition_json TEXT NOT NULL,
                start_index INTEGER NOT NULL,
                end_index INTEGER NOT NULL,
                expected_shards_json TEXT NOT NULL,
                batch_indices_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS window_buffer (
                window_id TEXT NOT NULL,
                shard_id TEXT NOT NULL,
                rows INTEGER NOT NULL,
                partial_digest TEXT NOT NULL,
                partial_blob BLOB NOT NULL,
                committed_at REAL NOT NULL,
                PRIMARY KEY (window_id, shard_id)
            );
            CREATE TABLE IF NOT EXISTS result (
                kind TEXT NOT NULL,
                result_key TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                PRIMARY KEY (kind, result_key)
            );
            CREATE TABLE IF NOT EXISTS injection_applied (
                injection_id TEXT PRIMARY KEY,
                shard_id TEXT,
                applied_at REAL NOT NULL,
                logical_clock INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS budget_event (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                shard_id TEXT,
                owner TEXT,
                bytes INTEGER NOT NULL,
                reason TEXT,
                logical_clock INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watermark (
                batch_index INTEGER PRIMARY KEY,
                committed_shards INTEGER NOT NULL,
                total_shards INTEGER NOT NULL,
                last_logical_clock INTEGER NOT NULL
            );
            """
        )

    @contextmanager
    def _critical(self, write: bool = True) -> Iterator[None]:
        with self._thread_lock:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._critical(write=True):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _run_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "status": row["status"],
            "input_digest": row["input_digest"],
            "query_digest": row["query_digest"],
            "config_digest": row["config_digest"],
            "plan_digest": row["plan_digest"],
            "mode": row["mode"],
            "mode_decision": json.loads(row["mode_decision_json"]),
            "logical_clock": int(row["logical_clock"]),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
        }

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def initialize(self, experiment: Experiment, plan: RunPlan) -> dict[str, Any]:
        injections = {
            injection.id: {
                "id": injection.id,
                "action": injection.action,
                "trigger": injection.trigger,
                "milliseconds": injection.milliseconds,
                "once": injection.once,
                "error_code": injection.error_code,
            }
            for injection in experiment.injections
        }
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM run WHERE run_id = 1").fetchone()
            if existing is not None:
                if (
                    existing["input_digest"] != experiment.digest
                    or existing["plan_digest"] != plan.plan_digest
                ):
                    raise fail(
                        ErrorCode.STATE_MISMATCH,
                        "existing state belongs to a different experiment or plan",
                        stored_input_digest=existing["input_digest"],
                        input_digest=experiment.digest,
                        stored_plan_digest=existing["plan_digest"],
                        plan_digest=plan.plan_digest,
                        db_path=str(self.path),
                    )
            else:
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO run(
                        run_id, status, input_digest, query_digest, config_digest, plan_digest,
                        mode, mode_decision_json, logical_clock, created_at, updated_at
                    ) VALUES(1, 'INITIALIZED', ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        experiment.digest,
                        experiment.query_digest,
                        experiment.config_digest,
                        plan.plan_digest,
                        plan.mode,
                        canonical_json(plan.mode_decision),
                        now,
                        now,
                    ),
                )
                self._set_meta("schema_version", "1")
                self._set_meta("experiment_path", str(experiment.experiment_path))
                self._set_meta("injection_json", canonical_json(injections))
                self._set_meta("peak_reserved_bytes", "0")
                for shard in plan.shard_plans:
                    conn.execute(
                        """
                        INSERT INTO shard(
                            shard_id, batch_index, shard_index, shard_count, row_start, row_end,
                            row_count, shard_digest, content_digest, estimated_bytes, estimate_json,
                            status, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                        """,
                        (
                            shard.shard_id,
                            shard.batch_index,
                            shard.shard_index,
                            shard.shard_count,
                            shard.row_start,
                            shard.row_end,
                            shard.row_count,
                            shard.shard_digest,
                            shard.content_digest,
                            shard.estimated_bytes,
                            canonical_json(shard.estimate),
                            now,
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO operator(
                        operator_id, version, status, expected_shards, committed_shards, updated_at
                    ) VALUES('group_agg', ?, 'RUNNING', ?, 0, ?)
                    """,
                    (experiment.query_digest, plan.total_shards, now),
                )
                conn.execute(
                    """
                    INSERT INTO operator(
                        operator_id, version, status, expected_shards, committed_shards, updated_at
                    ) VALUES('window_agg', ?, 'RUNNING', ?, 0, ?)
                    """,
                    (experiment.query_digest + ":window", plan.total_shards, now),
                )
                for window in plan.windows:
                    conn.execute(
                        """
                        INSERT INTO window_plan(
                            window_id, ordinal, partition_key, partition_json, start_index, end_index,
                            expected_shards_json, batch_indices_json
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            window.window_id,
                            window.ordinal,
                            window.partition_key,
                            canonical_json(window.partition_display),
                            window.start,
                            window.end,
                            canonical_json(list(window.expected_shards)),
                            canonical_json(list(window.batch_indices)),
                        ),
                    )
                per_batch: dict[int, int] = {}
                for shard in plan.shard_plans:
                    per_batch[shard.batch_index] = per_batch.get(shard.batch_index, 0) + 1
                for batch_index in sorted(per_batch):
                    conn.execute(
                        "INSERT INTO watermark(batch_index, committed_shards, total_shards, last_logical_clock) "
                        "VALUES(?, 0, ?, 0)",
                        (batch_index, per_batch[batch_index]),
                    )
        return self.run_summary()

    def verify_identity(self, experiment: Experiment, plan: RunPlan) -> None:
        with self._critical(write=False):
            row = self.conn.execute("SELECT * FROM run WHERE run_id = 1").fetchone()
            if row is None:
                raise fail(ErrorCode.STATE_NOT_FOUND, "state database has no run", db_path=str(self.path))
            if row["input_digest"] != experiment.digest or row["plan_digest"] != plan.plan_digest:
                raise fail(
                    ErrorCode.STATE_MISMATCH,
                    "state database does not match the supplied experiment",
                    stored_input_digest=row["input_digest"],
                    input_digest=experiment.digest,
                    stored_plan_digest=row["plan_digest"],
                    plan_digest=plan.plan_digest,
                )

    def _bump_clock(self, amount: int) -> int:
        row = self.conn.execute("SELECT logical_clock FROM run WHERE run_id = 1").fetchone()
        clock = int(row["logical_clock"]) + int(amount)
        self.conn.execute("UPDATE run SET logical_clock = ?, updated_at = ? WHERE run_id = 1", (clock, time.time()))
        return clock

    def _record_budget(
        self,
        action: str,
        shard_id: str | None,
        owner: str | None,
        bytes_value: int,
        reason: str | None,
        clock: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO budget_event(action, shard_id, owner, bytes, reason, logical_clock, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (action, shard_id, owner, bytes_value, reason, clock, time.time()),
        )

    def _reclaim_dead_locked(self) -> list[str]:
        reclaimed: list[str] = []
        now = time.time()
        leases = self.conn.execute("SELECT * FROM lease ORDER BY shard_id").fetchall()
        for lease in leases:
            if _owner_alive(lease["owner"]):
                continue
            shard_id = str(lease["shard_id"])
            self.conn.execute("DELETE FROM lease WHERE shard_id = ?", (shard_id,))
            updated = self.conn.execute(
                "UPDATE shard SET status = 'PENDING', interrupted_attempts = interrupted_attempts + 1, "
                "updated_at = ? WHERE shard_id = ? AND status = 'RUNNING'",
                (now, shard_id),
            )
            if updated.rowcount:
                self._bump_clock(1)
                reclaimed.append(shard_id)
        running_without_lease = self.conn.execute(
            "SELECT s.shard_id FROM shard s LEFT JOIN lease l ON l.shard_id = s.shard_id "
            "WHERE s.status = 'RUNNING' AND l.shard_id IS NULL"
        ).fetchall()
        for row in running_without_lease:
            self.conn.execute(
                "UPDATE shard SET status = 'PENDING', interrupted_attempts = interrupted_attempts + 1, "
                "updated_at = ? WHERE shard_id = ?",
                (now, row["shard_id"]),
            )
            self._bump_clock(1)
            reclaimed.append(str(row["shard_id"]))
        return sorted(set(reclaimed))

    def recover_orphans(self) -> list[str]:
        with self._transaction():
            return self._reclaim_dead_locked()

    def _current_usage(self) -> tuple[int, int, int]:
        rows = self.conn.execute("SELECT shard_id, reserved_bytes FROM lease").fetchall()
        return len(rows), sum(int(row["reserved_bytes"]) for row in rows), max(
            [int(row["reserved_bytes"]) for row in rows], default=0
        )

    def _abandon_locked(self, shard_id: str, reason: str, detail: str, clock_step: int) -> None:
        now = time.time()
        clock = self._bump_clock(clock_step)
        self.conn.execute("DELETE FROM lease WHERE shard_id = ?", (shard_id,))
        self.conn.execute(
            "UPDATE shard SET status = 'ABANDONED', error_code = ?, error_message = ?, updated_at = ? "
            "WHERE shard_id = ? AND status = 'PENDING'",
            (ErrorCode.BUDGET_REJECTED, detail, now, shard_id),
        )
        self._record_budget("ABANDON", shard_id, None, 0, reason, clock)

    def claim_shards(
        self,
        experiment: Experiment,
        plan: RunPlan,
        limit: int,
    ) -> list[ShardPlan]:
        plan_by_id = {shard.shard_id: shard for shard in plan.shard_plans}
        claimed: list[ShardPlan] = []
        with self._transaction():
            self._reclaim_dead_locked()
            active_count, active_bytes, _max_active = self._current_usage()
            candidates = self.conn.execute(
                "SELECT shard_id FROM shard WHERE status = 'PENDING' "
                "ORDER BY estimated_bytes DESC, shard_id ASC"
            ).fetchall()
            step = experiment.config.logical_clock_step
            for row in candidates:
                shard_id = str(row["shard_id"])
                shard = plan_by_id[shard_id]
                if shard.estimated_bytes > experiment.config.per_task_memory_bytes:
                    self._abandon_locked(
                        shard_id,
                        "MEMORY_BUDGET_PER_TASK",
                        "estimated_bytes exceeds per_task_memory_mib",
                        step,
                    )
                    continue
                if shard.estimated_bytes > experiment.config.total_memory_budget_bytes:
                    self._abandon_locked(
                        shard_id,
                        "TOTAL_MEMORY_BUDGET",
                        "estimated_bytes exceeds total_memory_budget_mib",
                        step,
                    )
                    continue
                if len(claimed) >= limit or active_count >= experiment.config.parallel_limit:
                    break
                if active_bytes + shard.estimated_bytes > experiment.config.total_memory_budget_bytes:
                    break
                now = time.time()
                self.conn.execute(
                    "INSERT INTO lease(shard_id, owner, reserved_bytes, acquired_at) VALUES(?, ?, ?, ?)",
                    (shard_id, self.worker_id, shard.estimated_bytes, now),
                )
                self.conn.execute(
                    "UPDATE shard SET status = 'RUNNING', attempts = attempts + 1, started_at = ?, "
                    "updated_at = ?, error_code = NULL, error_message = NULL WHERE shard_id = ?",
                    (now, now, shard_id),
                )
                clock = self._bump_clock(step)
                self._record_budget(
                    "RESERVE", shard_id, self.worker_id, shard.estimated_bytes, None, clock
                )
                active_count += 1
                active_bytes += shard.estimated_bytes
                claimed.append(shard)
            peak = max(
                int(self._meta("peak_reserved_bytes") or "0"),
                active_bytes,
            )
            self._set_meta("peak_reserved_bytes", str(peak))
        return claimed

    def _applied(self, injection_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM injection_applied WHERE injection_id = ?", (injection_id,)
        ).fetchone()
        return row is not None

    def claim_shard_injections(
        self, shard: ShardPlan
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        with self._transaction():
            raw = json.loads(self._meta("injection_json") or "{}")
            for injection_id in sorted(raw):
                item = raw[injection_id]
                trigger = item.get("trigger", {})
                matches = False
                if "shard_id" in trigger and trigger["shard_id"] == shard.shard_id:
                    matches = True
                if "shard_ordinal" in trigger and int(trigger["shard_ordinal"]) == shard.ordinal:
                    matches = True
                if "after_committed_shards" in trigger:
                    continue
                if not matches:
                    continue
                if item.get("once", True) and self._applied(injection_id):
                    continue
                clock = int(
                    self.conn.execute("SELECT logical_clock FROM run WHERE run_id = 1").fetchone()[
                        "logical_clock"
                    ]
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO injection_applied(injection_id, shard_id, applied_at, logical_clock) "
                    "VALUES(?, ?, ?, ?)",
                    (injection_id, shard.shard_id, time.time(), clock),
                )
                actions.append(item)
        return actions

    def _after_commit_injections_locked(self, clock: int) -> list[dict[str, Any]]:
        committed = int(
            self.conn.execute("SELECT COUNT(*) AS n FROM shard WHERE status = 'COMMITTED'").fetchone()[
                "n"
            ]
        )
        raw = json.loads(self._meta("injection_json") or "{}")
        actions: list[dict[str, Any]] = []
        for injection_id in sorted(raw):
            item = raw[injection_id]
            trigger = item.get("trigger", {})
            if "after_committed_shards" not in trigger:
                continue
            threshold = int(trigger["after_committed_shards"])
            if committed < threshold:
                continue
            if item.get("once", True) and self._applied(injection_id):
                continue
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO injection_applied(injection_id, shard_id, applied_at, logical_clock) "
                "VALUES(?, NULL, ?, ?)",
                (injection_id, time.time(), clock),
            )
            if cursor.rowcount:
                actions.append(item)
        return actions

    def commit_shard(
        self,
        experiment: Experiment,
        partial: dict[str, Any],
    ) -> list[dict[str, Any]]:
        shard_id = partial["shard_id"]
        blob = canonical_json(partial).encode("utf-8")
        digest = _sha(blob.decode("utf-8"))
        actions: list[dict[str, Any]] = []
        with self._transaction():
            shard = self.conn.execute("SELECT * FROM shard WHERE shard_id = ?", (shard_id,)).fetchone()
            if shard is None:
                raise fail(ErrorCode.STATE_MISMATCH, "unknown shard", shard_id=shard_id)
            if shard["status"] == "COMMITTED":
                if shard["result_digest"] == digest:
                    return []
                raise fail(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "shard was already committed with a different result",
                    shard_id=shard_id,
                    stored_digest=shard["result_digest"],
                    incoming_digest=digest,
                )
            if shard["status"] != "RUNNING":
                raise fail(
                    ErrorCode.LEASE_CONFLICT,
                    "shard is not in RUNNING state",
                    shard_id=shard_id,
                    status=shard["status"],
                )
            lease = self.conn.execute("SELECT * FROM lease WHERE shard_id = ?", (shard_id,)).fetchone()
            if lease is None:
                raise fail(ErrorCode.LEASE_CONFLICT, "shard has no active lease", shard_id=shard_id)
            if lease["owner"] != self.worker_id:
                raise fail(
                    ErrorCode.LEASE_CONFLICT,
                    "shard lease belongs to a different worker",
                    shard_id=shard_id,
                    lease_owner=lease["owner"],
                    worker=self.worker_id,
                )
            if shard["content_digest"] != partial["content_digest"]:
                raise fail(
                    ErrorCode.STATE_MISMATCH,
                    "shard content digest changed",
                    shard_id=shard_id,
                    stored=shard["content_digest"],
                    incoming=partial["content_digest"],
                )
            now = time.time()
            self.conn.execute(
                "UPDATE shard SET status = 'COMMITTED', result_digest = ?, result_blob = ?, "
                "committed_at = ?, updated_at = ?, error_code = NULL, error_message = NULL "
                "WHERE shard_id = ?",
                (digest, blob, now, now, shard_id),
            )
            self.conn.execute("DELETE FROM lease WHERE shard_id = ?", (shard_id,))
            clock = self._bump_clock(experiment.config.logical_clock_step)
            self._record_budget(
                "RELEASE",
                shard_id,
                lease["owner"],
                -int(lease["reserved_bytes"]),
                "COMMITTED",
                clock,
            )
            for window in partial.get("window", {}).get("windows", []):
                buf_digest = _sha(
                    canonical_json(
                        {
                            "window_id": window["window_id"],
                            "shard_id": shard_id,
                            "rows": window["rows"],
                            "metrics": window["metrics"],
                        }
                    )
                )
                self.conn.execute(
                    "INSERT INTO window_buffer(window_id, shard_id, rows, partial_digest, partial_blob, committed_at) "
                    "VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(window_id, shard_id) DO UPDATE SET rows=excluded.rows, "
                    "partial_digest=excluded.partial_digest, partial_blob=excluded.partial_blob, "
                    "committed_at=excluded.committed_at",
                    (
                        window["window_id"],
                        shard_id,
                        int(window["rows"]),
                        buf_digest,
                        canonical_json(window).encode("utf-8"),
                        now,
                    ),
                )
            self.conn.execute(
                "UPDATE watermark SET committed_shards = ("
                "SELECT COUNT(*) FROM shard WHERE batch_index = watermark.batch_index AND status = 'COMMITTED'"
                "), last_logical_clock = ? WHERE batch_index = ?",
                (clock, int(shard["batch_index"])),
            )
            committed_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) AS n FROM shard WHERE status = 'COMMITTED'"
                ).fetchone()["n"]
            )
            terminal_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) AS n FROM shard WHERE status IN ('COMMITTED', 'FAILED', 'ABANDONED')"
                ).fetchone()["n"]
            )
            total_count = int(self.conn.execute("SELECT COUNT(*) AS n FROM shard").fetchone()["n"])
            operator_status = "READY" if committed_count == total_count else "RUNNING"
            self.conn.execute(
                "UPDATE operator SET committed_shards = ?, status = ?, updated_at = ?",
                (committed_count, operator_status, now),
            )
            if terminal_count == total_count and committed_count == total_count:
                self.conn.execute(
                    "UPDATE run SET status = 'FINALIZING', updated_at = ? WHERE run_id = 1",
                    (now,),
                )
            actions = self._after_commit_injections_locked(clock)
        return actions

    def mark_shard_failed(
        self,
        experiment: Experiment,
        shard_id: str,
        error_code: str,
        error_message: str,
    ) -> None:
        with self._transaction():
            shard = self.conn.execute("SELECT * FROM shard WHERE shard_id = ?", (shard_id,)).fetchone()
            if shard is None or shard["status"] == "COMMITTED":
                return
            lease = self.conn.execute("SELECT * FROM lease WHERE shard_id = ?", (shard_id,)).fetchone()
            now = time.time()
            self.conn.execute(
                "UPDATE shard SET status = 'FAILED', error_code = ?, error_message = ?, updated_at = ? "
                "WHERE shard_id = ?",
                (error_code, error_message, now, shard_id),
            )
            self.conn.execute("DELETE FROM lease WHERE shard_id = ?", (shard_id,))
            clock = self._bump_clock(experiment.config.logical_clock_step)
            if lease is not None:
                self._record_budget(
                    "RELEASE",
                    shard_id,
                    lease["owner"],
                    -int(lease["reserved_bytes"]),
                    "FAILED",
                    clock,
                )
            terminal_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) AS n FROM shard WHERE status IN ('COMMITTED', 'FAILED', 'ABANDONED')"
                ).fetchone()["n"]
            )
            total_count = int(self.conn.execute("SELECT COUNT(*) AS n FROM shard").fetchone()["n"])
            if terminal_count == total_count:
                self.conn.execute(
                    "UPDATE run SET status = 'INCOMPLETE', error_code = ?, error_message = ?, updated_at = ? "
                    "WHERE run_id = 1",
                    (ErrorCode.SHARD_FAILED, "one or more shards failed", now),
                )

    def all_shards_committed(self) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'COMMITTED' THEN 1 ELSE 0 END) AS done FROM shard"
        ).fetchone()
        return int(row["done"] or 0) == int(row["total"])

    def has_terminal_failures(self) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM shard WHERE status IN ('FAILED', 'ABANDONED')"
        ).fetchone()
        return int(row["n"]) > 0

    def has_abandoned(self) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM shard WHERE status = 'ABANDONED'"
        ).fetchone()
        return int(row["n"]) > 0

    def pending_or_running(self) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM shard WHERE status IN ('PENDING', 'RUNNING')"
        ).fetchone()
        return int(row["n"]) > 0

    def get_committed_partials(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT shard_id, result_blob, result_digest FROM shard "
            "WHERE status = 'COMMITTED' ORDER BY batch_index, shard_index, shard_id"
        ).fetchall()
        partials: list[dict[str, Any]] = []
        for row in rows:
            payload = bytes(row["result_blob"])
            digest = _sha(payload.decode("utf-8"))
            if digest != row["result_digest"]:
                raise fail(
                    ErrorCode.STATE_MISMATCH,
                    "committed shard result digest is corrupted",
                    shard_id=row["shard_id"],
                    stored_digest=row["result_digest"],
                    actual_digest=digest,
                )
            partial = json.loads(payload.decode("utf-8"))
            if partial.get("shard_id") != row["shard_id"]:
                raise fail(
                    ErrorCode.STATE_MISMATCH,
                    "committed shard result has an unexpected shard id",
                    expected=row["shard_id"],
                    actual=partial.get("shard_id"),
                )
            partials.append(partial)
        return partials

    def complete_run(
        self,
        experiment: Experiment,
        groups: list[dict[str, Any]],
        windows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        group_digest = _sha(canonical_json(groups))
        window_digest = _sha(canonical_json(windows))
        already_complete = False
        with self._transaction() as conn:
            run = conn.execute("SELECT * FROM run WHERE run_id = 1").fetchone()
            if run["status"] == "COMPLETE":
                stored_group = conn.execute(
                    "SELECT result_digest FROM operator WHERE operator_id = 'group_agg'"
                ).fetchone()
                stored_window = conn.execute(
                    "SELECT result_digest FROM operator WHERE operator_id = 'window_agg'"
                ).fetchone()
                if (
                    stored_group is None
                    or stored_window is None
                    or stored_group["result_digest"] != group_digest
                    or stored_window["result_digest"] != window_digest
                ):
                    raise fail(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "completed run already holds different final result digests",
                        stored_group_digest=None
                        if stored_group is None
                        else stored_group["result_digest"],
                        incoming_group_digest=group_digest,
                        stored_window_digest=None
                        if stored_window is None
                        else stored_window["result_digest"],
                        incoming_window_digest=window_digest,
                    )
                already_complete = True
            else:
                if not self.all_shards_committed():
                    raise fail(
                        ErrorCode.RUN_INCOMPLETE,
                        "cannot finalize before every shard is committed",
                    )
                now = time.time()
                conn.execute("DELETE FROM result")
                for index, group in enumerate(groups):
                    payload = canonical_json(group)
                    conn.execute(
                        "INSERT INTO result(kind, result_key, ordinal, result_json, result_digest, sources_json) "
                        "VALUES('group', ?, ?, ?, ?, ?)",
                        (
                            group["key"],
                            index,
                            payload,
                            _sha(payload),
                            canonical_json(group.get("sources", {})),
                        ),
                    )
                for index, window in enumerate(windows):
                    payload = canonical_json(window)
                    conn.execute(
                        "INSERT INTO result(kind, result_key, ordinal, result_json, result_digest, sources_json) "
                        "VALUES('window', ?, ?, ?, ?, ?)",
                        (
                            window["window_id"],
                            index,
                            payload,
                            _sha(payload),
                            canonical_json(window.get("sources", {})),
                        ),
                    )
                clock = self._bump_clock(experiment.config.logical_clock_step)
                conn.execute(
                    "UPDATE operator SET status = 'COMMITTED', committed_shards = expected_shards, "
                    "result_digest = ?, updated_at = ? WHERE operator_id = 'group_agg'",
                    (group_digest, now),
                )
                conn.execute(
                    "UPDATE operator SET status = 'COMMITTED', committed_shards = expected_shards, "
                    "result_digest = ?, updated_at = ? WHERE operator_id = 'window_agg'",
                    (window_digest, now),
                )
                conn.execute(
                    "UPDATE run SET status = 'COMPLETE', error_code = NULL, error_message = NULL, "
                    "logical_clock = ?, updated_at = ? WHERE run_id = 1",
                    (clock, now),
                )
        if already_complete:
            return self.run_summary()
        return self.run_summary()

    def mark_incomplete(self, code: str, message: str) -> None:
        with self._transaction() as conn:
            now = time.time()
            conn.execute(
                "UPDATE run SET status = 'INCOMPLETE', error_code = ?, error_message = ?, updated_at = ? "
                "WHERE run_id = 1 AND status != 'COMPLETE'",
                (code, message, now),
            )
            conn.execute(
                "UPDATE operator SET status = 'INCOMPLETE', updated_at = ? WHERE status != 'COMMITTED'",
                (now,),
            )

    def read_results(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows = self.conn.execute(
            "SELECT kind, result_json, result_digest FROM result ORDER BY kind, ordinal"
        ).fetchall()
        groups: list[dict[str, Any]] = []
        windows: list[dict[str, Any]] = []
        for row in rows:
            payload = row["result_json"]
            if _sha(payload) != row["result_digest"]:
                raise fail(
                    ErrorCode.STATE_MISMATCH,
                    "stored result digest is corrupted",
                    kind=row["kind"],
                )
            parsed = json.loads(payload)
            if row["kind"] == "group":
                groups.append(parsed)
            else:
                windows.append(parsed)
        return groups, windows

    def run_summary(self) -> dict[str, Any]:
        with self._critical(write=False):
            row = self.conn.execute("SELECT * FROM run WHERE run_id = 1").fetchone()
            if row is None:
                raise fail(ErrorCode.STATE_NOT_FOUND, "state database has no run")
            shard_rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM shard GROUP BY status ORDER BY status"
            ).fetchall()
            counts = {str(item["status"]): int(item["n"]) for item in shard_rows}
            results = self.conn.execute(
                "SELECT kind, COUNT(*) AS n FROM result GROUP BY kind ORDER BY kind"
            ).fetchall()
            return {
                "ok": row["status"] == "COMPLETE",
                "run": self._run_dict(row),
                "shard_counts": counts,
                "results": {str(item["kind"]): int(item["n"]) for item in results},
            }

    def status(self) -> dict[str, Any]:
        with self._critical(write=False):
            row = self.conn.execute("SELECT * FROM run WHERE run_id = 1").fetchone()
            if row is None:
                raise fail(ErrorCode.STATE_NOT_FOUND, "state database has no run")
            shard_rows = self.conn.execute(
                "SELECT * FROM shard ORDER BY batch_index, shard_index, shard_id"
            ).fetchall()
            lease_rows = {
                lease["shard_id"]: lease
                for lease in self.conn.execute("SELECT * FROM lease ORDER BY shard_id").fetchall()
            }
            shard_counts: dict[str, int] = {}
            shards: list[dict[str, Any]] = []
            for shard in shard_rows:
                status = str(shard["status"])
                shard_counts[status] = shard_counts.get(status, 0) + 1
                lease = lease_rows.get(shard["shard_id"])
                shards.append(
                    {
                        "shard_id": shard["shard_id"],
                        "batch_index": int(shard["batch_index"]),
                        "shard_index": int(shard["shard_index"]),
                        "row_start": int(shard["row_start"]),
                        "row_end": int(shard["row_end"]),
                        "shard_digest": shard["shard_digest"],
                        "content_digest": shard["content_digest"],
                        "status": status,
                        "attempts": int(shard["attempts"]),
                        "interrupted_attempts": int(shard["interrupted_attempts"]),
                        "estimated_bytes": int(shard["estimated_bytes"]),
                        "estimate": json.loads(shard["estimate_json"]),
                        "result_digest": shard["result_digest"],
                        "error_code": shard["error_code"],
                        "error_message": shard["error_message"],
                        "lease": None
                        if lease is None
                        else {
                            "owner": lease["owner"],
                            "owner_alive": _owner_alive(lease["owner"]),
                            "reserved_bytes": int(lease["reserved_bytes"]),
                        },
                    }
                )
            operators = [
                {
                    "operator_id": item["operator_id"],
                    "version": item["version"],
                    "status": item["status"],
                    "expected_shards": int(item["expected_shards"]),
                    "committed_shards": int(item["committed_shards"]),
                    "result_digest": item["result_digest"],
                }
                for item in self.conn.execute(
                    "SELECT * FROM operator ORDER BY operator_id"
                ).fetchall()
            ]
            watermarks = [
                {
                    "batch_index": int(item["batch_index"]),
                    "committed_shards": int(item["committed_shards"]),
                    "total_shards": int(item["total_shards"]),
                    "last_logical_clock": int(item["last_logical_clock"]),
                }
                for item in self.conn.execute(
                    "SELECT * FROM watermark ORDER BY batch_index"
                ).fetchall()
            ]
            leases = [
                {
                    "shard_id": item["shard_id"],
                    "owner": item["owner"],
                    "owner_alive": _owner_alive(item["owner"]),
                    "reserved_bytes": int(item["reserved_bytes"]),
                }
                for item in self.conn.execute("SELECT * FROM lease ORDER BY shard_id").fetchall()
            ]
            active_bytes = sum(item["reserved_bytes"] for item in leases)
            events = [
                {
                    "event_id": int(item["event_id"]),
                    "action": item["action"],
                    "shard_id": item["shard_id"],
                    "owner": item["owner"],
                    "bytes": int(item["bytes"]),
                    "reason": item["reason"],
                    "logical_clock": int(item["logical_clock"]),
                }
                for item in self.conn.execute(
                    "SELECT * FROM budget_event ORDER BY event_id"
                ).fetchall()
            ]
            applied = [
                {
                    "injection_id": item["injection_id"],
                    "shard_id": item["shard_id"],
                    "logical_clock": int(item["logical_clock"]),
                }
                for item in self.conn.execute(
                    "SELECT * FROM injection_applied ORDER BY injection_id, applied_at"
                ).fetchall()
            ]
            window_buffers = [
                {
                    "window_id": item["window_id"],
                    "shard_id": item["shard_id"],
                    "rows": int(item["rows"]),
                    "partial_digest": item["partial_digest"],
                }
                for item in self.conn.execute(
                    "SELECT * FROM window_buffer ORDER BY window_id, shard_id"
                ).fetchall()
            ]
            results = [
                {
                    "kind": item["kind"],
                    "result_key": item["result_key"],
                    "ordinal": int(item["ordinal"]),
                    "result_digest": item["result_digest"],
                }
                for item in self.conn.execute(
                    "SELECT * FROM result ORDER BY kind, ordinal"
                ).fetchall()
            ]
            return {
                "ok": row["status"] == "COMPLETE",
                "run": self._run_dict(row),
                "shard_watermark": {
                    "counts": dict(sorted(shard_counts.items())),
                    "batches": watermarks,
                    "shards": shards,
                },
                "operators": operators,
                "budget": {
                    "active_leases": len(leases),
                    "reserved_bytes": active_bytes,
                    "reserved_mib": active_bytes / (1024 * 1024),
                    "peak_reserved_bytes": int(self._meta("peak_reserved_bytes") or "0"),
                    "leases": leases,
                    "events": events,
                },
                "window_buffer": {
                    "entries": len(window_buffers),
                    "buffers": window_buffers,
                },
                "injections_applied": applied,
                "results": results,
            }


class ReadonlyStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        if not self.path.exists():
            raise fail(
                ErrorCode.STATE_NOT_FOUND,
                "state database does not exist",
                db_path=str(self.path),
            )
        self.conn = sqlite3.connect(
            f"file:{self.path}?mode=ro",
            uri=True,
            timeout=30.0,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")

    def close(self) -> None:
        self.conn.close()

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @contextmanager
    def _critical(self, write: bool = False) -> Iterator[None]:
        del write
        yield

    def _run_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "status": row["status"],
            "input_digest": row["input_digest"],
            "query_digest": row["query_digest"],
            "config_digest": row["config_digest"],
            "plan_digest": row["plan_digest"],
            "mode": row["mode"],
            "mode_decision": json.loads(row["mode_decision_json"]),
            "logical_clock": int(row["logical_clock"]),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
        }

    def status(self) -> dict[str, Any]:
        return Store.status(self)
