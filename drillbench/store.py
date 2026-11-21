from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError
from .filelock import NonBlockingFileLock
from .models import (
    DEPENDENCY_SATISFIED_STAGES,
    FINAL_STAGES,
    OUTCOME_NOT_STARTED,
    OUTCOME_SKIPPED_MANUALLY,
    STAGE_PENDING,
    STAGE_RUNNING,
    STAGE_SKIPPED,
    Snapshot,
    StepObservation,
)
from .util import canonical_json, digest_value, sha256_text


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    version INTEGER PRIMARY KEY,
    parent_version INTEGER,
    plan_name TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    steps_digest TEXT NOT NULL,
    services_digest TEXT NOT NULL,
    clock_digest TEXT NOT NULL,
    injections_digest TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    source_digests_json TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    created_order INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL REFERENCES snapshots(version),
    updated_commit_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    plan_name TEXT NOT NULL,
    version INTEGER NOT NULL REFERENCES snapshots(version),
    state TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    observation_digest TEXT NOT NULL,
    injection_watermark INTEGER NOT NULL,
    commit_seq INTEGER NOT NULL,
    commit_head TEXT NOT NULL,
    recovery_count INTEGER NOT NULL,
    start_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    stage TEXT NOT NULL,
    outcome_code TEXT NOT NULL,
    outcome_class TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    started_at_ms INTEGER,
    ended_at_ms INTEGER,
    result_digest TEXT,
    recovery_count INTEGER NOT NULL,
    PRIMARY KEY (run_id, version, seq),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    step_seq INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    code TEXT NOT NULL,
    outcome_class TEXT NOT NULL,
    stage TEXT NOT NULL,
    event TEXT,
    response_status INTEGER,
    body_sha256 TEXT,
    injected_at_ms INTEGER,
    injections_json TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    UNIQUE (run_id, version, step_seq, attempt_no),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS step_claims (
    version INTEGER NOT NULL,
    step_seq INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_at_ms INTEGER NOT NULL,
    PRIMARY KEY (version, step_seq)
);

CREATE TABLE IF NOT EXISTS injection_consumptions (
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    injection_id TEXT NOT NULL,
    step_seq INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    at_ms INTEGER NOT NULL,
    consumed_at_ms INTEGER NOT NULL,
    outcome_code TEXT NOT NULL,
    PRIMARY KEY (run_id, version, injection_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS commits (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    version INTEGER NOT NULL,
    step_seq INTEGER,
    action TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    observation_digest TEXT NOT NULL,
    injection_watermark INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    commit_hash TEXT NOT NULL,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS recovery_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    step_seq INTEGER,
    action TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_observations_run
ON observations(run_id, version, step_seq, attempt_no);

CREATE INDEX IF NOT EXISTS idx_commits_run
ON commits(run_id, seq);
"""


def genesis_hash(run_id: str, version: int) -> str:
    return sha256_text(f"drillbench-genesis:{run_id}:{version}")


class StateStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.db_path.parent / (self.db_path.name + ".locks")
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=0.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        busy_ms = int(os.environ.get("DRILLBENCH_DB_BUSY_MS", "50"))
        connection.execute(f"PRAGMA busy_timeout = {max(0, busy_ms)}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def init_schema(self) -> None:
        connection = self._connect()
        try:
            try:
                current_mode = connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
                if str(current_mode).lower() != "wal":
                    connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError as exc:
                self._raise_if_locked(exc)
                raise
            connection.executescript(SCHEMA_SQL)
            self._schema_ready = True
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        try:
            with self.connect() as connection:
                row = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'runs'
                    """
                ).fetchone()
                if row is not None:
                    self._schema_ready = True
                    return
        except sqlite3.OperationalError as exc:
            self._raise_if_locked(exc)
            raise
        self.init_schema()

    @contextmanager
    def _transaction(
        self,
        *,
        immediate: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            except sqlite3.OperationalError as exc:
                self._raise_if_locked(exc)
                raise
            try:
                yield connection
                connection.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                self._raise_if_locked(exc)
                raise
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
        finally:
            connection.close()

    @staticmethod
    def _raise_if_locked(exc: sqlite3.OperationalError) -> None:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            raise BusinessError(
                "E_LOCKED",
                "SQLite state is locked by another drill process",
                {"reason": str(exc)},
            ) from exc

    def load_lock(self) -> NonBlockingFileLock:
        return NonBlockingFileLock(self.lock_dir / "snapshot-load.lock")

    def step_lock(self, version: int, step_seq: int) -> NonBlockingFileLock:
        return NonBlockingFileLock(
            self.lock_dir / f"step-v{version}-{step_seq:04d}.lock"
        )

    def _pointer_version(self, connection: sqlite3.Connection) -> int | None:
        row = connection.execute(
            "SELECT version FROM pointer WHERE id = 1"
        ).fetchone()
        return int(row["version"]) if row is not None else None

    def current_version(self) -> int | None:
        with self.connect() as connection:
            return self._pointer_version(connection)

    def current_snapshot_info(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            version = self._pointer_version(connection)
            if version is None:
                return None
            row = connection.execute(
                "SELECT * FROM snapshots WHERE version = ?",
                (version,),
            ).fetchone()
            if row is None:
                raise BusinessError(
                    "E_VERIFY_FAILED",
                    "current snapshot pointer references a missing version",
                    {"version": version},
                )
            return self._snapshot_row_info(row)

    @staticmethod
    def _snapshot_row_info(row: sqlite3.Row) -> dict[str, Any]:
        snapshot_json = json.loads(row["snapshot_json"])
        return {
            "version": int(row["version"]),
            "parent_version": (
                int(row["parent_version"])
                if row["parent_version"] is not None
                else None
            ),
            "plan": row["plan_name"],
            "change_kind": row["change_kind"],
            "content_digest": row["content_digest"],
            "plan_digest": row["plan_digest"],
            "steps_digest": row["steps_digest"],
            "services_digest": row["services_digest"],
            "clock_digest": row["clock_digest"],
            "injections_digest": row["injections_digest"],
            "source_digests": json.loads(row["source_digests_json"]),
            "snapshot": snapshot_json,
        }

    def _snapshot_for_version(
        self,
        connection: sqlite3.Connection,
        version: int,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM snapshots WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            raise BusinessError(
                "E_VERIFY_FAILED",
                "snapshot version is missing",
                {"version": version},
            )
        return self._snapshot_row_info(row)

    @staticmethod
    def _snapshot_from_info(info: dict[str, Any]) -> Snapshot:
        data = info["snapshot"]
        from .validation import _parse_clock, _parse_injections, _parse_routes  # local
        from .validation import _parse_steps
        from .models import Plan

        plan_data = data["plan"]
        plan = Plan(
            name=plan_data["name"],
            steps=_parse_steps(plan_data["steps"], plan_name=plan_data["name"]),
        )
        services_doc = {
            "schema_version": 1,
            "plans": [{"name": plan.name, "routes": data["routes"]}],
        }
        clock_doc = {
            "schema_version": 1,
            "plans": [
                {
                    "name": plan.name,
                    **data["clock"],
                }
            ],
        }
        injections_doc = {
            "schema_version": 1,
            "plans": [
                {
                    "name": plan.name,
                    "injections": data["injections"],
                }
            ],
        }
        routes = _parse_routes(services_doc, plan)
        clock = _parse_clock(clock_doc, plan)
        injections = _parse_injections(injections_doc, plan, clock)
        return Snapshot(
            plan=plan,
            routes=routes,
            clock=clock,
            injections=injections,
            source_digests=dict(data.get("source_digests", {})),
            content_digest=info["content_digest"],
            plan_digest=info["plan_digest"],
            steps_digest=info["steps_digest"],
            services_digest=info["services_digest"],
            clock_digest=info["clock_digest"],
            injections_digest=info["injections_digest"],
        )

    def get_snapshot(self, version: int) -> tuple[dict[str, Any], Snapshot]:
        with self.connect() as connection:
            info = self._snapshot_for_version(connection, version)
        return info, self._snapshot_from_info(info)

    def install_snapshot(
        self,
        snapshot: Snapshot,
        *,
        requested_version: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self.load_lock():
            with self._transaction() as connection:
                current_version = self._pointer_version(connection)
                current_info: dict[str, Any] | None = None
                if current_version is not None:
                    current_info = self._snapshot_for_version(
                        connection, current_version
                    )

                old_snapshot_json = (
                    current_info["snapshot"] if current_info is not None else None
                )
                old_step_seqs = (
                    {
                        int(item["seq"])
                        for item in old_snapshot_json["plan"]["steps"]
                    }
                    if old_snapshot_json is not None
                    else set()
                )
                new_step_seqs = {step.seq for step in snapshot.plan.steps}
                old_injection_ids = (
                    {
                        item["id"]
                        for item in old_snapshot_json.get("injections", [])
                    }
                    if old_snapshot_json is not None
                    else set()
                )
                new_injection_ids = {
                    injection.injection_id for injection in snapshot.injections
                }
                comparison = {
                    "previous_version": current_version,
                    "previous_content_digest": (
                        current_info["content_digest"]
                        if current_info is not None
                        else None
                    ),
                    "new_content_digest": snapshot.content_digest,
                    "previous_steps_digest": (
                        current_info["steps_digest"]
                        if current_info is not None
                        else None
                    ),
                    "new_steps_digest": snapshot.steps_digest,
                    "previous_injections_digest": (
                        current_info["injections_digest"]
                        if current_info is not None
                        else None
                    ),
                    "new_injections_digest": snapshot.injections_digest,
                    "content_digest_changed": (
                        current_info is None
                        or current_info["content_digest"]
                        != snapshot.content_digest
                    ),
                    "steps_digest_changed": (
                        current_info is None
                        or current_info["steps_digest"] != snapshot.steps_digest
                    ),
                    "injections_digest_changed": (
                        current_info is None
                        or current_info["injections_digest"]
                        != snapshot.injections_digest
                    ),
                    "services_digest_changed": (
                        current_info is None
                        or current_info["services_digest"]
                        != snapshot.services_digest
                    ),
                    "clock_digest_changed": (
                        current_info is None
                        or current_info["clock_digest"] != snapshot.clock_digest
                    ),
                    "added_steps": sorted(new_step_seqs - old_step_seqs),
                    "removed_steps": sorted(old_step_seqs - new_step_seqs),
                    "added_injections": sorted(
                        new_injection_ids - old_injection_ids
                    ),
                    "removed_injections": sorted(
                        old_injection_ids - new_injection_ids
                    ),
                    "injection_relation": (
                        "INITIAL"
                        if current_info is None
                        else (
                            "UNCHANGED"
                            if current_info["injections_digest"]
                            == snapshot.injections_digest
                            else "REPLACE_OR_MODIFY"
                        )
                    ),
                }

                if (
                    requested_version is not None
                    and current_version is not None
                    and requested_version <= current_version
                ):
                    is_same_noop = (
                        requested_version == current_version
                        and current_info is not None
                        and current_info["content_digest"]
                        == snapshot.content_digest
                    )
                    if not is_same_noop:
                        raise BusinessError(
                            "E_VERSION_ROLLBACK",
                            "version rollback or overwrite is not allowed",
                            {
                                "current_version": current_version,
                                "requested_version": requested_version,
                            },
                        )

                if (
                    current_info is not None
                    and current_info["content_digest"] == snapshot.content_digest
                ):
                    return {
                        "changed": False,
                        "change_kind": "NOOP",
                        "current_version": current_version,
                        "version": current_version,
                        "plan": snapshot.plan.name,
                        "content_digest": snapshot.content_digest,
                        "steps_digest": snapshot.steps_digest,
                        "injections_digest": snapshot.injections_digest,
                        "source_digests": snapshot.source_digests,
                        "comparison": comparison,
                    }

                if current_version is not None:
                    prior_match = connection.execute(
                        """
                        SELECT version FROM snapshots
                        WHERE content_digest = ? AND version < ?
                        ORDER BY version DESC LIMIT 1
                        """,
                        (snapshot.content_digest, current_version),
                    ).fetchone()
                    if prior_match is not None:
                        raise BusinessError(
                            "E_VERSION_ROLLBACK",
                            "snapshot content would roll back to an older version",
                            {
                                "current_version": current_version,
                                "matching_old_version": int(
                                    prior_match["version"]
                                ),
                            },
                        )

                if requested_version is not None and requested_version == current_version:
                    raise BusinessError(
                        "E_VERSION_ROLLBACK",
                        "changing an existing version in place is not allowed",
                        {
                            "current_version": current_version,
                            "requested_version": requested_version,
                        },
                    )

                change_kind = "FULL_REPLACEMENT"
                if current_info is not None:
                    old_snapshot = current_info["snapshot"]
                    if (
                        old_snapshot["plan"]["name"] == snapshot.plan.name
                        and current_info["steps_digest"] == snapshot.steps_digest
                        and current_info["services_digest"]
                        == snapshot.services_digest
                        and current_info["clock_digest"] == snapshot.clock_digest
                    ):
                        old_injections = {
                            item["id"]: item
                            for item in old_snapshot.get("injections", [])
                        }
                        new_injections = {
                            item.injection_id: item.to_dict()
                            for item in snapshot.injections
                        }
                        if (
                            all(
                                key in new_injections
                                and new_injections[key] == value
                                for key, value in old_injections.items()
                            )
                            and len(new_injections) > len(old_injections)
                        ):
                            change_kind = "INCREMENTAL_APPEND"
                            comparison["injection_relation"] = "APPEND"

                next_version = (
                    requested_version
                    if requested_version is not None
                    else (current_version or 0) + 1
                )
                if next_version < 1:
                    raise BusinessError(
                        "E_VERSION_ROLLBACK",
                        "version must be a positive integer",
                        {"requested_version": next_version},
                    )

                connection.execute("DROP TABLE IF EXISTS temp.drill_load_staging")
                connection.execute(
                    """
                    CREATE TEMP TABLE drill_load_staging (
                        version INTEGER NOT NULL,
                        plan_name TEXT NOT NULL,
                        content_digest TEXT NOT NULL,
                        plan_digest TEXT NOT NULL,
                        steps_digest TEXT NOT NULL,
                        services_digest TEXT NOT NULL,
                        clock_digest TEXT NOT NULL,
                        injections_digest TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        source_digests_json TEXT NOT NULL,
                        change_kind TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO drill_load_staging VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_version,
                        snapshot.plan.name,
                        snapshot.content_digest,
                        snapshot.plan_digest,
                        snapshot.steps_digest,
                        snapshot.services_digest,
                        snapshot.clock_digest,
                        snapshot.injections_digest,
                        canonical_json(snapshot.to_json_dict()),
                        canonical_json(dict(sorted(snapshot.source_digests.items()))),
                        change_kind,
                    ),
                )
                created_order_row = connection.execute(
                    "SELECT COALESCE(MAX(created_order), 0) + 1 AS next_order FROM snapshots"
                ).fetchone()
                created_order = int(created_order_row["next_order"])
                connection.execute(
                    """
                    INSERT INTO snapshots (
                        version, parent_version, plan_name, content_digest,
                        plan_digest, steps_digest, services_digest, clock_digest,
                        injections_digest, snapshot_json, source_digests_json,
                        change_kind, created_order
                    )
                    SELECT version, ?, plan_name, content_digest, plan_digest,
                           steps_digest, services_digest, clock_digest,
                           injections_digest, snapshot_json, source_digests_json,
                           change_kind, ?
                    FROM drill_load_staging
                    WHERE version = ?
                    """,
                    (current_version, created_order, next_version),
                )
                connection.execute(
                    """
                    INSERT INTO pointer (id, version, updated_commit_seq)
                    VALUES (1, ?, 0)
                    ON CONFLICT(id) DO UPDATE SET
                        version = excluded.version,
                        updated_commit_seq = pointer.updated_commit_seq + 1
                    """,
                    (next_version,),
                )
                connection.execute("DROP TABLE temp.drill_load_staging")
                return {
                    "changed": True,
                    "change_kind": change_kind,
                    "current_version": next_version,
                    "version": next_version,
                    "parent_version": current_version,
                    "plan": snapshot.plan.name,
                    "content_digest": snapshot.content_digest,
                    "steps_digest": snapshot.steps_digest,
                    "injections_digest": snapshot.injections_digest,
                    "source_digests": snapshot.source_digests,
                    "comparison": comparison,
                }

    def ensure_run(self, *, run_id: str, plan_name: str | None) -> dict[str, Any]:
        self.ensure_schema()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if plan_name is not None and existing["plan_name"] != plan_name:
                    raise BusinessError(
                        "E_BAD_PLAN",
                        "run id is bound to a different plan",
                        {
                            "run_id": run_id,
                            "run_plan": existing["plan_name"],
                            "requested_plan": plan_name,
                        },
                    )
                return {"created": False, "run": dict(existing)}

            current_version = self._pointer_version(connection)
            if current_version is None:
                raise BusinessError(
                    "E_NO_SNAPSHOT",
                    "no drill snapshot is loaded",
                )
            info = self._snapshot_for_version(connection, current_version)
            if plan_name is not None and info["plan"] != plan_name:
                raise BusinessError(
                    "E_BAD_PLAN",
                    "loaded snapshot belongs to a different plan",
                    {"loaded_plan": info["plan"], "requested_plan": plan_name},
                )
            snapshot = self._snapshot_from_info(info)
            now = snapshot.clock.start_ms
            empty_digest = digest_value([])
            head = genesis_hash(run_id, current_version)
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, plan_name, version, state, logical_clock_ms,
                    observation_digest, injection_watermark, commit_seq,
                    commit_head, recovery_count, start_clock_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    snapshot.plan.name,
                    current_version,
                    "READY",
                    now,
                    empty_digest,
                    0,
                    0,
                    head,
                    0,
                    now,
                ),
            )
            for step in snapshot.plan.steps:
                connection.execute(
                    """
                    INSERT INTO steps (
                        run_id, version, seq, name, stage, outcome_code,
                        outcome_class, attempt_no, started_at_ms, ended_at_ms,
                        result_digest, recovery_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0)
                    """,
                    (
                        run_id,
                        current_version,
                        step.seq,
                        step.name,
                        STAGE_PENDING,
                        OUTCOME_NOT_STARTED,
                        OUTCOME_NOT_STARTED,
                        1,
                    ),
                )
            payload = {
                "action": "INIT",
                "run_id": run_id,
                "version": current_version,
                "clock_ms": now,
                "observation_digest": empty_digest,
                "injection_watermark": 0,
                "pointer_version": current_version,
            }
            self._append_commit(
                connection,
                run_id=run_id,
                version=current_version,
                step_seq=None,
                action="INIT",
                clock_ms=now,
                observation_digest=empty_digest,
                injection_watermark=0,
                payload=payload,
            )
            created = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return {"created": True, "run": dict(created)}

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise BusinessError(
                    "E_NO_RUN",
                    f"run not found: {run_id}",
                    {"run_id": run_id},
                )
            return dict(row)

    def _fetch_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise BusinessError(
                "E_NO_RUN",
                f"run not found: {run_id}",
                {"run_id": run_id},
            )
        return row

    def _steps_for_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        version: int,
    ) -> dict[int, sqlite3.Row]:
        rows = connection.execute(
            "SELECT * FROM steps WHERE run_id = ? AND version = ? ORDER BY seq",
            (run_id, version),
        ).fetchall()
        return {int(row["seq"]): row for row in rows}

    def list_steps(self, run_id: str) -> list[dict[str, Any]]:
        run = self.get_run(run_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? AND version = ? ORDER BY seq",
                (run_id, run["version"]),
            ).fetchall()
            return [dict(row) for row in rows]

    def reserve_step(
        self,
        *,
        run_id: str,
        version: int,
        step_seq: int,
        start_ms: int,
        deps: tuple[int, ...],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            run = self._fetch_run(connection, run_id)
            if int(run["version"]) != version:
                raise BusinessError(
                    "E_VERSION_BINDING",
                    "run is bound to a different snapshot version",
                    {
                        "run_id": run_id,
                        "run_version": run["version"],
                        "requested_version": version,
                    },
                )
            steps = self._steps_for_run(connection, run_id, version)
            if step_seq not in steps:
                raise BusinessError(
                    "E_BAD_STEP",
                    f"step not found in run: {step_seq}",
                    {"run_id": run_id, "step": step_seq},
                )
            step = steps[step_seq]
            if step["stage"] in FINAL_STAGES:
                return {
                    "claimed": False,
                    "reason": "FINAL",
                    "stage": step["stage"],
                    "outcome_code": step["outcome_code"],
                    "result_digest": step["result_digest"],
                }
            if step["stage"] == STAGE_RUNNING:
                return {
                    "claimed": False,
                    "reason": "RUNNING",
                    "stage": step["stage"],
                }
            for dep in deps:
                if dep not in steps:
                    raise BusinessError(
                        "E_BAD_PLAN",
                        "run is missing a dependency row",
                        {"run_id": run_id, "step": step_seq, "dependency": dep},
                    )
                if steps[dep]["stage"] not in DEPENDENCY_SATISFIED_STAGES:
                    return {
                        "claimed": False,
                        "reason": "DEPENDENCY_NOT_SATISFIED",
                        "dependency": dep,
                        "dependency_stage": steps[dep]["stage"],
                    }
            claim = connection.execute(
                "SELECT * FROM step_claims WHERE version = ? AND step_seq = ?",
                (version, step_seq),
            ).fetchone()
            if claim is not None and not (
                claim["run_id"] == run_id and claim["status"] == "RUNNING"
            ):
                raise BusinessError(
                    "E_STEP_CLAIMED",
                    "step has already been claimed for this snapshot version",
                    {
                        "version": version,
                        "step": step_seq,
                        "claimed_by_run": claim["run_id"],
                        "claim_status": claim["status"],
                    },
                )
            connection.execute(
                """
                INSERT INTO step_claims (
                    version, step_seq, run_id, status, claimed_at_ms
                ) VALUES (?, ?, ?, 'RUNNING', ?)
                ON CONFLICT(version, step_seq) DO UPDATE SET
                    run_id = excluded.run_id,
                    status = 'RUNNING',
                    claimed_at_ms = excluded.claimed_at_ms
                """,
                (version, step_seq, run_id, start_ms),
            )
            connection.execute(
                """
                UPDATE steps SET
                    stage = ?, outcome_code = ?, outcome_class = ?,
                    attempt_no = 1, started_at_ms = ?, ended_at_ms = NULL,
                    result_digest = NULL
                WHERE run_id = ? AND version = ? AND seq = ?
                """,
                (
                    STAGE_RUNNING,
                    OUTCOME_NOT_STARTED,
                    OUTCOME_NOT_STARTED,
                    start_ms,
                    run_id,
                    version,
                    step_seq,
                ),
            )
            connection.execute(
                "UPDATE runs SET state = 'RUNNING', logical_clock_ms = MAX(logical_clock_ms, ?) WHERE run_id = ?",
                (start_ms, run_id),
            )
            return {
                "claimed": True,
                "stage": STAGE_RUNNING,
                "started_at_ms": start_ms,
                "attempt_no": 1,
            }

    def _observation_payload(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        version: int,
    ) -> tuple[list[dict[str, Any]], str]:
        rows = connection.execute(
            """
            SELECT * FROM observations
            WHERE run_id = ? AND version = ?
            ORDER BY step_seq, attempt_no
            """,
            (run_id, version),
        ).fetchall()
        payload: list[dict[str, Any]] = []
        for row in rows:
            payload.append(
                {
                    "step": int(row["step_seq"]),
                    "attempt_no": int(row["attempt_no"]),
                    "code": row["code"],
                    "outcome_class": row["outcome_class"],
                    "stage": row["stage"],
                    "event": row["event"],
                    "response_status": row["response_status"],
                    "body_sha256": row["body_sha256"],
                    "injected_at_ms": row["injected_at_ms"],
                    "injections": json.loads(row["injections_json"]),
                    "detail": json.loads(row["detail_json"]),
                    "result_digest": row["result_digest"],
                }
            )
        return payload, digest_value(payload)

    def _append_commit(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        version: int,
        step_seq: int | None,
        action: str,
        clock_ms: int,
        observation_digest: str,
        injection_watermark: int,
        payload: dict[str, Any],
    ) -> str:
        run = self._fetch_run(connection, run_id)
        seq = int(run["commit_seq"]) + 1
        previous_hash = str(run["commit_head"])
        payload_json = canonical_json(payload)
        commit_hash = sha256_text(previous_hash + payload_json)
        connection.execute(
            """
            INSERT INTO commits (
                run_id, seq, version, step_seq, action, clock_ms,
                observation_digest, injection_watermark, payload_json,
                previous_hash, commit_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                seq,
                version,
                step_seq,
                action,
                clock_ms,
                observation_digest,
                injection_watermark,
                payload_json,
                previous_hash,
                commit_hash,
            ),
        )
        connection.execute(
            """
            UPDATE runs SET
                commit_seq = ?, commit_head = ?,
                logical_clock_ms = MAX(logical_clock_ms, ?)
            WHERE run_id = ?
            """,
            (seq, commit_hash, clock_ms, run_id),
        )
        return commit_hash

    def skip_step(
        self,
        *,
        run_id: str,
        version: int,
        step_seq: int,
        clock_ms: int,
        step_is_skippable: bool,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            run = self._fetch_run(connection, run_id)
            if int(run["version"]) != version:
                raise BusinessError(
                    "E_VERSION_BINDING",
                    "run is bound to a different snapshot version",
                    {"run_id": run_id, "version": version},
                )
            steps = self._steps_for_run(connection, run_id, version)
            step = steps[step_seq]
            if step["stage"] == STAGE_SKIPPED:
                return {
                    "changed": False,
                    "stage": STAGE_SKIPPED,
                    "outcome_code": OUTCOME_SKIPPED_MANUALLY,
                    "result_digest": step["result_digest"],
                }
            if step["stage"] in FINAL_STAGES or step["stage"] == STAGE_RUNNING:
                raise BusinessError(
                    "E_SKIP_NOT_ALLOWED",
                    "step can only be skipped before it starts",
                    {
                        "run_id": run_id,
                        "step": step_seq,
                        "stage": step["stage"],
                    },
                )
            if not step_is_skippable:
                raise BusinessError(
                    "E_SKIP_NOT_ALLOWED",
                    "step is not marked skippable",
                    {"run_id": run_id, "step": step_seq},
                )
            detail = {
                "reason": "operator_requested_skip",
                "dependency_snapshot": {},
            }
            observation = StepObservation(
                step=step_seq,
                attempt_no=1,
                code=OUTCOME_SKIPPED_MANUALLY,
                outcome_class=OUTCOME_SKIPPED_MANUALLY,
                stage=STAGE_SKIPPED,
                event="SKIPPED",
                response_status=None,
                body_sha256=None,
                injected_at_ms=None,
                injections=(),
                detail=detail,
                started_at_ms=None,
                ended_at_ms=clock_ms,
            )
            result_digest = self._insert_observation(
                connection,
                run_id=run_id,
                version=version,
                observation=observation,
            )
            connection.execute(
                """
                UPDATE steps SET
                    stage = ?, outcome_code = ?, outcome_class = ?,
                    attempt_no = 1, ended_at_ms = ?, result_digest = ?
                WHERE run_id = ? AND version = ? AND seq = ?
                """,
                (
                    STAGE_SKIPPED,
                    OUTCOME_SKIPPED_MANUALLY,
                    OUTCOME_SKIPPED_MANUALLY,
                    clock_ms,
                    result_digest,
                    run_id,
                    version,
                    step_seq,
                ),
            )
            connection.execute(
                """
                INSERT INTO step_claims (
                    version, step_seq, run_id, status, claimed_at_ms
                ) VALUES (?, ?, ?, 'DONE', ?)
                ON CONFLICT(version, step_seq) DO UPDATE SET
                    run_id = excluded.run_id,
                    status = 'DONE',
                    claimed_at_ms = excluded.claimed_at_ms
                """,
                (version, step_seq, run_id, clock_ms),
            )
            _, observation_digest = self._observation_payload(
                connection, run_id, version
            )
            payload = {
                "action": "SKIP",
                "run_id": run_id,
                "version": version,
                "step": step_seq,
                "clock_ms": clock_ms,
                "observation_digest": observation_digest,
                "injection_watermark": run["injection_watermark"],
                "pointer_version": self._pointer_version(connection),
                "result_digest": result_digest,
            }
            head = self._append_commit(
                connection,
                run_id=run_id,
                version=version,
                step_seq=step_seq,
                action="SKIP",
                clock_ms=clock_ms,
                observation_digest=observation_digest,
                injection_watermark=int(run["injection_watermark"]),
                payload=payload,
            )
            connection.execute(
                """
                UPDATE runs SET
                    observation_digest = ?,
                    logical_clock_ms = MAX(logical_clock_ms, ?)
                WHERE run_id = ?
                """,
                (observation_digest, clock_ms, run_id),
            )
            return {
                "changed": True,
                "stage": STAGE_SKIPPED,
                "outcome_code": OUTCOME_SKIPPED_MANUALLY,
                "result_digest": result_digest,
                "observation_digest": observation_digest,
                "commit_head": head,
            }

    def _insert_observation(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        version: int,
        observation: StepObservation,
    ) -> str:
        injections_json = canonical_json(list(observation.injections))
        detail_json = canonical_json(observation.detail)
        result_digest = digest_value(
            {
                "step": observation.step,
                "attempt_no": observation.attempt_no,
                "code": observation.code,
                "outcome_class": observation.outcome_class,
                "stage": observation.stage,
                "event": observation.event,
                "response_status": observation.response_status,
                "body_sha256": observation.body_sha256,
                "injected_at_ms": observation.injected_at_ms,
                "injections": list(observation.injections),
                "detail": observation.detail,
                "started_at_ms": observation.started_at_ms,
                "ended_at_ms": observation.ended_at_ms,
            }
        )
        try:
            connection.execute(
                """
                INSERT INTO observations (
                    run_id, version, step_seq, attempt_no, code,
                    outcome_class, stage, event, response_status,
                    body_sha256, injected_at_ms, injections_json,
                    detail_json, result_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    version,
                    observation.step,
                    observation.attempt_no,
                    observation.code,
                    observation.outcome_class,
                    observation.stage,
                    observation.event,
                    observation.response_status,
                    observation.body_sha256,
                    observation.injected_at_ms,
                    injections_json,
                    detail_json,
                    result_digest,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise BusinessError(
                "E_CONFLICT",
                "observation already exists for this step attempt",
                {
                    "run_id": run_id,
                    "version": version,
                    "step": observation.step,
                    "attempt_no": observation.attempt_no,
                },
            ) from exc
        return result_digest

    def commit_step(
        self,
        *,
        run_id: str,
        version: int,
        observation: StepObservation,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            run = self._fetch_run(connection, run_id)
            if int(run["version"]) != version:
                raise BusinessError(
                    "E_VERSION_BINDING",
                    "run is bound to a different snapshot version",
                    {"run_id": run_id, "version": version},
                )
            steps = self._steps_for_run(connection, run_id, version)
            step = steps[observation.step]
            existing_obs = connection.execute(
                """
                SELECT * FROM observations
                WHERE run_id = ? AND version = ? AND step_seq = ? AND attempt_no = ?
                """,
                (run_id, version, observation.step, observation.attempt_no),
            ).fetchone()
            if step["stage"] in FINAL_STAGES:
                if existing_obs is not None:
                    candidate = self._observation_result_digest(observation)
                    if existing_obs["result_digest"] == candidate:
                        return {
                            "changed": False,
                            "idempotent": True,
                            "stage": step["stage"],
                            "result_digest": existing_obs["result_digest"],
                            "commit_head": run["commit_head"],
                        }
                raise BusinessError(
                    "E_CONFLICT",
                    "step already has a different committed outcome",
                    {
                        "run_id": run_id,
                        "step": observation.step,
                        "stage": step["stage"],
                    },
                )
            if step["stage"] != STAGE_RUNNING:
                raise BusinessError(
                    "E_STEP_NOT_RUNNING",
                    "step outcome can only be committed from RUNNING",
                    {
                        "run_id": run_id,
                        "step": observation.step,
                        "stage": step["stage"],
                    },
                )
            claim = connection.execute(
                "SELECT * FROM step_claims WHERE version = ? AND step_seq = ?",
                (version, observation.step),
            ).fetchone()
            if claim is None or claim["run_id"] != run_id:
                raise BusinessError(
                    "E_STEP_NOT_CLAIMED",
                    "step is not claimed by this run",
                    {"run_id": run_id, "step": observation.step},
                )
            if existing_obs is not None:
                candidate = self._observation_result_digest(observation)
                if existing_obs["result_digest"] != candidate:
                    raise BusinessError(
                        "E_CONFLICT",
                        "step already has a different observation",
                        {"run_id": run_id, "step": observation.step},
                    )
                return {
                    "changed": False,
                    "idempotent": True,
                    "stage": step["stage"],
                    "result_digest": existing_obs["result_digest"],
                    "commit_head": run["commit_head"],
                }

            consumed_at_ms = observation.injected_at_ms or observation.ended_at_ms or 0
            for injection_id in observation.injections:
                existing_consumption = connection.execute(
                    """
                    SELECT * FROM injection_consumptions
                    WHERE run_id = ? AND version = ? AND injection_id = ?
                    """,
                    (run_id, version, injection_id),
                ).fetchone()
                if existing_consumption is not None:
                    if (
                        int(existing_consumption["step_seq"]) == observation.step
                        and int(existing_consumption["attempt_no"])
                        == observation.attempt_no
                    ):
                        continue
                    raise BusinessError(
                        "E_INJECTION_ALREADY_CONSUMED",
                        "injection entry was already consumed",
                        {
                            "run_id": run_id,
                            "injection": injection_id,
                            "consumed_by_step": existing_consumption["step_seq"],
                        },
                    )
                connection.execute(
                    """
                    INSERT INTO injection_consumptions (
                        run_id, version, injection_id, step_seq,
                        attempt_no, at_ms, consumed_at_ms, outcome_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        version,
                        injection_id,
                        observation.step,
                        observation.attempt_no,
                        observation.injected_at_ms
                        if observation.injected_at_ms is not None
                        else consumed_at_ms,
                        consumed_at_ms,
                        observation.code,
                    ),
                )
            result_digest = self._insert_observation(
                connection,
                run_id=run_id,
                version=version,
                observation=observation,
            )
            connection.execute(
                """
                UPDATE steps SET
                    stage = ?, outcome_code = ?, outcome_class = ?,
                    attempt_no = ?, started_at_ms = ?, ended_at_ms = ?,
                    result_digest = ?
                WHERE run_id = ? AND version = ? AND seq = ?
                """,
                (
                    observation.stage,
                    observation.code,
                    observation.outcome_class,
                    observation.attempt_no,
                    observation.started_at_ms,
                    observation.ended_at_ms,
                    result_digest,
                    run_id,
                    version,
                    observation.step,
                ),
            )
            connection.execute(
                """
                UPDATE step_claims SET status = 'DONE'
                WHERE version = ? AND step_seq = ? AND run_id = ?
                """,
                (version, observation.step, run_id),
            )
            _, observation_digest = self._observation_payload(
                connection, run_id, version
            )
            consumed_rows = connection.execute(
                """
                SELECT at_ms FROM injection_consumptions
                WHERE run_id = ? AND version = ?
                """,
                (run_id, version),
            ).fetchall()
            injection_watermark = max(
                [int(run["injection_watermark"])]
                + [int(row["at_ms"]) for row in consumed_rows]
            )
            payload = {
                "action": "STEP",
                "run_id": run_id,
                "version": version,
                "step": observation.step,
                "attempt_no": observation.attempt_no,
                "stage": observation.stage,
                "outcome_code": observation.code,
                "clock_ms": observation.ended_at_ms,
                "observation_digest": observation_digest,
                "injection_watermark": injection_watermark,
                "pointer_version": self._pointer_version(connection),
                "injections": list(observation.injections),
                "result_digest": result_digest,
            }
            head = self._append_commit(
                connection,
                run_id=run_id,
                version=version,
                step_seq=observation.step,
                action="STEP",
                clock_ms=observation.ended_at_ms or 0,
                observation_digest=observation_digest,
                injection_watermark=injection_watermark,
                payload=payload,
            )
            connection.execute(
                """
                UPDATE runs SET
                    observation_digest = ?,
                    injection_watermark = ?,
                    logical_clock_ms = MAX(logical_clock_ms, ?)
                WHERE run_id = ?
                """,
                (
                    observation_digest,
                    injection_watermark,
                    observation.ended_at_ms or 0,
                    run_id,
                ),
            )
            return {
                "changed": True,
                "idempotent": False,
                "stage": observation.stage,
                "outcome_code": observation.code,
                "result_digest": result_digest,
                "observation_digest": observation_digest,
                "commit_head": head,
            }

    @staticmethod
    def _observation_result_digest(observation: StepObservation) -> str:
        return digest_value(
            {
                "step": observation.step,
                "attempt_no": observation.attempt_no,
                "code": observation.code,
                "outcome_class": observation.outcome_class,
                "stage": observation.stage,
                "event": observation.event,
                "response_status": observation.response_status,
                "body_sha256": observation.body_sha256,
                "injected_at_ms": observation.injected_at_ms,
                "injections": list(observation.injections),
                "detail": observation.detail,
                "started_at_ms": observation.started_at_ms,
                "ended_at_ms": observation.ended_at_ms,
            }
        )

    def recover_run(self, run_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            run = self._fetch_run(connection, run_id)
            version = int(run["version"])
            steps = self._steps_for_run(connection, run_id, version)
            half_committed: list[int] = []
            restored_from_observation: list[int] = []
            committed = [
                seq for seq, step in steps.items() if step["stage"] in FINAL_STAGES
            ]
            for seq, step in sorted(steps.items()):
                if step["stage"] != STAGE_RUNNING:
                    continue
                obs = connection.execute(
                    """
                    SELECT * FROM observations
                    WHERE run_id = ? AND version = ? AND step_seq = ?
                    ORDER BY attempt_no DESC LIMIT 1
                    """,
                    (run_id, version, seq),
                ).fetchone()
                if obs is not None:
                    connection.execute(
                        """
                        UPDATE steps SET
                            stage = ?, outcome_code = ?, outcome_class = ?,
                            attempt_no = ?, started_at_ms = ?, ended_at_ms = ?,
                            result_digest = ?
                        WHERE run_id = ? AND version = ? AND seq = ?
                        """,
                        (
                            obs["stage"],
                            obs["code"],
                            obs["outcome_class"],
                            obs["attempt_no"],
                            step["started_at_ms"],
                            step["started_at_ms"],
                            obs["result_digest"],
                            run_id,
                            version,
                            seq,
                        ),
                    )
                    restored_from_observation.append(seq)
                    committed.append(seq)
                    continue
                half_committed.append(seq)
                connection.execute(
                    """
                    INSERT INTO recovery_events (
                        run_id, version, step_seq, action,
                        clock_ms, detail_json
                    ) VALUES (?, ?, ?, 'HALF_COMMIT_REPLAY', ?, ?)
                    """,
                    (
                        run_id,
                        version,
                        seq,
                        int(run["logical_clock_ms"]),
                        canonical_json(
                            {
                                "stage": step["stage"],
                                "started_at_ms": step["started_at_ms"],
                                "observation_digest": run["observation_digest"],
                                "injection_watermark": run["injection_watermark"],
                            }
                        ),
                    ),
                )
                connection.execute(
                    """
                    UPDATE steps SET
                        stage = ?, outcome_code = ?, outcome_class = ?,
                        started_at_ms = NULL, ended_at_ms = NULL,
                        result_digest = NULL,
                        recovery_count = recovery_count + 1
                    WHERE run_id = ? AND version = ? AND seq = ?
                    """,
                    (
                        STAGE_PENDING,
                        OUTCOME_NOT_STARTED,
                        OUTCOME_NOT_STARTED,
                        run_id,
                        version,
                        seq,
                    ),
                )
                connection.execute(
                    "DELETE FROM step_claims WHERE version = ? AND step_seq = ? AND run_id = ?",
                    (version, seq, run_id),
                )
            if half_committed or restored_from_observation:
                _, observation_digest = self._observation_payload(
                    connection, run_id, version
                )
                connection.execute(
                    """
                    UPDATE runs SET
                        recovery_count = recovery_count + 1,
                        state = 'RUNNING',
                        observation_digest = ?
                    WHERE run_id = ?
                    """,
                    (observation_digest, run_id),
                )
            verdict = (
                "RECOVERED_REPLAY_REQUIRED"
                if half_committed
                else (
                    "RECOVERED_FINALIZED_FROM_COMMIT"
                    if restored_from_observation
                    else "NO_INTERRUPTION"
                )
            )
            return {
                "run_id": run_id,
                "version": version,
                "verdict": verdict,
                "half_committed_steps": half_committed,
                "restored_from_observation": restored_from_observation,
                "committed_steps": sorted(set(committed)),
            }

    def finalize_run_state(self, run_id: str) -> str:
        with self._transaction() as connection:
            run = self._fetch_run(connection, run_id)
            version = int(run["version"])
            steps = self._steps_for_run(connection, run_id, version)
            stages = [step["stage"] for step in steps.values()]
            if any(stage == STAGE_RUNNING for stage in stages):
                state = "RUNNING"
            elif any(stage == STAGE_PENDING for stage in stages):
                state = "BLOCKED"
            else:
                state = "COMPLETED"
            connection.execute(
                "UPDATE runs SET state = ? WHERE run_id = ?",
                (state, run_id),
            )
            return state

    def get_run_bundle(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            run = self._fetch_run(connection, run_id)
            version = int(run["version"])
            info = self._snapshot_for_version(connection, version)
            steps = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? AND version = ? ORDER BY seq",
                (run_id, version),
            ).fetchall()
            observations = connection.execute(
                """
                SELECT * FROM observations
                WHERE run_id = ? AND version = ?
                ORDER BY step_seq, attempt_no
                """,
                (run_id, version),
            ).fetchall()
            consumptions = connection.execute(
                """
                SELECT * FROM injection_consumptions
                WHERE run_id = ? AND version = ?
                ORDER BY at_ms, injection_id
                """,
                (run_id, version),
            ).fetchall()
            claims = connection.execute(
                "SELECT * FROM step_claims WHERE version = ? ORDER BY step_seq",
                (version,),
            ).fetchall()
            pointer = self._pointer_version(connection)
            return {
                "run": dict(run),
                "snapshot_info": info,
                "steps": [dict(row) for row in steps],
                "observations": [dict(row) for row in observations],
                "consumptions": [dict(row) for row in consumptions],
                "claims": [dict(row) for row in claims],
                "pointer_version": pointer,
            }

    def verify_run(self, run_id: str) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: Any = None) -> None:
            item: dict[str, Any] = {"name": name, "ok": bool(ok)}
            if detail is not None:
                item["detail"] = detail
            checks.append(item)

        with self.connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            add("sqlite_integrity", integrity == "ok", integrity)
            run_row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                add("run_exists", False, {"run_id": run_id})
                return {"ok": False, "checks": checks}
            add("run_exists", True)
            version = int(run_row["version"])
            snapshot_row = connection.execute(
                "SELECT * FROM snapshots WHERE version = ?",
                (version,),
            ).fetchone()
            add("run_snapshot_exists", snapshot_row is not None, {"version": version})
            snapshot_digest_ok = False
            snapshot_digest_detail: Any = {"version": version}
            snapshot_value: Snapshot | None = None
            if snapshot_row is not None:
                try:
                    info = self._snapshot_for_version(connection, version)
                    snapshot_value = self._snapshot_from_info(info)
                    recomputed_content_digest = digest_value(
                        {
                            "plan": snapshot_value.plan.to_dict(),
                            "routes": [
                                route.to_dict()
                                for route in snapshot_value.routes
                            ],
                            "clock": snapshot_value.clock.to_dict(),
                            "injections": [
                                injection.to_dict()
                                for injection in snapshot_value.injections
                            ],
                        }
                    )
                    snapshot_digest_ok = (
                        recomputed_content_digest
                        == snapshot_row["content_digest"]
                    )
                    snapshot_digest_detail = {
                        "stored": snapshot_row["content_digest"],
                        "recomputed": recomputed_content_digest,
                    }
                except (BusinessError, KeyError, TypeError, ValueError) as exc:
                    snapshot_digest_detail = {
                        "reason": f"{type(exc).__name__}: {exc}"
                    }
            add(
                "snapshot_content_digest",
                snapshot_digest_ok,
                snapshot_digest_detail,
            )
            pointer = self._pointer_version(connection)
            add(
                "pointer_target_exists",
                pointer is not None
                and connection.execute(
                    "SELECT 1 FROM snapshots WHERE version = ?",
                    (pointer,),
                ).fetchone()
                is not None,
                {"pointer_version": pointer},
            )
            steps = self._steps_for_run(connection, run_id, version)
            if snapshot_value is not None:
                expected_step_set = {
                    step.seq for step in snapshot_value.plan.steps
                }
                persisted_step_set = set(steps)
                add(
                    "step_set_matches_snapshot",
                    expected_step_set == persisted_step_set,
                    {
                        "expected": sorted(expected_step_set),
                        "persisted": sorted(persisted_step_set),
                    },
                )
            obs_rows = connection.execute(
                """
                SELECT * FROM observations
                WHERE run_id = ? AND version = ?
                ORDER BY step_seq, attempt_no
                """,
                (run_id, version),
            ).fetchall()
            obs_by_step: dict[int, list[sqlite3.Row]] = {}
            for row in obs_rows:
                obs_by_step.setdefault(int(row["step_seq"]), []).append(row)

            final_step_issues: list[dict[str, Any]] = []
            for seq, step in steps.items():
                rows = obs_by_step.get(seq, [])
                if step["stage"] in FINAL_STAGES:
                    if not rows:
                        final_step_issues.append(
                            {"step": seq, "reason": "missing observation"}
                        )
                        continue
                    match = [
                        row
                        for row in rows
                        if row["result_digest"] == step["result_digest"]
                    ]
                    if not match:
                        final_step_issues.append(
                            {"step": seq, "reason": "result_digest mismatch"}
                        )
                    elif (
                        match[-1]["stage"] != step["stage"]
                        or match[-1]["code"] != step["outcome_code"]
                    ):
                        final_step_issues.append(
                            {"step": seq, "reason": "stage/outcome mismatch"}
                        )
            add("final_steps_have_observations", not final_step_issues, final_step_issues)

            _, recomputed_digest = self._observation_payload(
                connection, run_id, version
            )
            add(
                "observation_digest",
                recomputed_digest == run_row["observation_digest"],
                {
                    "stored": run_row["observation_digest"],
                    "recomputed": recomputed_digest,
                },
            )

            commit_rows = connection.execute(
                "SELECT * FROM commits WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
            chain_errors: list[dict[str, Any]] = []
            expected_previous = genesis_hash(run_id, version)
            for index, row in enumerate(commit_rows, start=1):
                if int(row["seq"]) != index:
                    chain_errors.append(
                        {"seq": row["seq"], "reason": "non-contiguous sequence"}
                    )
                if row["previous_hash"] != expected_previous:
                    chain_errors.append(
                        {"seq": row["seq"], "reason": "previous_hash mismatch"}
                    )
                expected_hash = sha256_text(
                    str(row["previous_hash"]) + str(row["payload_json"])
                )
                if row["commit_hash"] != expected_hash:
                    chain_errors.append(
                        {"seq": row["seq"], "reason": "commit_hash mismatch"}
                    )
                expected_previous = str(row["commit_hash"])
            if commit_rows:
                if int(run_row["commit_seq"]) != len(commit_rows):
                    chain_errors.append({"reason": "run commit_seq mismatch"})
                if run_row["commit_head"] != commit_rows[-1]["commit_hash"]:
                    chain_errors.append({"reason": "run commit_head mismatch"})
            add("commit_hash_chain", not chain_errors, chain_errors)
            commit_watermark_ok = bool(commit_rows) and (
                commit_rows[-1]["observation_digest"]
                == run_row["observation_digest"]
            ) and (
                int(commit_rows[-1]["clock_ms"])
                <= int(run_row["logical_clock_ms"])
            )
            add(
                "commit_watermark",
                commit_watermark_ok,
                {
                    "last_observation_digest": (
                        commit_rows[-1]["observation_digest"]
                        if commit_rows
                        else None
                    ),
                    "run_observation_digest": run_row["observation_digest"],
                    "last_commit_clock_ms": (
                        int(commit_rows[-1]["clock_ms"])
                        if commit_rows
                        else None
                    ),
                    "run_clock_ms": int(run_row["logical_clock_ms"]),
                },
            )

            consumption_issues: list[dict[str, Any]] = []
            consumption_rows: list[sqlite3.Row] = []
            if snapshot_row is not None:
                snapshot_json = json.loads(snapshot_row["snapshot_json"])
                known_injections = {
                    item["id"]: item for item in snapshot_json.get("injections", [])
                }
                consumption_rows = connection.execute(
                    """
                    SELECT * FROM injection_consumptions
                    WHERE run_id = ? AND version = ?
                    """,
                    (run_id, version),
                ).fetchall()
                for row in consumption_rows:
                    injection = known_injections.get(row["injection_id"])
                    if injection is None:
                        consumption_issues.append(
                            {
                                "injection": row["injection_id"],
                                "reason": "unknown injection",
                            }
                        )
                    elif int(injection["step"]) != int(row["step_seq"]):
                        consumption_issues.append(
                            {
                                "injection": row["injection_id"],
                                "reason": "step mismatch",
                            }
                        )
                    obs_match = [
                        obs
                        for obs in obs_by_step.get(int(row["step_seq"]), [])
                        if obs["attempt_no"] == row["attempt_no"]
                    ]
                    if not obs_match:
                        consumption_issues.append(
                            {
                                "injection": row["injection_id"],
                                "reason": "missing consuming observation",
                            }
                        )
            add("injection_consumption_integrity", not consumption_issues, consumption_issues)
            expected_injection_watermark = max(
                [0] + [int(row["at_ms"]) for row in consumption_rows]
            )
            add(
                "injection_watermark",
                expected_injection_watermark
                == int(run_row["injection_watermark"]),
                {
                    "stored": int(run_row["injection_watermark"]),
                    "recomputed": expected_injection_watermark,
                },
            )
            step_rows = connection.execute(
                """
                SELECT seq, started_at_ms FROM steps
                WHERE run_id = ? AND version = ?
                """,
                (run_id, version),
            ).fetchall()
            max_step_clock = max(
                [
                    int(run_row["start_clock_ms"]),
                    *[
                        int(row["started_at_ms"])
                        for row in step_rows
                        if row["started_at_ms"] is not None
                    ],
                ]
            )
            add(
                "logical_clock_watermark",
                int(run_row["logical_clock_ms"]) >= max_step_clock,
                {
                    "stored": int(run_row["logical_clock_ms"]),
                    "minimum": max_step_clock,
                },
            )

            return {
                "ok": all(item["ok"] for item in checks),
                "run_id": run_id,
                "version": version,
                "observation_digest": run_row["observation_digest"],
                "recomputed_observation_digest": recomputed_digest,
                "commit_head": run_row["commit_head"],
                "pointer_version": pointer,
                "checks": checks,
            }

