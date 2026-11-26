"""SQLite transactional state, file locking and recovery primitives."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .cost import DriftEvent, parse_drift_dict
from .errors import (
    LATE_RESULT,
    NO_ACTIVE_PLAN,
    NOT_FOUND,
    PLAN_VERSION_CONFLICT,
    REVISION_CONFLICT,
    STALE_REVISION,
    STAT_CONFLICT,
    UNKNOWN_JOB,
    BusinessError,
)
from .models import (
    Plan,
    PlatformStat,
    PlatformStats,
    Task,
    parse_plan_object,
)
from .subtasks import Subtask, SubtaskChain
from .util import canonical_json, digest_obj


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    job_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    task_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_revisions (
    job_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    task_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (job_id, revision)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_version TEXT PRIMARY KEY,
    plan_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_stats (
    logical_clock_ms INTEGER NOT NULL,
    cache_layer TEXT NOT NULL,
    hit_rate REAL NOT NULL,
    thread_occupancy INTEGER NOT NULL,
    throughput_cost REAL NOT NULL,
    stat_id TEXT NOT NULL,
    PRIMARY KEY (logical_clock_ms, cache_layer)
);

CREATE TABLE IF NOT EXISTS subtasks (
    subtask_id TEXT PRIMARY KEY,
    subtask_hash TEXT NOT NULL,
    job_id TEXT NOT NULL,
    job_revision INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    phase TEXT NOT NULL,
    shard_index INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subtasks_job
    ON subtasks(job_id, job_revision, ordinal);

CREATE TABLE IF NOT EXISTS plan_subtasks (
    plan_version TEXT NOT NULL,
    subtask_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (plan_version, subtask_id)
);
CREATE INDEX IF NOT EXISTS idx_plan_subtasks_order
    ON plan_subtasks(plan_version, job_id, ordinal);

CREATE TABLE IF NOT EXISTS ledger (
    subtask_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    job_revision INTEGER NOT NULL,
    plan_version TEXT NOT NULL,
    subtask_hash TEXT NOT NULL,
    output_digest TEXT NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    status TEXT NOT NULL,
    cost_json TEXT NOT NULL,
    commit_seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_job
    ON ledger(job_id, job_revision);

CREATE TABLE IF NOT EXISTS ledger_attempts (
    attempt_id TEXT PRIMARY KEY,
    subtask_id TEXT NOT NULL,
    plan_version TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_pending
    ON ledger_attempts(status, subtask_id);

CREATE TABLE IF NOT EXISTS schedule_runs (
    run_id TEXT PRIMARY KEY,
    plan_version TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_items (
    run_id TEXT NOT NULL,
    subtask_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, subtask_id)
);

CREATE TABLE IF NOT EXISTS arbitrations (
    decision_id TEXT PRIMARY KEY,
    signature TEXT NOT NULL UNIQUE,
    plan_version TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    deadline_clock_ms REAL NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_arbitrations_deadline
    ON arbitrations(plan_version, deadline_clock_ms, priority, job_id);

CREATE TABLE IF NOT EXISTS arbitration_conflicts (
    conflict_id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    incoming_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invalidation_closures (
    closure_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    plan_version TEXT NOT NULL,
    reason TEXT NOT NULL,
    affected_json TEXT NOT NULL,
    reused_json TEXT NOT NULL,
    protected_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS invalidation_items (
    closure_id TEXT NOT NULL,
    subtask_id TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (closure_id, subtask_id)
);

CREATE TABLE IF NOT EXISTS drift_events (
    event_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    plan_version TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drift_plan_clock
    ON drift_events(plan_version, logical_clock_ms, event_id);

CREATE TABLE IF NOT EXISTS late_arrivals (
    late_id TEXT PRIMARY KEY,
    subtask_id TEXT NOT NULL,
    plan_version TEXT NOT NULL,
    accepted_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    policy TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_log (
    change_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    details_json TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watermark_versions (
    watermark_version INTEGER PRIMARY KEY,
    pointer_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watermarks (
    watermark_version INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    plan_version TEXT NOT NULL,
    revision INTEGER NOT NULL,
    max_ordinal INTEGER NOT NULL,
    summary_digest TEXT NOT NULL,
    PRIMARY KEY (watermark_version, job_id)
);

CREATE TABLE IF NOT EXISTS build_checkpoints (
    plan_version TEXT NOT NULL,
    job_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    task_hash TEXT NOT NULL,
    summary_digest TEXT NOT NULL,
    subtask_summary TEXT NOT NULL,
    ledger_count INTEGER NOT NULL,
    PRIMARY KEY (plan_version, job_id, revision)
);

CREATE TABLE IF NOT EXISTS cost_evaluations (
    plan_version TEXT PRIMARY KEY,
    result_digest TEXT NOT NULL,
    selected_candidate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_forecasts (
    plan_version TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    aggregation_index INTEGER NOT NULL,
    subtask_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (plan_version, candidate_id, aggregation_index)
);
CREATE INDEX IF NOT EXISTS idx_cost_forecasts_subtask
    ON cost_forecasts(plan_version, subtask_id);
"""


class Storage:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.conn = sqlite3.connect(
            str(self.path),
            timeout=15.0,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(invalidation_closures)")
        }
        if "payload_json" not in columns:
            self.conn.execute(
                "ALTER TABLE invalidation_closures ADD COLUMN payload_json TEXT"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @contextmanager
    def transaction(self, *, acquire_lock: bool = True) -> Iterator[sqlite3.Connection]:
        if acquire_lock:
            with self.write_lock():
                with self.transaction(acquire_lock=False) as conn:
                    yield conn
            return
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def meta_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def meta_set(self, conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, canonical_json(value)),
        )

    def _next_sequence(self, conn: sqlite3.Connection) -> int:
        value = int(self.meta_get("state_sequence", 0)) + 1
        self.meta_set(conn, "state_sequence", value)
        return value

    def active_plan_version(self) -> str | None:
        return self.meta_get("current_plan_version")

    def previous_plan_version(self) -> str | None:
        return self.meta_get("previous_plan_version")

    def logical_clock_ms(self) -> int:
        return int(self.meta_get("logical_clock_ms", 0))

    def advance_logical_clock(self, clock_ms: int) -> int:
        with self.transaction() as conn:
            current = int(self.meta_get("logical_clock_ms", 0))
            value = max(current, int(clock_ms))
            self.meta_set(conn, "logical_clock_ms", value)
            return value

    def get_plan(self, plan_version: str | None = None) -> Plan:
        version = plan_version or self.active_plan_version()
        if not version:
            raise BusinessError(NO_ACTIVE_PLAN, "no active plan version")
        row = self.one(
            "SELECT payload_json FROM plans WHERE plan_version = ?", (version,)
        )
        if row is None:
            raise BusinessError(
                NOT_FOUND,
                "plan version does not exist",
                details={"plan_version": version},
            )
        return parse_plan_object(
            json.loads(row["payload_json"]), source=f"sqlite:{version}"
        )

    def get_plan_hash(self, plan_version: str) -> str:
        row = self.one(
            "SELECT plan_hash FROM plans WHERE plan_version = ?", (plan_version,)
        )
        if row is None:
            raise BusinessError(
                NOT_FOUND,
                "plan version does not exist",
                details={"plan_version": plan_version},
            )
        return str(row["plan_hash"])

    def get_tasks(self) -> dict[str, Task]:
        rows = self.query("SELECT payload_json FROM tasks ORDER BY job_id")
        return {
            task.job_id: task
            for task in (
                Task.from_dict(json.loads(row["payload_json"]))
                for row in rows
            )
        }

    def get_task_revision(self, job_id: str, revision: int) -> Task:
        row = self.one(
            "SELECT payload_json FROM task_revisions "
            "WHERE job_id = ? AND revision = ?",
            (job_id, revision),
        )
        if row is None:
            raise BusinessError(
                NOT_FOUND,
                "task revision does not exist",
                details={"job_id": job_id, "revision": revision},
            )
        return Task.from_dict(json.loads(row["payload_json"]))

    def get_platform_stats(self) -> PlatformStats:
        rows = self.query(
            "SELECT logical_clock_ms, cache_layer, hit_rate, thread_occupancy, "
            "throughput_cost FROM platform_stats "
            "ORDER BY logical_clock_ms, cache_layer"
        )
        entries = tuple(
            PlatformStat(
                logical_clock_ms=int(row["logical_clock_ms"]),
                cache_layer=str(row["cache_layer"]),
                hit_rate=float(row["hit_rate"]),
                thread_occupancy=int(row["thread_occupancy"]),
                throughput_cost=float(row["throughput_cost"]),
            )
            for row in rows
        )
        if not entries:
            raise BusinessError(
                NOT_FOUND, "no platform statistics have been loaded"
            )
        max_threads = int(
            self.meta_get(
                "platform_max_threads",
                max(entry.thread_occupancy for entry in entries),
            )
        )
        return PlatformStats(max_threads=max_threads, entries=entries)

    def ingest(
        self,
        *,
        tasks: list[Task] | None = None,
        stats: PlatformStats | None = None,
        plan: Plan | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            current_clock = int(self.meta_get("logical_clock_ms", 0))
            previous_clock = current_clock
            active_plan: Plan | None = None
            active_version = self.meta_get("current_plan_version")
            if active_version:
                active_row = self.one(
                    "SELECT payload_json FROM plans WHERE plan_version=?",
                    (active_version,),
                )
                if active_row is not None:
                    active_plan = parse_plan_object(
                        json.loads(active_row["payload_json"]),
                        source=f"sqlite:{active_version}",
                    )
            if plan is not None:
                existing_max_row = self.one(
                    "SELECT COALESCE(MAX(thread_occupancy), 0) AS maximum "
                    "FROM platform_stats"
                )
                existing_max = int(existing_max_row["maximum"])
                incoming_max = (
                    max(
                        (entry.thread_occupancy for entry in stats.entries),
                        default=0,
                    )
                    if stats
                    else 0
                )
                if max(existing_max, incoming_max) > plan.max_threads:
                    raise BusinessError(
                        "INVALID_THREAD_OCCUPANCY",
                        "persisted or incoming thread occupancy exceeds plan max_threads",
                        details={
                            "plan_version": plan.plan_version,
                            "max_threads": plan.max_threads,
                            "maximum_occupancy": max(existing_max, incoming_max),
                        },
                    )
            if stats is not None and active_plan is not None:
                maximum = max(
                    (entry.thread_occupancy for entry in stats.entries),
                    default=0,
                )
                if maximum > active_plan.max_threads:
                    raise BusinessError(
                        "INVALID_THREAD_OCCUPANCY",
                        "thread occupancy exceeds active plan max_threads",
                        details={
                            "plan_version": active_plan.plan_version,
                            "max_threads": active_plan.max_threads,
                            "maximum_occupancy": maximum,
                        },
                    )
            if tasks:
                for task in tasks:
                    if task.logical_clock_ms < current_clock:
                        raise BusinessError(
                            "CLOCK_REGRESSION",
                            "task logical clock is older than persisted state",
                            details={
                                "job_id": task.job_id,
                                "current": current_clock,
                                "incoming": task.logical_clock_ms,
                            },
                        )
                for task in tasks:
                    self._ingest_task(conn, task)
                current_clock = max(
                    current_clock, max(task.logical_clock_ms for task in tasks)
                )
                self.meta_set(conn, "logical_clock_ms", current_clock)

            changed_layers: set[str] = set()
            if stats:
                first_stat_clock = min(
                    entry.logical_clock_ms for entry in stats.entries
                )
                if first_stat_clock < previous_clock:
                    raise BusinessError(
                        "CLOCK_REGRESSION",
                        "platform stats logical clock is older than persisted state",
                        details={
                            "previous": previous_clock,
                            "incoming": first_stat_clock,
                        },
                    )
                changed_layers = self._ingest_stats(conn, stats)
                current_clock = max(
                    current_clock,
                    max(entry.logical_clock_ms for entry in stats.entries),
                )
                self.meta_set(conn, "logical_clock_ms", current_clock)
                self.meta_set(conn, "platform_max_threads", stats.max_threads)

            plan_changed = False
            if plan:
                known_tasks = {
                    row["job_id"]
                    for row in self.query("SELECT job_id FROM tasks ORDER BY job_id")
                }
                if tasks:
                    known_tasks.update(task.job_id for task in tasks)
                unknown = sorted(set(plan.jobs) - known_tasks)
                if unknown:
                    raise BusinessError(
                        UNKNOWN_JOB,
                        "plan references jobs that have not been loaded",
                        details={"job_ids": unknown, "plan_version": plan.plan_version},
                    )
                plan_changed = self._ingest_plan(conn, plan)
                if plan_changed:
                    current = self.meta_get("current_plan_version")
                    if current and current != plan.plan_version:
                        self.meta_set(conn, "previous_plan_version", current)
                    self.meta_set(conn, "current_plan_version", plan.plan_version)
                self.meta_set(
                    conn,
                    "logical_clock_ms",
                    max(current_clock, plan.base_logical_clock_ms),
                )

            if changed_layers:
                details = {
                    "cache_layers": sorted(changed_layers),
                    "latest": {
                        layer: stats.latest_by_layer()[layer].to_dict()
                        for layer in sorted(changed_layers)
                    },
                }
                change_id = "chg_" + digest_obj(
                    {"kind": "cache_layer_change", "details": details}
                )
                conn.execute(
                    "INSERT OR IGNORE INTO change_log(change_id, kind, details_json, consumed) "
                    "VALUES(?, 'cache_layer_change', ?, 0)",
                    (change_id, canonical_json(details)),
                )
            return {
                "tasks_loaded": len(tasks or []),
                "stats_loaded": len(stats.entries) if stats else 0,
                "changed_cache_layers": sorted(changed_layers),
                "plan_loaded": plan.plan_version if plan else None,
                "plan_changed": plan_changed,
                "active_plan_version": self.meta_get("current_plan_version"),
                "logical_clock_ms": self.meta_get("logical_clock_ms", 0),
            }

    def _ingest_task(self, conn: sqlite3.Connection, task: Task) -> None:
        existing = self.one(
            "SELECT revision, task_hash FROM tasks WHERE job_id = ?",
            (task.job_id,),
        )
        if existing is not None:
            revision = int(existing["revision"])
            task_hash = str(existing["task_hash"])
            if task.revision < revision:
                raise BusinessError(
                    STALE_REVISION,
                    "task revision is older than persisted revision",
                    details={
                        "job_id": task.job_id,
                        "persisted": revision,
                        "incoming": task.revision,
                    },
                )
            if task.revision == revision:
                if task_hash != task.task_hash:
                    raise BusinessError(
                        REVISION_CONFLICT,
                        "same task revision cannot change content",
                        details={"job_id": task.job_id, "revision": task.revision},
                    )
                return
        payload = canonical_json(task.to_dict())
        conn.execute(
            "INSERT OR IGNORE INTO task_revisions"
            "(job_id, revision, task_hash, payload_json, logical_clock_ms) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                task.job_id,
                task.revision,
                task.task_hash,
                payload,
                task.logical_clock_ms,
            ),
        )
        conn.execute(
            "INSERT INTO tasks(job_id, revision, task_hash, payload_json, logical_clock_ms) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            "revision=excluded.revision, task_hash=excluded.task_hash, "
            "payload_json=excluded.payload_json, "
            "logical_clock_ms=excluded.logical_clock_ms",
            (
                task.job_id,
                task.revision,
                task.task_hash,
                payload,
                task.logical_clock_ms,
            ),
        )

    def _ingest_stats(
        self, conn: sqlite3.Connection, stats: PlatformStats
    ) -> set[str]:
        existing_latest = {
            str(row["cache_layer"]): PlatformStat(
                logical_clock_ms=int(row["logical_clock_ms"]),
                cache_layer=str(row["cache_layer"]),
                hit_rate=float(row["hit_rate"]),
                thread_occupancy=int(row["thread_occupancy"]),
                throughput_cost=float(row["throughput_cost"]),
            )
            for row in self.query(
                "SELECT s.* FROM platform_stats s "
                "JOIN (SELECT cache_layer, MAX(logical_clock_ms) AS clock "
                "      FROM platform_stats GROUP BY cache_layer) m "
                "ON s.cache_layer=m.cache_layer AND s.logical_clock_ms=m.clock"
            )
        }
        changed: set[str] = set()
        for entry in stats.entries:
            row = self.one(
                "SELECT hit_rate, thread_occupancy, throughput_cost "
                "FROM platform_stats WHERE logical_clock_ms = ? AND cache_layer = ?",
                (entry.logical_clock_ms, entry.cache_layer),
            )
            if row is not None:
                if (
                    float(row["hit_rate"]) != entry.hit_rate
                    or int(row["thread_occupancy"]) != entry.thread_occupancy
                    or float(row["throughput_cost"]) != entry.throughput_cost
                ):
                    raise BusinessError(
                        STAT_CONFLICT,
                        "platform stats conflict at the same logical clock and layer",
                        details={
                            "logical_clock_ms": entry.logical_clock_ms,
                            "cache_layer": entry.cache_layer,
                        },
                    )
                continue
            conn.execute(
                "INSERT INTO platform_stats"
                "(logical_clock_ms, cache_layer, hit_rate, thread_occupancy, "
                " throughput_cost, stat_id) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    entry.logical_clock_ms,
                    entry.cache_layer,
                    entry.hit_rate,
                    entry.thread_occupancy,
                    entry.throughput_cost,
                    entry.stat_id,
                ),
            )
        new_latest = stats.latest_by_layer()
        if existing_latest:
            for layer, entry in new_latest.items():
                old = existing_latest.get(layer)
                if old is None or old.to_dict() != entry.to_dict():
                    changed.add(layer)
        return changed

    def _ingest_plan(self, conn: sqlite3.Connection, plan: Plan) -> bool:
        row = self.one(
            "SELECT plan_hash FROM plans WHERE plan_version = ?",
            (plan.plan_version,),
        )
        if row is not None:
            if str(row["plan_hash"]) != plan.plan_hash:
                raise BusinessError(
                    PLAN_VERSION_CONFLICT,
                    "plan_version already exists with different content",
                    details={"plan_version": plan.plan_version},
                )
            return False
        conn.execute(
            "INSERT INTO plans(plan_version, plan_hash, payload_json) VALUES(?, ?, ?)",
            (
                plan.plan_version,
                plan.plan_hash,
                canonical_json(plan.to_dict()),
            ),
        )
        return True

    def register_subtasks(
        self,
        *,
        plan: Plan,
        chains: dict[str, SubtaskChain],
    ) -> None:
        with self.transaction() as conn:
            for job_id in sorted(chains):
                chain = chains[job_id]
                for subtask in chain.subtasks:
                    payload = canonical_json(subtask.to_dict())
                    conn.execute(
                        "INSERT OR IGNORE INTO subtasks"
                        "(subtask_id, subtask_hash, job_id, job_revision, ordinal, "
                        " phase, shard_index, payload_json) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            subtask.subtask_id,
                            subtask.subtask_hash,
                            subtask.job_id,
                            subtask.job_revision,
                            subtask.ordinal,
                            subtask.phase,
                            subtask.shard_index,
                            payload,
                        ),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO plan_subtasks"
                        "(plan_version, subtask_id, job_id, ordinal) "
                        "VALUES(?, ?, ?, ?)",
                        (
                            plan.plan_version,
                            subtask.subtask_id,
                            subtask.job_id,
                            subtask.ordinal,
                        ),
                    )

    def stored_subtasks(self, plan_version: str) -> list[Subtask]:
        rows = self.query(
            "SELECT s.payload_json FROM plan_subtasks p "
            "JOIN subtasks s ON s.subtask_id=p.subtask_id "
            "WHERE p.plan_version = ? "
            "ORDER BY p.job_id, p.ordinal, p.subtask_id",
            (plan_version,),
        )
        return [self._subtask_from_payload(json.loads(row["payload_json"])) for row in rows]

    def all_stored_subtasks(self) -> dict[str, Subtask]:
        rows = self.query(
            "SELECT payload_json FROM subtasks ORDER BY subtask_id"
        )
        result: dict[str, Subtask] = {}
        for row in rows:
            subtask = self._subtask_from_payload(json.loads(row["payload_json"]))
            result[subtask.subtask_id] = subtask
        return result

    def subtask_job_map(self) -> dict[str, str]:
        rows = self.query(
            "SELECT subtask_id, job_id FROM subtasks ORDER BY subtask_id"
        )
        return {str(row["subtask_id"]): str(row["job_id"]) for row in rows}

    @staticmethod
    def _subtask_from_payload(payload: dict[str, Any]) -> Subtask:
        return Subtask(
            subtask_id=payload["subtask_id"],
            job_id=payload["job_id"],
            job_revision=int(payload["job_revision"]),
            task_hash=payload["task_hash"],
            ordinal=int(payload["ordinal"]),
            phase=payload["phase"],
            shard_index=int(payload["shard_index"]),
            shard_count=int(payload["shard_count"]),
            input_shard=payload["input_shard"],
            input_shapes=tuple(tuple(shape) for shape in payload["input_shapes"]),
            output_shape=tuple(payload["output_shape"]),
            seed=int(payload["seed"]),
            dependencies=tuple(payload["dependencies"]),
            precision_sensitive=bool(payload["precision_sensitive"]),
        )

    def ledger_entry(self, subtask_id: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM ledger WHERE subtask_id = ?", (subtask_id,))
        if row is None:
            return None
        return {
            "subtask_id": row["subtask_id"],
            "job_id": row["job_id"],
            "job_revision": int(row["job_revision"]),
            "plan_version": row["plan_version"],
            "subtask_hash": row["subtask_hash"],
            "output_digest": row["output_digest"],
            "precision_sensitive": bool(row["precision_sensitive"]),
            "status": row["status"],
            "cost": json.loads(row["cost_json"]),
            "commit_seq": int(row["commit_seq"]),
        }

    def begin_attempt(self, subtask_id: str, plan_version: str) -> dict[str, Any]:
        with self.transaction() as conn:
            existing = self.ledger_entry(subtask_id)
            if existing is not None:
                return {"state": "committed", "ledger": existing}
            attempt_id = "att_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO ledger_attempts"
                "(attempt_id, subtask_id, plan_version, owner_pid, status, detail_json) "
                "VALUES(?, ?, ?, ?, 'pending', '{}')",
                (attempt_id, subtask_id, plan_version, os.getpid()),
            )
            return {"state": "pending", "attempt_id": attempt_id}

    def finish_attempt(
        self,
        *,
        attempt_id: str,
        subtask: Subtask,
        plan_version: str,
        output_digest: str,
        cost: dict[str, Any],
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            existing = self.ledger_entry(subtask.subtask_id)
            if existing is not None:
                if (
                    existing["subtask_hash"] == subtask.subtask_hash
                    and existing["output_digest"] == output_digest
                ):
                    conn.execute(
                        "UPDATE ledger_attempts SET status='committed', detail_json=? "
                        "WHERE attempt_id=?",
                        (canonical_json({"result": "idempotent"}), attempt_id),
                    )
                    return {"state": "reused", "ledger": existing}
                late = self._record_late(
                    conn,
                    subtask=subtask,
                    plan_version=plan_version,
                    accepted_digest=existing["output_digest"],
                    incoming_digest=output_digest,
                    policy="reject_late_and_keep_accepted",
                )
                conn.execute(
                    "UPDATE ledger_attempts SET status='late_rejected', detail_json=? "
                    "WHERE attempt_id=?",
                    (canonical_json(late), attempt_id),
                )
                return {"state": "late_rejected", "ledger": existing, "late": late}
            seq = self._next_sequence(conn)
            conn.execute(
                "INSERT INTO ledger"
                "(subtask_id, job_id, job_revision, plan_version, subtask_hash, "
                " output_digest, precision_sensitive, status, cost_json, commit_seq) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 'committed', ?, ?)",
                (
                    subtask.subtask_id,
                    subtask.job_id,
                    subtask.job_revision,
                    plan_version,
                    subtask.subtask_hash,
                    output_digest,
                    1 if subtask.precision_sensitive else 0,
                    canonical_json(cost),
                    seq,
                ),
            )
            conn.execute(
                "UPDATE ledger_attempts SET status='committed', detail_json=? "
                "WHERE attempt_id=?",
                (canonical_json({"result": "inserted"}), attempt_id),
            )
            return {"state": "inserted", "ledger": self.ledger_entry(subtask.subtask_id)}

    def _record_late(
        self,
        conn: sqlite3.Connection,
        *,
        subtask: Subtask,
        plan_version: str,
        accepted_digest: str,
        incoming_digest: str,
        policy: str,
    ) -> dict[str, Any]:
        payload = {
            "subtask_id": subtask.subtask_id,
            "plan_version": plan_version,
            "accepted_digest": accepted_digest,
            "incoming_digest": incoming_digest,
            "policy": policy,
            "subtask_hash": subtask.subtask_hash,
        }
        late_id = "late_" + digest_obj(payload)
        conn.execute(
            "INSERT OR IGNORE INTO late_arrivals"
            "(late_id, subtask_id, plan_version, accepted_digest, incoming_digest, "
            " policy, payload_json) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                late_id,
                subtask.subtask_id,
                plan_version,
                accepted_digest,
                incoming_digest,
                policy,
                canonical_json(payload),
            ),
        )
        return {"late_id": late_id, **payload}

    def record_late_result(
        self, *, subtask_id: str, plan_version: str, incoming_digest: str
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            row = self.one(
                "SELECT s.payload_json, l.output_digest FROM subtasks s "
                "LEFT JOIN ledger l ON l.subtask_id=s.subtask_id "
                "WHERE s.subtask_id=?",
                (subtask_id,),
            )
            if row is None:
                raise BusinessError(
                    NOT_FOUND,
                    "subtask does not exist",
                    details={"subtask_id": subtask_id},
                )
            if row["output_digest"] is None:
                raise BusinessError(
                    LATE_RESULT,
                    "cannot apply late policy to an uncommitted subtask",
                    details={"subtask_id": subtask_id},
                )
            subtask = self._subtask_from_payload(json.loads(row["payload_json"]))
            return self._record_late(
                conn,
                subtask=subtask,
                plan_version=plan_version,
                accepted_digest=row["output_digest"],
                incoming_digest=incoming_digest,
                policy="reject_late_and_keep_accepted",
            )

    def pending_attempts(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT a.* FROM ledger_attempts a "
            "LEFT JOIN ledger l ON l.subtask_id=a.subtask_id "
            "WHERE a.status='pending' AND l.subtask_id IS NULL "
            "ORDER BY a.attempt_id"
        )
        return [dict(row) for row in rows]

    def orphan_pending_attempts(self) -> int:
        with self.transaction() as conn:
            pending = self.pending_attempts()
            for attempt in pending:
                conn.execute(
                    "UPDATE ledger_attempts SET status='orphaned' WHERE attempt_id=?",
                    (attempt["attempt_id"],),
                )
            return len(pending)

    def save_checkpoint(
        self,
        *,
        plan: Plan,
        chain: SubtaskChain,
        task: Task,
    ) -> None:
        committed = [
            entry
            for entry in (self.ledger_entry(subtask.subtask_id) for subtask in chain.subtasks)
            if entry is not None
        ]
        summary = digest_obj(
            {
                "job_id": chain.job_id,
                "revision": chain.revision,
                "subtask_summary": chain.digest,
                "ledger": [
                    {
                        "subtask_id": entry["subtask_id"],
                        "output_digest": entry["output_digest"],
                    }
                    for entry in sorted(committed, key=lambda item: item["subtask_id"])
                ],
            }
        )
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO build_checkpoints"
                "(plan_version, job_id, revision, task_hash, summary_digest, "
                " subtask_summary, ledger_count) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    plan.plan_version,
                    chain.job_id,
                    chain.revision,
                    task.task_hash,
                    summary,
                    chain.digest,
                    len(committed),
                ),
            )

    def checkpoint(
        self, plan_version: str, job_id: str
    ) -> dict[str, Any] | None:
        row = self.one(
            "SELECT * FROM build_checkpoints "
            "WHERE plan_version=? AND job_id=? ORDER BY revision DESC LIMIT 1",
            (plan_version, job_id),
        )
        return dict(row) if row else None

    def all_checkpoints(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM build_checkpoints ORDER BY plan_version, job_id, revision"
        )
        return [dict(row) for row in rows]

    def advance_watermark(
        self, *, plan: Plan, chains: dict[str, SubtaskChain]
    ) -> dict[str, Any]:
        """Atomically publish a new watermark pointer using a TEMP stage table."""

        with self.transaction() as conn:
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS watermark_stage ("
                "job_id TEXT PRIMARY KEY, plan_version TEXT NOT NULL, "
                "revision INTEGER NOT NULL, max_ordinal INTEGER NOT NULL, "
                "summary_digest TEXT NOT NULL)"
            )
            conn.execute("DELETE FROM watermark_stage")
            staged: list[dict[str, Any]] = []
            for job_id in sorted(chains):
                chain = chains[job_id]
                entries = [
                    self.ledger_entry(subtask.subtask_id)
                    for subtask in chain.subtasks
                ]
                if any(entry is None for entry in entries):
                    continue
                summary_digest = digest_obj(
                    {
                        "job_id": job_id,
                        "plan_version": plan.plan_version,
                        "revision": chain.revision,
                        "subtasks": [
                            {
                                "subtask_id": entry["subtask_id"],
                                "output_digest": entry["output_digest"],
                            }
                            for entry in entries
                        ],
                    }
                )
                staged.append(
                    {
                        "job_id": job_id,
                        "plan_version": plan.plan_version,
                        "revision": chain.revision,
                        "max_ordinal": max(subtask.ordinal for subtask in chain.subtasks),
                        "summary_digest": summary_digest,
                    }
                )
                conn.execute(
                    "INSERT INTO watermark_stage"
                    "(job_id, plan_version, revision, max_ordinal, summary_digest) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        job_id,
                        plan.plan_version,
                        chain.revision,
                        staged[-1]["max_ordinal"],
                        summary_digest,
                    ),
                )
            version_row = self.one(
                "SELECT COALESCE(MAX(watermark_version), 0) + 1 AS next "
                "FROM watermark_versions"
            )
            version = int(version_row["next"])
            pointer = sorted(staged, key=lambda item: item["job_id"])
            pointer_json = canonical_json(pointer)
            current_version = int(
                self.meta_get("current_watermark_version", 0)
            )
            current_row = self.one(
                "SELECT pointer_json FROM watermark_versions "
                "WHERE watermark_version=?",
                (current_version,),
            )
            if (
                current_version > 0
                and current_row is not None
                and current_row["pointer_json"] == pointer_json
            ):
                conn.execute("DROP TABLE watermark_stage")
                return {
                    "watermark_version": current_version,
                    "watermarks": pointer,
                }
            conn.execute(
                "INSERT INTO watermarks"
                "(watermark_version, job_id, plan_version, revision, max_ordinal, summary_digest) "
                "SELECT ?, job_id, plan_version, revision, max_ordinal, summary_digest "
                "FROM watermark_stage",
                (version,),
            )
            conn.execute(
                "INSERT INTO watermark_versions(watermark_version, pointer_json) "
                "VALUES(?, ?)",
                (version, pointer_json),
            )
            self.meta_set(conn, "current_watermark_version", version)
            conn.execute("DROP TABLE watermark_stage")
            return {
                "watermark_version": version,
                "watermarks": pointer,
            }

    def watermarks(self) -> dict[str, Any]:
        version = int(self.meta_get("current_watermark_version", 0))
        if version == 0:
            return {"watermark_version": 0, "watermarks": []}
        rows = self.query(
            "SELECT job_id, plan_version, revision, max_ordinal, summary_digest "
            "FROM watermarks WHERE watermark_version=? ORDER BY job_id",
            (version,),
        )
        return {
            "watermark_version": version,
            "watermarks": [dict(row) for row in rows],
        }

    def save_drift_events(self, events: Iterable[DriftEvent]) -> dict[str, Any]:
        inserted = 0
        events = sorted(
            list(events),
            key=lambda item: (item.logical_clock_ms, item.kind, item.event_id),
        )
        with self.transaction() as conn:
            for event in events:
                before = conn.total_changes
                conn.execute(
                    "INSERT OR IGNORE INTO drift_events"
                    "(event_id, fingerprint, plan_version, logical_clock_ms, payload_json) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.fingerprint,
                        event.plan_version,
                        event.logical_clock_ms,
                        canonical_json(event.to_dict()),
                    ),
                )
                inserted += int(conn.total_changes - before)
        return {"inserted": inserted, "total": len(events)}

    def drift_events(self, plan_version: str) -> list[DriftEvent]:
        rows = self.query(
            "SELECT payload_json FROM drift_events "
            "WHERE plan_version=? ORDER BY logical_clock_ms, event_id",
            (plan_version,),
        )
        return [parse_drift_dict(json.loads(row["payload_json"])) for row in rows]

    def save_schedule(self, run: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as conn:
            existing = self.one(
                "SELECT result_digest FROM schedule_runs WHERE run_id=?",
                (run["run_id"],),
            )
            if existing is not None:
                if str(existing["result_digest"]) != run["result_digest"]:
                    raise BusinessError(
                        "STATE_CONFLICT",
                        "schedule run ID already exists with different content",
                        details={"run_id": run["run_id"]},
                    )
                recognized = self._recognized_arbitrations(
                    run["plan_version"], None
                )
                return {
                    "inserted": False,
                    "run": json.loads(
                        self.one(
                            "SELECT payload_json FROM schedule_runs WHERE run_id=?",
                            (run["run_id"],),
                        )["payload_json"]
                    ),
                    "arbitrations": recognized,
                }
            conn.execute(
                "INSERT INTO schedule_runs"
                "(run_id, plan_version, candidate_id, result_digest, payload_json) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    run["run_id"],
                    run["plan_version"],
                    run["candidate_id"],
                    run["result_digest"],
                    canonical_json(run),
                ),
            )
            for item in run["items"]:
                conn.execute(
                    "INSERT INTO schedule_items"
                    "(run_id, subtask_id, job_id, status, payload_json) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        run["run_id"],
                        item["subtask_id"],
                        item["job_id"],
                        item["status"],
                        canonical_json(item),
                    ),
                )
            for decision in run["arbitrations"]:
                existing_decision = self.one(
                    "SELECT payload_json FROM arbitrations WHERE signature=?",
                    (decision["signature"],),
                )
                if existing_decision is None:
                    conn.execute(
                        "INSERT INTO arbitrations"
                        "(decision_id, signature, plan_version, candidate_id, job_id, "
                        " deadline_clock_ms, priority, status, payload_json) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            decision["decision_id"],
                            decision["signature"],
                            decision["plan_version"],
                            decision["candidate_id"],
                            decision["job_id"],
                            decision["deadline_clock_ms"],
                            decision["priority"],
                            decision["status"],
                            canonical_json(decision),
                        ),
                    )
                else:
                    existing_payload = json.loads(existing_decision["payload_json"])
                    if existing_payload != decision:
                        conflict_id = "conf_" + digest_obj(
                            {
                                "signature": decision["signature"],
                                "incoming": decision,
                            }
                        )
                        conn.execute(
                            "INSERT OR IGNORE INTO arbitration_conflicts"
                            "(conflict_id, signature, incoming_json) VALUES(?, ?, ?)",
                            (
                                conflict_id,
                                decision["signature"],
                                canonical_json(decision),
                            ),
                        )
            recognized = self._recognized_arbitrations(
                run["plan_version"], None
            )
            return {"inserted": True, "run": run, "arbitrations": recognized}

    def _recognized_arbitrations(
        self, plan_version: str, candidate_id: str | None
    ) -> list[dict[str, Any]]:
        params: list[Any] = [plan_version]
        sql = "SELECT payload_json FROM arbitrations WHERE plan_version=?"
        if candidate_id is not None:
            sql += " AND candidate_id=?"
            params.append(candidate_id)
        sql += " ORDER BY deadline_clock_ms, priority, job_id, decision_id"
        return [json.loads(row["payload_json"]) for row in self.query(sql, params)]

    def recognized_arbitrations(
        self, plan_version: str | None = None
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        sql = "SELECT payload_json FROM arbitrations"
        if plan_version:
            sql += " WHERE plan_version=?"
            params.append(plan_version)
        sql += " ORDER BY deadline_clock_ms, priority, job_id, decision_id"
        return [json.loads(row["payload_json"]) for row in self.query(sql, params)]

    def arbitration_conflicts(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM arbitration_conflicts ORDER BY conflict_id"
        )
        return [
            {
                "conflict_id": row["conflict_id"],
                "signature": row["signature"],
                "incoming": json.loads(row["incoming_json"]),
            }
            for row in rows
        ]

    def save_closure(self, closure: dict[str, Any]) -> dict[str, Any]:
        fingerprint = closure["fingerprint"]
        with self.transaction() as conn:
            existing = self.one(
                "SELECT payload_json FROM invalidation_closures WHERE fingerprint=?",
                (fingerprint,),
            )
            if existing is not None:
                if existing["payload_json"]:
                    payload = json.loads(existing["payload_json"])
                else:
                    payload = dict(closure)
                payload["inserted"] = False
                return payload
            closure_id = "inv_" + fingerprint
            payload = {**closure, "closure_id": closure_id}
            conn.execute(
                "INSERT INTO invalidation_closures"
                "(closure_id, fingerprint, plan_version, reason, affected_json, "
                " reused_json, protected_json, details_json, payload_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    closure_id,
                    fingerprint,
                    closure["plan_version"],
                    closure["reason"],
                    canonical_json(closure["affected"]),
                    canonical_json(closure["reused"]),
                    canonical_json(closure["protected_committed"]),
                    canonical_json(closure.get("details", {})),
                    canonical_json(payload),
                ),
            )
            for item in closure.get("items", []):
                conn.execute(
                    "INSERT OR IGNORE INTO invalidation_items"
                    "(closure_id, subtask_id, disposition, reason) "
                    "VALUES(?, ?, ?, ?)",
                    (
                        closure_id,
                        item["subtask_id"],
                        item["disposition"],
                        item["reason"],
                    ),
                )
            payload["closure_id"] = closure_id
            payload["inserted"] = True
            return payload

    def closures(self, plan_version: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        sql = "SELECT * FROM invalidation_closures"
        if plan_version:
            sql += " WHERE plan_version=?"
            params.append(plan_version)
        sql += " ORDER BY plan_version, reason, closure_id"
        results: list[dict[str, Any]] = []
        for row in self.query(sql, params):
            closure = {
                "closure_id": row["closure_id"],
                "fingerprint": row["fingerprint"],
                "plan_version": row["plan_version"],
                "reason": row["reason"],
                "affected": json.loads(row["affected_json"]),
                "reused": json.loads(row["reused_json"]),
                "protected_committed": json.loads(row["protected_json"]),
                "details": json.loads(row["details_json"]),
                "items": [],
            }
            items = self.query(
                "SELECT subtask_id, disposition, reason FROM invalidation_items "
                "WHERE closure_id=? ORDER BY subtask_id",
                (row["closure_id"],),
            )
            closure["items"] = [dict(item) for item in items]
            results.append(closure)
        return results

    def pending_changes(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM change_log WHERE consumed=0 ORDER BY change_id"
        )
        return [
            {
                "change_id": row["change_id"],
                "kind": row["kind"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def consume_changes(self, change_ids: Iterable[str]) -> None:
        ids = sorted(set(change_ids))
        if not ids:
            return
        with self.transaction() as conn:
            for change_id in ids:
                conn.execute(
                    "UPDATE change_log SET consumed=1 WHERE change_id=?",
                    (change_id,),
                )

    def latest_schedule(self, plan_version: str) -> dict[str, Any] | None:
        row = self.one(
            "SELECT payload_json FROM schedule_runs "
            "WHERE plan_version=? ORDER BY rowid DESC LIMIT 1",
            (plan_version,),
        )
        return json.loads(row["payload_json"]) if row else None

    def schedule_runs(self, plan_version: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT payload_json FROM schedule_runs "
            "WHERE plan_version=? ORDER BY run_id",
            (plan_version,),
        )
        return [json.loads(row["payload_json"]) for row in rows]

    def late_arrivals(self, plan_version: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        sql = "SELECT * FROM late_arrivals"
        if plan_version:
            sql += " WHERE plan_version=?"
            params.append(plan_version)
        sql += " ORDER BY late_id"
        return [dict(row) for row in self.query(sql, params)]

    def ledger_for_job(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT subtask_id FROM ledger WHERE job_id=? ORDER BY commit_seq, subtask_id",
            (job_id,),
        )
        return [
            entry
            for entry in (self.ledger_entry(row["subtask_id"]) for row in rows)
            if entry is not None
        ]

    def ledger_for_plan(self, plan_version: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT l.subtask_id, s.ordinal, s.phase, s.shard_index "
            "FROM ledger l JOIN subtasks s ON s.subtask_id=l.subtask_id "
            "JOIN plan_subtasks p ON p.subtask_id=l.subtask_id "
            "WHERE p.plan_version=? "
            "ORDER BY l.job_id, s.ordinal, l.subtask_id",
            (plan_version,),
        )
        result = []
        for row in rows:
            entry = self.ledger_entry(row["subtask_id"])
            if entry is None:
                continue
            entry["ordinal"] = int(row["ordinal"])
            entry["phase"] = row["phase"]
            entry["shard_index"] = int(row["shard_index"])
            result.append(entry)
        return result

    def all_ledger(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT subtask_id FROM ledger ORDER BY job_id, commit_seq, subtask_id"
        )
        return [
            entry
            for entry in (self.ledger_entry(row["subtask_id"]) for row in rows)
            if entry is not None
        ]

    def duplicate_ledger_count(self) -> int:
        row = self.one(
            "SELECT COALESCE(SUM(c-1), 0) AS duplicates FROM "
            "(SELECT subtask_id, COUNT(*) AS c FROM ledger GROUP BY subtask_id "
            " HAVING COUNT(*) > 1)"
        )
        return int(row["duplicates"])

    def integrity_check(self) -> str:
        row = self.one("PRAGMA integrity_check")
        return str(row[0])

    def save_cost_evaluation(
        self, plan_version: str, evaluation: dict[str, Any]
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO cost_evaluations"
                "(plan_version, result_digest, selected_candidate_id, payload_json) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(plan_version) DO UPDATE SET "
                "result_digest=excluded.result_digest, "
                "selected_candidate_id=excluded.selected_candidate_id, "
                "payload_json=excluded.payload_json",
                (
                    plan_version,
                    evaluation.get("result_digest") or digest_obj(evaluation),
                    evaluation["selected_candidate_id"],
                    canonical_json(evaluation),
                ),
            )
            conn.execute(
                "DELETE FROM cost_forecasts WHERE plan_version=?",
                (plan_version,),
            )
            for candidate_id, result in evaluation["candidates"].items():
                for step in result["steps"]:
                    conn.execute(
                        "INSERT INTO cost_forecasts"
                        "(plan_version, candidate_id, aggregation_index, "
                        " subtask_id, payload_json) VALUES(?, ?, ?, ?, ?)",
                        (
                            plan_version,
                            candidate_id,
                            step["aggregation_index"],
                            step["subtask_id"],
                            canonical_json(step),
                        ),
                    )

    def cost_evaluation(
        self, plan_version: str
    ) -> dict[str, Any] | None:
        row = self.one(
            "SELECT payload_json FROM cost_evaluations WHERE plan_version=?",
            (plan_version,),
        )
        return json.loads(row["payload_json"]) if row else None

    def cost_forecasts(self, plan_version: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT payload_json FROM cost_forecasts "
            "WHERE plan_version=? ORDER BY candidate_id, aggregation_index",
            (plan_version,),
        )
        return [json.loads(row["payload_json"]) for row in rows]

    def clear_drift_events(self, plan_version: str) -> int:
        with self.transaction() as conn:
            before = conn.total_changes
            conn.execute(
                "DELETE FROM drift_events WHERE plan_version=?",
                (plan_version,),
            )
            return int(conn.total_changes - before)

