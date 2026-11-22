"""Transactional SQLite state, shape versioning, and cross-process locking."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from .canonical import digest_value
from .errors import BusinessError, ErrorCode
from .models import ExecutionShape, Scenario


class FileLock:
    """A small cross-platform exclusive file lock.

    POSIX uses ``flock``. Windows uses the first byte of the lock file through
    ``msvcrt.locking``. The OS releases the lock when a killed process exits.
    """

    def __init__(self, path: str | Path, timeout_s: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self._fh: Any = None

    def __enter__(self) -> FileLock:  # noqa: PYI034 - Python 3.10 compatibility
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                if sys.platform.startswith("win"):
                    import msvcrt

                    self._fh.seek(0)
                    if self._fh.tell() == 0:
                        self._fh.write(b"\0")
                        self._fh.flush()
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise BusinessError(
                        ErrorCode.CONCURRENCY_CONFLICT,
                        "timed out waiting for the workspace file lock",
                        details={"lock_path": str(self.path), "timeout_s": self.timeout_s},
                    )
                time.sleep(0.02)
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"pid={os.getpid()} acquired={time.time():.6f}\n".encode("ascii"))
            self._fh.flush()
        except OSError:
            pass
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE,
    plan_digest TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    seed INTEGER NOT NULL,
    tables_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    watermark_seq INTEGER NOT NULL,
    watermark_batch TEXT,
    current_version INTEGER NOT NULL,
    current_shape_digest TEXT NOT NULL,
    last_decision_ms INTEGER,
    drift_accumulated REAL NOT NULL DEFAULT 0,
    drift_max REAL NOT NULL DEFAULT 0,
    reopt_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shapes (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    content_digest TEXT NOT NULL,
    content_json TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, version)
);
CREATE TABLE IF NOT EXISTS staging_shapes (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    proposed_version INTEGER NOT NULL,
    parent_version INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, proposed_version)
);
CREATE TABLE IF NOT EXISTS batches (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    arrival_seq INTEGER NOT NULL,
    shape_version INTEGER NOT NULL,
    shape_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    output_digest TEXT NOT NULL,
    execution_digest TEXT NOT NULL,
    commit_digest TEXT NOT NULL,
    input_rows INTEGER NOT NULL,
    output_rows INTEGER NOT NULL,
    stage_rows INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    block_path TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, batch_id),
    UNIQUE (run_id, arrival_seq)
);
CREATE TABLE IF NOT EXISTS observations (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    arrival_seq INTEGER NOT NULL,
    join_id TEXT NOT NULL,
    record_digest TEXT NOT NULL,
    record_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, batch_id, join_id)
);
CREATE TABLE IF NOT EXISTS drift_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    arrival_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    arrival_seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_join
    ON observations(run_id, join_id, arrival_seq);
CREATE INDEX IF NOT EXISTS idx_events_batch
    ON drift_events(run_id, arrival_seq, event_type);
"""


class StateStore:
    def __init__(self, db_path: str | Path, lock_path: str | Path, *, lock_timeout_s: float = 30.0) -> None:
        self.db_path = Path(db_path)
        self.lock_path = Path(lock_path)
        self.lock_timeout_s = lock_timeout_s
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def lock(self) -> FileLock:
        return FileLock(self.lock_path, self.lock_timeout_s)

    def _ensure_schema(self) -> None:
        self.conn.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load(value: str | None) -> Any:
        return json.loads(value) if value is not None else None

    def initialize(self, scenario: Scenario) -> tuple[str, bool]:
        run_id = scenario.plan.plan_id
        plan_digest = digest_value(scenario.to_dict())
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE plan_id = ?", (scenario.plan.plan_id,)).fetchone()
            if row is not None:
                if row["plan_digest"] != plan_digest:
                    raise BusinessError(
                        ErrorCode.STATE_CORRUPT,
                        "plan_id is already bound to different plan content",
                        details={
                            "plan_id": scenario.plan.plan_id,
                            "stored_plan_digest": row["plan_digest"],
                            "incoming_plan_digest": plan_digest,
                        },
                    )
                return str(row["run_id"]), False
            conn.execute(
                """
                INSERT INTO runs(
                    run_id, plan_id, plan_digest, source_digest, seed,
                    tables_json, plan_json, stats_json, config_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    scenario.plan.plan_id,
                    plan_digest,
                    scenario.source_digest,
                    scenario.plan.seed,
                    self._json([table.to_dict() for table in scenario.tables]),
                    self._json(scenario.plan.to_dict()),
                    self._json(scenario.initial_stats.to_dict()),
                    self._json(scenario.config.to_dict()),
                    now,
                    now,
                ),
            )
            initial = scenario.initial_shape
            conn.execute(
                """
                INSERT INTO shapes(
                    run_id, version, parent_version, content_digest,
                    content_json, action, reason, created_at
                ) VALUES (?, ?, NULL, ?, ?, 'FULL_REPLACE', ?, ?)
                """,
                (
                    run_id,
                    initial.version,
                    initial.content_digest,
                    self._json(initial.to_dict()),
                    initial.reason,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO state(
                    run_id, watermark_seq, watermark_batch, current_version,
                    current_shape_digest, last_decision_ms,
                    drift_accumulated, drift_max, reopt_count, status
                ) VALUES (?, -1, NULL, ?, ?, NULL, 0, 0, 0, 'initialized')
                """,
                (run_id, initial.version, initial.content_digest),
            )
        return run_id, True

    def find_run(self, plan_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE plan_id = ?", (plan_id,)).fetchone()

    def require_run(self, plan_id: str) -> str:
        row = self.find_run(plan_id)
        if row is None:
            raise BusinessError(
                ErrorCode.NOT_FOUND,
                f"no persisted state for plan {plan_id}",
                details={"plan_id": plan_id, "db_path": str(self.db_path)},
            )
        return str(row["run_id"])

    def get_state(self, run_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM state WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise BusinessError(
                ErrorCode.STATE_CORRUPT,
                "state row is missing",
                details={"run_id": run_id},
            )
        return dict(row)

    def get_shape(self, run_id: str, version: int | None = None) -> ExecutionShape:
        if version is None:
            state = self.get_state(run_id)
            version = int(state["current_version"])
        row = self.conn.execute(
            "SELECT * FROM shapes WHERE run_id = ? AND version = ?",
            (run_id, version),
        ).fetchone()
        if row is None:
            raise BusinessError(
                ErrorCode.STATE_CORRUPT,
                "current shape version is missing",
                details={"run_id": run_id, "version": version},
            )
        data = json.loads(row["content_json"])
        shape = ExecutionShape(
            version=int(row["version"]),
            join_order=tuple(data["join_order"]),
            algorithms=tuple(data["algorithms"]),
            reason=str(data.get("reason", row["reason"])),
        )
        if shape.content_digest != row["content_digest"]:
            raise BusinessError(
                ErrorCode.STATE_CORRUPT,
                "shape content digest does not match stored digest",
                details={"run_id": run_id, "version": version},
            )
        return shape

    def list_shapes(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM shapes WHERE run_id = ? ORDER BY version",
            (run_id,),
        ).fetchall()
        return [
            {
                "version": row["version"],
                "parent_version": row["parent_version"],
                "content_digest": row["content_digest"],
                "action": row["action"],
                "reason": row["reason"],
                "content": json.loads(row["content_json"]),
            }
            for row in rows
        ]

    def cleanup_staging(self, run_id: str) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            orphaned = self._staging_rows(conn, run_id)
            conn.execute("DELETE FROM staging_shapes WHERE run_id = ?", (run_id,))
        return orphaned

    @staticmethod
    def _staging_rows(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM staging_shapes WHERE run_id = ? ORDER BY proposed_version",
            (run_id,),
        ).fetchall()
        return [
            {
                "proposed_version": row["proposed_version"],
                "parent_version": row["parent_version"],
                "content_digest": row["content_digest"],
                "status": "ORPHANED_HALF_COMMIT",
            }
            for row in rows
        ]

    def list_staging(self, run_id: str) -> list[dict[str, Any]]:
        return self._staging_rows(self.conn, run_id)

    def validate_locked(self, run_id: str) -> tuple[dict[str, Any], ExecutionShape, list[dict[str, Any]]]:
        """Validate state while the caller already owns the workspace lock."""

        state = self.get_state(run_id)
        shape = self.get_shape(run_id)
        batches = self.list_batches(run_id)
        if batches:
            sequences = [int(batch["arrival_seq"]) for batch in batches]
            if sequences != list(range(len(sequences))):
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "committed batch sequence contains a gap",
                    details={"sequences": sequences},
                )
            max_seq = max(sequences)
            if max_seq != int(state["watermark_seq"]):
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "batch table and watermark disagree",
                    details={"watermark": state["watermark_seq"], "max_batch_seq": max_seq},
                )
        if shape.content_digest != state["current_shape_digest"]:
            raise BusinessError(
                ErrorCode.STATE_CORRUPT,
                "current shape pointer digest is inconsistent",
                details={"run_id": run_id},
            )
        return state, shape, batches

    def recover(self, run_id: str) -> dict[str, Any]:
        with self.lock():
            staging = self.cleanup_staging(run_id)
            state, shape, _ = self.validate_locked(run_id)
            return {
                "run_id": run_id,
                "watermark_seq": state["watermark_seq"],
                "watermark_batch": state["watermark_batch"],
                "current_version": shape.version,
                "current_shape_digest": shape.content_digest,
                "orphaned_staging": staging,
                "recovered": bool(staging),
            }

    def stage_shape(self, run_id: str, proposed: ExecutionShape) -> None:
        with self._transaction() as conn:
            state = conn.execute("SELECT * FROM state WHERE run_id = ?", (run_id,)).fetchone()
            if state is None:
                raise BusinessError(ErrorCode.STATE_CORRUPT, "state row is missing", details={"run_id": run_id})
            if proposed.version <= int(state["current_version"]):
                raise BusinessError(
                    ErrorCode.FORM_VERSION_REGRESSION,
                    "cannot stage a shape version that is not newer than the current version",
                    details={
                        "current_version": state["current_version"],
                        "proposed_version": proposed.version,
                    },
                )
            conn.execute(
                """
                INSERT INTO staging_shapes(
                    run_id, proposed_version, parent_version,
                    content_digest, content_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, proposed_version) DO UPDATE SET
                    parent_version = excluded.parent_version,
                    content_digest = excluded.content_digest,
                    content_json = excluded.content_json,
                    created_at = excluded.created_at
                """,
                (
                    run_id,
                    proposed.version,
                    int(state["current_version"]),
                    proposed.content_digest,
                    self._json(proposed.to_dict()),
                    time.time(),
                ),
            )

    def append_shadow_shape(self, run_id: str, shape: ExecutionShape) -> dict[str, Any]:
        """Append an inactive candidate version; current pointer is untouched."""

        with self.lock(), self._transaction() as conn:
                state = conn.execute("SELECT * FROM state WHERE run_id = ?", (run_id,)).fetchone()
                if state is None:
                    raise BusinessError(ErrorCode.STATE_CORRUPT, "state row is missing", details={"run_id": run_id})
                current = self.get_shape(run_id)
                if shape.content_digest == current.content_digest:
                    return {"action": "NOOP", "version": current.version, "digest": current.content_digest}
                if shape.version <= int(state["current_version"]):
                    raise BusinessError(
                        ErrorCode.FORM_VERSION_REGRESSION,
                        "shape version regression is forbidden",
                        details={
                            "current_version": state["current_version"],
                            "proposed_version": shape.version,
                        },
                    )
                conn.execute(
                    """
                    INSERT INTO shapes(
                        run_id, version, parent_version, content_digest,
                        content_json, action, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'INCREMENTAL_APPEND', ?, ?)
                    """,
                    (
                        run_id,
                        shape.version,
                        int(state["current_version"]),
                        shape.content_digest,
                        self._json(shape.to_dict()),
                        shape.reason,
                        time.time(),
                    ),
                )
                return {
                    "action": "INCREMENTAL_APPEND",
                    "version": shape.version,
                    "digest": shape.content_digest,
                    "current_version": int(state["current_version"]),
                }

    def commit_batch(
        self,
        *,
        run_id: str,
        result: Any,
        observation_records: Sequence[Mapping[str, Any]],
        assessment: Mapping[str, Any],
        decision: Mapping[str, Any] | None,
        expected_seq: int,
        expected_shape_version: int,
        commit_digest: str,
        block_path: str | None,
    ) -> dict[str, Any]:
        now = time.time()
        batch = result.batch
        with self._transaction() as conn:
            state = conn.execute("SELECT * FROM state WHERE run_id = ?", (run_id,)).fetchone()
            if state is None:
                raise BusinessError(ErrorCode.STATE_CORRUPT, "state row is missing", details={"run_id": run_id})
            existing = conn.execute(
                "SELECT * FROM batches WHERE run_id = ? AND batch_id = ?",
                (run_id, batch.batch_id),
            ).fetchone()
            if existing is not None:
                if existing["commit_digest"] == commit_digest:
                    return {
                        "duplicate": True,
                        "batch_id": batch.batch_id,
                        "watermark_seq": int(existing["arrival_seq"]),
                        "shape_version": int(existing["shape_version"]),
                    }
                raise BusinessError(
                    ErrorCode.BATCH_DIGEST_MISMATCH,
                    "batch was already committed with a different digest",
                    details={
                        "batch_id": batch.batch_id,
                        "stored_commit_digest": existing["commit_digest"],
                        "incoming_commit_digest": commit_digest,
                    },
                )
            if int(state["current_version"]) != expected_shape_version:
                raise BusinessError(
                    ErrorCode.CONCURRENCY_CONFLICT,
                    "current shape changed before batch commit",
                    details={
                        "expected_shape_version": expected_shape_version,
                        "actual_shape_version": state["current_version"],
                    },
                )
            if str(state["current_shape_digest"]) != result.shape.content_digest:
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "executed shape does not match current shape pointer",
                    details={
                        "executed_digest": result.shape.content_digest,
                        "current_digest": state["current_shape_digest"],
                    },
                )
            if int(state["watermark_seq"]) + 1 != expected_seq or batch.arrival_seq != expected_seq:
                raise BusinessError(
                    ErrorCode.WATERMARK_MISMATCH,
                    "batch cannot advance a non-contiguous watermark",
                    details={
                        "current_watermark": state["watermark_seq"],
                        "expected_seq": expected_seq,
                        "batch_seq": batch.arrival_seq,
                    },
                )
            input_rows = sum(item["input_rows"] for item in result.stage_dicts())
            summary = result.summary()
            conn.execute(
                """
                INSERT INTO batches(
                    run_id, batch_id, arrival_seq, shape_version, shape_digest,
                    input_digest, output_digest, execution_digest, commit_digest,
                    input_rows, output_rows, stage_rows, summary_json,
                    block_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    batch.batch_id,
                    batch.arrival_seq,
                    result.shape.version,
                    result.shape.content_digest,
                    result.input_digest,
                    result.output_digest,
                    result.execution_digest,
                    commit_digest,
                    input_rows,
                    result.output_rows,
                    sum(stage.output_rows for stage in result.stages),
                    self._json(summary),
                    block_path,
                    now,
                ),
            )
            for record in observation_records:
                record_json = dict(record)
                record_json["arrival_seq"] = batch.arrival_seq
                record_digest = digest_value(record_json)
                conn.execute(
                    """
                    INSERT INTO observations(
                        run_id, batch_id, arrival_seq, join_id,
                        record_digest, record_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        batch.batch_id,
                        batch.arrival_seq,
                        str(record["join_id"]),
                        record_digest,
                        self._json(record_json),
                        now,
                    ),
                )
            conn.execute(
                """
                INSERT INTO drift_events(
                    run_id, batch_id, arrival_seq, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, 'drift_assessment', ?, ?)
                """,
                (run_id, batch.batch_id, batch.arrival_seq, self._json(assessment), now),
            )
            if assessment.get("triggered"):
                event_type = "suppressed_reoptimization" if assessment.get("suppressed") else "drift_trigger"
                conn.execute(
                    """
                    INSERT INTO drift_events(
                        run_id, batch_id, arrival_seq, event_type, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, batch.batch_id, batch.arrival_seq, event_type, self._json(assessment), now),
                )
            current_version = int(state["current_version"])
            current_digest = str(state["current_shape_digest"])
            reopt_count = int(state["reopt_count"])
            if decision is not None:
                conn.execute(
                    """
                    INSERT INTO decisions(
                        run_id, batch_id, arrival_seq, status, error_code,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        batch.batch_id,
                        batch.arrival_seq,
                        str(decision.get("status")),
                        decision.get("error_code"),
                        self._json(decision),
                        now,
                    ),
                )
                if decision.get("status") == "SWITCHED":
                    selected_data = decision["selected"]
                    proposed = ExecutionShape(
                        version=int(selected_data["version"]),
                        join_order=tuple(selected_data["join_order"]),
                        algorithms=tuple(selected_data["algorithms"]),
                        reason=str(selected_data.get("reason", "drift_reoptimization")),
                    )
                    if proposed.version != current_version + 1:
                        raise BusinessError(
                            ErrorCode.FORM_VERSION_REGRESSION,
                            "new shape version must be exactly current_version + 1",
                            details={
                                "current_version": current_version,
                                "proposed_version": proposed.version,
                            },
                        )
                    staging = conn.execute(
                        """
                        SELECT * FROM staging_shapes
                        WHERE run_id = ? AND proposed_version = ?
                        """,
                        (run_id, proposed.version),
                    ).fetchone()
                    if staging is not None and staging["content_digest"] != proposed.content_digest:
                        raise BusinessError(
                            ErrorCode.STATE_CORRUPT,
                            "staged shape content differs from selected shape",
                            details={"proposed_version": proposed.version},
                        )
                    conn.execute(
                        """
                        INSERT INTO shapes(
                            run_id, version, parent_version, content_digest,
                            content_json, action, reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'FULL_REPLACE', ?, ?)
                        """,
                        (
                            run_id,
                            proposed.version,
                            current_version,
                            proposed.content_digest,
                            self._json(proposed.to_dict()),
                            proposed.reason,
                            now,
                        ),
                    )
                    current_version = proposed.version
                    current_digest = proposed.content_digest
                    reopt_count += 1
                conn.execute("DELETE FROM staging_shapes WHERE run_id = ?", (run_id,))
            status = "running"
            last_decision_ms = state["last_decision_ms"]
            if assessment.get("triggered") and not assessment.get("suppressed"):
                last_decision_ms = int(assessment["logical_time_ms"])
            conn.execute(
                """
                UPDATE state SET
                    watermark_seq = ?,
                    watermark_batch = ?,
                    current_version = ?,
                    current_shape_digest = ?,
                    last_decision_ms = ?,
                    drift_accumulated = drift_accumulated + ?,
                    drift_max = MAX(drift_max, ?),
                    reopt_count = ?,
                    status = ?
                WHERE run_id = ?
                """,
                (
                    batch.arrival_seq,
                    batch.batch_id,
                    current_version,
                    current_digest,
                    last_decision_ms,
                    float(assessment.get("drift_increment", 0.0)),
                    float(assessment.get("max_deviation_ratio", 0.0)),
                    reopt_count,
                    status,
                    run_id,
                ),
            )
            conn.execute(
                "UPDATE runs SET updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            return {
                "duplicate": False,
                "batch_id": batch.batch_id,
                "watermark_seq": batch.arrival_seq,
                "shape_version": current_version,
                "shape_digest": current_digest,
            }

    def list_batches(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM batches WHERE run_id = ? ORDER BY arrival_seq",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_batch(self, run_id: str, batch_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM batches WHERE run_id = ? AND batch_id = ?",
            (run_id, batch_id),
        ).fetchone()
        return dict(row) if row else None

    def list_observations(self, run_id: str, *, before_seq: int | None = None) -> list[dict[str, Any]]:
        if before_seq is None:
            rows = self.conn.execute(
                "SELECT record_json FROM observations WHERE run_id = ? ORDER BY arrival_seq, batch_id, join_id",
                (run_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT record_json FROM observations
                WHERE run_id = ? AND arrival_seq <= ?
                ORDER BY arrival_seq, batch_id, join_id
                """,
                (run_id, before_seq),
            ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def list_events(self, run_id: str, event_type: str | None = None) -> list[dict[str, Any]]:
        if event_type is None:
            rows = self.conn.execute(
                "SELECT * FROM drift_events WHERE run_id = ? ORDER BY event_id",
                (run_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM drift_events WHERE run_id = ? AND event_type = ? ORDER BY event_id",
                (run_id, event_type),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "batch_id": row["batch_id"],
                "arrival_seq": row["arrival_seq"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def list_decisions(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE run_id = ? ORDER BY decision_id",
            (run_id,),
        ).fetchall()
        return [
            {
                "decision_id": row["decision_id"],
                "batch_id": row["batch_id"],
                "arrival_seq": row["arrival_seq"],
                "status": row["status"],
                "error_code": row["error_code"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def status(self, run_id: str) -> dict[str, Any]:
        run = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise BusinessError(ErrorCode.NOT_FOUND, "run not found", details={"run_id": run_id})
        state = self.get_state(run_id)
        shape = self.get_shape(run_id)
        batches = self.list_batches(run_id)
        events = self.list_events(run_id)
        decisions = self.list_decisions(run_id)
        return {
            "run_id": run_id,
            "plan_id": run["plan_id"],
            "plan_digest": run["plan_digest"],
            "watermark_seq": state["watermark_seq"],
            "watermark_batch": state["watermark_batch"],
            "committed_batches": len(batches),
            "next_arrival_seq": int(state["watermark_seq"]) + 1,
            "current_shape": shape.to_dict(),
            "shape_versions": self.list_shapes(run_id),
            "drift": {
                "accumulated": state["drift_accumulated"],
                "max_deviation_ratio": state["drift_max"],
                "last_decision_ms": state["last_decision_ms"],
                "reopt_count": state["reopt_count"],
            },
            "counts": {
                "drift_triggers": sum(1 for event in events if event["event_type"] == "drift_trigger"),
                "suppressed": sum(1 for event in events if event["event_type"] == "suppressed_reoptimization"),
                "decisions": len(decisions),
                "switch_budget_errors": sum(
                    1 for decision in decisions if decision["error_code"] == ErrorCode.SWITCH_BUDGET
                ),
            },
            "orphaned_staging": self.list_staging(run_id),
            "last_events": events[-5:],
        }

    @staticmethod
    def _compare_batch_rows(
        left_rows: Sequence[Mapping[str, Any]],
        right_rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        left = {row["batch_id"]: row for row in left_rows}
        right = {row["batch_id"]: row for row in right_rows}
        mismatches: list[dict[str, Any]] = []
        for batch_id in sorted(set(left) | set(right)):
            a = left.get(batch_id)
            b = right.get(batch_id)
            if a is None or b is None:
                mismatches.append(
                    {
                        "batch_id": batch_id,
                        "field": "presence",
                        "left": a is not None,
                        "right": b is not None,
                    }
                )
                continue
            for field in (
                "arrival_seq",
                "shape_version",
                "shape_digest",
                "input_digest",
                "output_digest",
                "output_rows",
                "execution_digest",
                "commit_digest",
            ):
                if a[field] != b[field]:
                    mismatches.append(
                        {
                            "batch_id": batch_id,
                            "field": field,
                            "left": a[field],
                            "right": b[field],
                        }
                    )
        return mismatches

    def compare_batches(
        self,
        left_run_id: str,
        right_run_id: str,
    ) -> list[dict[str, Any]]:
        return self._compare_batch_rows(
            self.list_batches(left_run_id),
            self.list_batches(right_run_id),
        )

    def compare_batches_with(
        self,
        left_run_id: str,
        other: StateStore,
        right_run_id: str,
    ) -> list[dict[str, Any]]:
        return self._compare_batch_rows(
            self.list_batches(left_run_id),
            other.list_batches(right_run_id),
        )

