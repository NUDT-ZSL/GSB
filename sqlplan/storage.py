from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from .errors import BusinessError, ErrorCode
from .models import PlanRecord, ViewDef
from .util import canonical_json, digest_object, normalize_digest


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS current_pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    plan_generation INTEGER NOT NULL,
    snapshot_digest TEXT NOT NULL,
    logical_clock_watermark INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_generations (
    plan_generation INTEGER PRIMARY KEY,
    created_at_ms INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    view_set_digest TEXT NOT NULL,
    snapshot_content_digest TEXT NOT NULL,
    source TEXT NOT NULL,
    patch_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('staged', 'committed'))
);

CREATE TABLE IF NOT EXISTS plans (
    plan_generation INTEGER NOT NULL,
    view_name TEXT NOT NULL,
    sql TEXT NOT NULL,
    sql_digest TEXT NOT NULL,
    upstream_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    executable_sql TEXT NOT NULL,
    resolution_json TEXT NOT NULL,
    PRIMARY KEY (plan_generation, view_name)
);

CREATE INDEX IF NOT EXISTS idx_plans_generation
ON plans(plan_generation);

CREATE TABLE IF NOT EXISTS edit_runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    patch_id TEXT NOT NULL UNIQUE,
    patch_digest TEXT NOT NULL,
    edit_round INTEGER NOT NULL,
    base_generation INTEGER NOT NULL,
    target_generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('prepared', 'staged', 'committed')),
    payload_json TEXT NOT NULL,
    result_json TEXT,
    created_at_ms INTEGER NOT NULL,
    staged_at_ms INTEGER,
    committed_at_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_edit_runs_status
ON edit_runs(status, target_generation);

CREATE TABLE IF NOT EXISTS invalidation_events (
    event_id TEXT PRIMARY KEY,
    patch_id TEXT NOT NULL,
    edit_round INTEGER NOT NULL,
    plan_generation INTEGER NOT NULL,
    view_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    trigger_source_json TEXT NOT NULL,
    propagation_path_json TEXT NOT NULL,
    node_id TEXT,
    details_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'applied')),
    created_at_ms INTEGER NOT NULL,
    applied_at_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_events_generation_status
ON invalidation_events(plan_generation, status);

CREATE TABLE IF NOT EXISTS snapshot_packages (
    package_digest TEXT PRIMARY KEY,
    plan_generation INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    view_set_digest TEXT NOT NULL,
    decision TEXT NOT NULL,
    imported_at_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
"""


TEMP_PLANS_SQL = """
CREATE TEMP TABLE IF NOT EXISTS _stage_plans (
    view_name TEXT PRIMARY KEY,
    sql TEXT NOT NULL,
    sql_digest TEXT NOT NULL,
    upstream_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    executable_sql TEXT NOT NULL,
    resolution_json TEXT NOT NULL
)
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def _fault(point: str) -> None:
    if os.environ.get("SQLPLAN_FAULT_POINT") == point:
        os._exit(91)


def views_from_records(records: Mapping[str, PlanRecord]) -> Dict[str, ViewDef]:
    return {
        name: ViewDef(
            name=record.view_name,
            sql=record.sql,
            upstream=tuple(record.upstream),
            logical_clock_ms=record.logical_clock_ms,
            content_digest=record.content_digest,
        )
        for name, record in records.items()
    }


def view_set_digest(views: Mapping[str, ViewDef]) -> str:
    return "sha256:" + digest_object([views[name].to_dict() for name in sorted(views)])


def state_digest(records: Mapping[str, PlanRecord]) -> str:
    return "sha256:" + digest_object(
        [
            {
                "view": name,
                "content_digest": records[name].content_digest,
                "plan_digest": records[name].plan_digest,
            }
            for name in sorted(records)
        ]
    )


def _record_from_row(row: sqlite3.Row) -> PlanRecord:
    return PlanRecord(
        view_name=row["view_name"],
        sql=row["sql"],
        sql_digest=row["sql_digest"],
        upstream=json.loads(row["upstream_json"]),
        logical_clock_ms=row["logical_clock_ms"],
        content_digest=row["content_digest"],
        plan=json.loads(row["plan_json"]),
        plan_digest=row["plan_digest"],
        columns=json.loads(row["columns_json"]),
        executable_sql=row["executable_sql"],
        resolution=json.loads(row["resolution_json"]),
    )


def _event_from_payload(
    event: Mapping[str, Any],
    *,
    patch_id: str,
    edit_round: int,
    plan_generation: int,
    created_at_ms: int,
) -> Tuple[Any, ...]:
    return (
        event["event_id"],
        patch_id,
        edit_round,
        plan_generation,
        event["view"],
        event["kind"],
        canonical_json(event.get("trigger_source", {})),
        canonical_json(event.get("propagation_path", [])),
        event.get("node_id"),
        canonical_json(event.get("details", {})),
        "pending",
        created_at_ms,
        None,
    )


class Storage:
    def __init__(
        self,
        db_path: str | Path,
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.timeout_seconds = timeout_seconds

    def exists(self) -> bool:
        return self.db_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(
        self, connection: sqlite3.Connection
    ) -> Iterator[sqlite3.Connection]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA_SQL)
            with self.transaction(connection):
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif int(row["value"]) != SCHEMA_VERSION:
                    raise BusinessError(
                        ErrorCode.E_STATE_CORRUPT,
                        "数据库 schema 版本不兼容",
                        {"expected": SCHEMA_VERSION, "actual": row["value"]},
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO current_pointer(
                        id, plan_generation, snapshot_digest,
                        logical_clock_watermark, updated_at_ms
                    ) VALUES (1, 0, '', 0, ?)
                    """,
                    (now_ms(),),
                )

    def require_initialized(self) -> None:
        if not self.exists():
            raise BusinessError(
                ErrorCode.E_NOT_LOADED,
                "尚未加载视图与快照",
                {"db": str(self.db_path)},
            )
        try:
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='current_pointer'"
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise BusinessError(
                ErrorCode.E_STATE_CORRUPT,
                "SQLite 状态库无法读取",
                {"db": str(self.db_path), "reason": str(exc)},
            ) from exc
        if row is None:
            raise BusinessError(
                ErrorCode.E_NOT_LOADED,
                "状态库尚未初始化",
                {"db": str(self.db_path)},
            )

    def read_state(self) -> Dict[str, Any]:
        try:
            return self._read_state_unchecked()
        except BusinessError:
            raise
        except (
            sqlite3.DatabaseError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise BusinessError(
                ErrorCode.E_STATE_CORRUPT,
                "SQLite 计划状态损坏或结构不完整",
                {"db": str(self.db_path), "reason": str(exc)},
            ) from exc

    def _read_state_unchecked(self) -> Dict[str, Any]:
        self.require_initialized()
        with self.connection() as connection:
            pointer = connection.execute(
                "SELECT * FROM current_pointer WHERE id = 1"
            ).fetchone()
            if pointer is None:
                raise BusinessError(
                    ErrorCode.E_NOT_LOADED,
                    "当前没有已提交计划指针",
                    {"db": str(self.db_path)},
                )
            generation = pointer["plan_generation"]
            rows = connection.execute(
                "SELECT * FROM plans WHERE plan_generation = ? ORDER BY view_name",
                (generation,),
            ).fetchall()
            generation_row = connection.execute(
                "SELECT * FROM plan_generations WHERE plan_generation = ?",
                (generation,),
            ).fetchone()
            pending_events = connection.execute(
                "SELECT COUNT(*) AS n FROM invalidation_events WHERE status = 'pending'"
            ).fetchone()["n"]
            pending_runs = [
                dict(row)
                for row in connection.execute(
                    "SELECT run_id, kind, patch_id, edit_round, base_generation, "
                    "target_generation, status, created_at_ms FROM edit_runs "
                    "WHERE status != 'committed' ORDER BY created_at_ms, run_id"
                )
            ]
            totals = connection.execute(
                "SELECT COUNT(*) AS generations FROM plan_generations WHERE status = 'committed'"
            ).fetchone()["generations"]
        records = {row["view_name"]: _record_from_row(row) for row in rows}
        views = views_from_records(records)
        return {
            "plan_generation": generation,
            "snapshot_digest": pointer["snapshot_digest"],
            "logical_clock_watermark": pointer["logical_clock_watermark"],
            "updated_at_ms": pointer["updated_at_ms"],
            "generation_meta": dict(generation_row) if generation_row else None,
            "records": records,
            "views": views,
            "view_set_digest": (
                generation_row["view_set_digest"] if generation_row else None
            ),
            "pending_invalidation_events": pending_events,
            "pending_runs": pending_runs,
            "committed_generation_count": totals,
        }

    def find_run(self, patch_id: str) -> Optional[Dict[str, Any]]:
        self.require_initialized()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM edit_runs WHERE patch_id = ?", (patch_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_run_by_id(self, run_id: str) -> Optional[Dict[str, Any]]:
        self.require_initialized()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM edit_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def prepare_run(
        self,
        *,
        run_id: str,
        kind: str,
        patch_id: str,
        patch_digest: str,
        edit_round: int,
        base_generation: int,
        target_generation: int,
        logical_clock_ms: int,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        created = now_ms()
        with self.connection() as connection:
            with self.transaction(connection):
                existing = connection.execute(
                    "SELECT * FROM edit_runs WHERE patch_id = ? OR run_id = ?",
                    (patch_id, run_id),
                ).fetchone()
                if existing is not None:
                    if normalize_digest(existing["patch_digest"]) != normalize_digest(
                        patch_digest
                    ):
                        raise BusinessError(
                            ErrorCode.E_PATCH_CONFLICT,
                            "同一 patch_id 对应了不同内容",
                            {
                                "patch_id": patch_id,
                                "existing_digest": existing["patch_digest"],
                                "incoming_digest": patch_digest,
                            },
                        )
                    return dict(existing)
                pointer = connection.execute(
                    "SELECT * FROM current_pointer WHERE id = 1"
                ).fetchone()
                current_generation = (
                    pointer["plan_generation"] if pointer is not None else 0
                )
                if current_generation != base_generation:
                    raise BusinessError(
                        ErrorCode.E_BASE_GENERATION_MISMATCH,
                        "运行基线计划代数已变化",
                        {
                            "expected_base_generation": base_generation,
                            "current_generation": current_generation,
                        },
                    )
                if target_generation <= base_generation:
                    raise BusinessError(
                        ErrorCode.E_GENERATION_ROLLBACK,
                        "目标计划代数必须大于当前代数",
                        {
                            "base_generation": base_generation,
                            "target_generation": target_generation,
                        },
                    )
                connection.execute(
                    """
                    INSERT INTO edit_runs(
                        run_id, kind, patch_id, patch_digest, edit_round,
                        base_generation, target_generation, logical_clock_ms,
                        status, payload_json, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                    """,
                    (
                        run_id,
                        kind,
                        patch_id,
                        patch_digest,
                        edit_round,
                        base_generation,
                        target_generation,
                        logical_clock_ms,
                        canonical_json(payload),
                        created,
                    ),
                )
                return dict(
                    connection.execute(
                        "SELECT * FROM edit_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                )

    def resume_pending(self) -> Dict[str, Any]:
        self.require_initialized()
        resumed: List[Dict[str, Any]] = []
        with self.connection() as connection:
            runs = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM edit_runs WHERE status != 'committed' "
                    "ORDER BY created_at_ms, run_id"
                )
            ]
        for run in runs:
            if run["status"] == "prepared":
                self.stage_run(run["run_id"])
            current = self.find_run_by_id(run["run_id"])
            if current is not None and current["status"] == "staged":
                self.finalize_run(run["run_id"])
            current = self.find_run_by_id(run["run_id"])
            resumed.append(
                {
                    "run_id": run["run_id"],
                    "patch_id": run["patch_id"],
                    "kind": run["kind"],
                    "status": current["status"] if current else "missing",
                    "target_generation": run["target_generation"],
                    "result": json.loads(current["result_json"])
                    if current and current.get("result_json")
                    else None,
                }
            )
        return {"resumed": resumed, "resumed_count": len(resumed)}

    def stage_run(self, run_id: str) -> Dict[str, Any]:
        _fault("before_stage")
        with self.connection() as connection:
            run = connection.execute(
                "SELECT * FROM edit_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise BusinessError(
                    ErrorCode.E_STATE_CORRUPT,
                    "待恢复运行不存在",
                    {"run_id": run_id},
                )
            if run["status"] == "committed":
                return dict(run)
            pointer = connection.execute(
                "SELECT * FROM current_pointer WHERE id = 1"
            ).fetchone()
            current_generation = (
                pointer["plan_generation"] if pointer is not None else 0
            )
            if current_generation not in (
                run["base_generation"],
                run["target_generation"],
            ):
                raise BusinessError(
                    ErrorCode.E_RECOVERY_CONFLICT,
                    "恢复时当前指针与运行基线冲突",
                    {
                        "run_id": run_id,
                        "base_generation": run["base_generation"],
                        "target_generation": run["target_generation"],
                        "current_generation": current_generation,
                    },
                )
            if current_generation == run["target_generation"]:
                return dict(run)
            payload = json.loads(run["payload_json"])
            records = {
                item["view_name"]: PlanRecord(**item) for item in payload["records"]
            }
            views = {
                item["name"]: ViewDef(
                    name=item["name"],
                    sql=item["sql"],
                    upstream=tuple(item.get("upstream", [])),
                    logical_clock_ms=item["logical_clock_ms"],
                    content_digest=item["content_digest"],
                )
                for item in payload["views"]
            }
            expected_view_set_digest = view_set_digest(views)
            existing_generation = connection.execute(
                "SELECT * FROM plan_generations WHERE plan_generation = ?",
                (run["target_generation"],),
            ).fetchone()
            if existing_generation is not None:
                if existing_generation["patch_id"] not in (None, run["patch_id"]):
                    raise BusinessError(
                        ErrorCode.E_RECOVERY_CONFLICT,
                        "目标计划代数已被其他运行占用",
                        {
                            "target_generation": run["target_generation"],
                            "existing_patch_id": existing_generation["patch_id"],
                            "incoming_patch_id": run["patch_id"],
                        },
                    )
            created = now_ms()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM invalidation_events WHERE patch_id = ? AND status = 'pending'",
                    (run["patch_id"],),
                )
                connection.execute(
                    "DELETE FROM plans WHERE plan_generation = ?",
                    (run["target_generation"],),
                )
                connection.execute(
                    "DELETE FROM plan_generations WHERE plan_generation = ? "
                    "AND status = 'staged'",
                    (run["target_generation"],),
                )
                connection.execute(TEMP_PLANS_SQL)
                connection.execute("DELETE FROM _stage_plans")
                connection.executemany(
                    """
                    INSERT INTO _stage_plans(
                        view_name, sql, sql_digest, upstream_json,
                        logical_clock_ms, content_digest, plan_json,
                        plan_digest, columns_json, executable_sql, resolution_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            record.view_name,
                            record.sql,
                            record.sql_digest,
                            canonical_json(record.upstream),
                            record.logical_clock_ms,
                            record.content_digest,
                            canonical_json(record.plan),
                            record.plan_digest,
                            canonical_json(record.columns),
                            record.executable_sql,
                            canonical_json(record.resolution),
                        )
                        for record in records.values()
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO plans(
                        plan_generation, view_name, sql, sql_digest,
                        upstream_json, logical_clock_ms, content_digest,
                        plan_json, plan_digest, columns_json,
                        executable_sql, resolution_json
                    )
                    SELECT ?, view_name, sql, sql_digest, upstream_json,
                           logical_clock_ms, content_digest, plan_json,
                           plan_digest, columns_json, executable_sql,
                           resolution_json
                    FROM _stage_plans
                    """,
                    (run["target_generation"],),
                )
                connection.execute(
                    """
                    INSERT INTO plan_generations(
                        plan_generation, created_at_ms, logical_clock_ms,
                        view_set_digest, snapshot_content_digest, source,
                        patch_id, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'staged')
                    ON CONFLICT(plan_generation) DO UPDATE SET
                        created_at_ms = excluded.created_at_ms,
                        logical_clock_ms = excluded.logical_clock_ms,
                        view_set_digest = excluded.view_set_digest,
                        snapshot_content_digest = excluded.snapshot_content_digest,
                        source = excluded.source,
                        patch_id = excluded.patch_id,
                        status = 'staged'
                    """,
                    (
                        run["target_generation"],
                        created,
                        run["logical_clock_ms"],
                        expected_view_set_digest,
                        payload.get("snapshot_content_digest", ""),
                        run["kind"],
                        run["patch_id"],
                    ),
                )
                events = payload.get("events", [])
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO invalidation_events(
                        event_id, patch_id, edit_round, plan_generation,
                        view_name, kind, trigger_source_json,
                        propagation_path_json, node_id, details_json,
                        status, created_at_ms, applied_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        _event_from_payload(
                            event,
                            patch_id=run["patch_id"],
                            edit_round=run["edit_round"],
                            plan_generation=run["target_generation"],
                            created_at_ms=created,
                        )
                        for event in events
                    ],
                )
                connection.execute(
                    "UPDATE edit_runs SET status = 'staged', staged_at_ms = ? "
                    "WHERE run_id = ?",
                    (created, run_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        _fault("after_stage")
        return self.find_run_by_id(run_id) or {}

    def finalize_run(self, run_id: str) -> Dict[str, Any]:
        _fault("before_finalize")
        with self.connection() as connection:
            run = connection.execute(
                "SELECT * FROM edit_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise BusinessError(
                    ErrorCode.E_STATE_CORRUPT,
                    "待最终化运行不存在",
                    {"run_id": run_id},
                )
            if run["status"] == "committed":
                return dict(run)
            if run["status"] != "staged":
                raise BusinessError(
                    ErrorCode.E_RECOVERY_CONFLICT,
                    "运行尚未进入 staged 状态",
                    {"run_id": run_id, "status": run["status"]},
                )
            payload = json.loads(run["payload_json"])
            expected_count = len(payload["records"])
            pointer = connection.execute(
                "SELECT * FROM current_pointer WHERE id = 1"
            ).fetchone()
            current_generation = (
                pointer["plan_generation"] if pointer is not None else 0
            )
            if current_generation not in (
                run["base_generation"],
                run["target_generation"],
            ):
                raise BusinessError(
                    ErrorCode.E_RECOVERY_CONFLICT,
                    "最终化时指针已偏离运行基线",
                    {
                        "run_id": run_id,
                        "base_generation": run["base_generation"],
                        "target_generation": run["target_generation"],
                        "current_generation": current_generation,
                    },
                )
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM plans WHERE plan_generation = ?",
                (run["target_generation"],),
            ).fetchone()["n"]
            if count != expected_count:
                raise BusinessError(
                    ErrorCode.E_STATE_CORRUPT,
                    "staged 计划集合不完整，拒绝切换指针",
                    {
                        "run_id": run_id,
                        "target_generation": run["target_generation"],
                        "expected_plan_count": expected_count,
                        "actual_plan_count": count,
                    },
                )
            committed = now_ms()
            try:
                connection.execute("BEGIN IMMEDIATE")
                if current_generation != run["target_generation"]:
                    updated = connection.execute(
                        """
                        UPDATE current_pointer
                        SET plan_generation = ?, snapshot_digest = ?,
                            logical_clock_watermark = ?, updated_at_ms = ?
                        WHERE id = 1 AND plan_generation = ?
                        """,
                        (
                            run["target_generation"],
                            payload.get("state_digest", ""),
                            run["logical_clock_ms"],
                            committed,
                            run["base_generation"],
                        ),
                    ).rowcount
                    if updated != 1:
                        raise BusinessError(
                            ErrorCode.E_RECOVERY_CONFLICT,
                            "原子切换计划指针失败",
                            {"run_id": run_id},
                        )
                else:
                    connection.execute(
                        "UPDATE current_pointer SET snapshot_digest = ?, "
                        "logical_clock_watermark = ?, updated_at_ms = ? WHERE id = 1",
                        (
                            payload.get("state_digest", ""),
                            run["logical_clock_ms"],
                            committed,
                        ),
                    )
                _fault("during_finalize")
                connection.execute(
                    "UPDATE plan_generations SET status = 'committed' "
                    "WHERE plan_generation = ? AND patch_id = ?",
                    (run["target_generation"], run["patch_id"]),
                )
                connection.execute(
                    "UPDATE invalidation_events SET status = 'applied', "
                    "applied_at_ms = ? WHERE patch_id = ? AND status = 'pending'",
                    (committed, run["patch_id"]),
                )
                snapshot_package = payload.get("snapshot_package")
                if snapshot_package:
                    connection.execute(
                        """
                        INSERT INTO snapshot_packages(
                            package_digest, plan_generation, content_digest,
                            view_set_digest, decision, imported_at_ms, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(package_digest) DO UPDATE SET
                            plan_generation = excluded.plan_generation,
                            decision = excluded.decision,
                            imported_at_ms = excluded.imported_at_ms,
                            payload_json = excluded.payload_json
                        """,
                        (
                            snapshot_package["package_digest"],
                            run["target_generation"],
                            snapshot_package["content_digest"],
                            snapshot_package["view_set_digest"],
                            snapshot_package["decision"],
                            committed,
                            canonical_json(snapshot_package.get("payload", {})),
                        ),
                    )
                result = payload.get("result", {})
                result = {
                    **result,
                    "ok": True,
                    "run_id": run_id,
                    "patch_id": run["patch_id"],
                    "base_plan_generation": run["base_generation"],
                    "plan_generation": run["target_generation"],
                    "logical_clock_ms": run["logical_clock_ms"],
                }
                connection.execute(
                    "UPDATE edit_runs SET status = 'committed', "
                    "committed_at_ms = ?, result_json = ? WHERE run_id = ?",
                    (committed, canonical_json(result), run_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        _fault("after_commit")
        return self.find_run_by_id(run_id) or {}

    def pending_event_count(self) -> int:
        self.require_initialized()
        with self.connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) AS n FROM invalidation_events WHERE status = 'pending'"
            ).fetchone()["n"]

    def list_pending_runs(self) -> List[Dict[str, Any]]:
        self.require_initialized()
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT run_id, kind, patch_id, edit_round, base_generation, "
                    "target_generation, status, created_at_ms FROM edit_runs "
                    "WHERE status != 'committed' ORDER BY created_at_ms, run_id"
                )
            ]

    def generation_is_committed(self, generation: int) -> bool:
        self.require_initialized()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM plan_generations WHERE plan_generation = ?",
                (generation,),
            ).fetchone()
        return bool(row and row["status"] == "committed")

    def snapshot_package_count(self) -> int:
        self.require_initialized()
        with self.connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) AS n FROM snapshot_packages"
            ).fetchone()["n"]

    def latest_package_content_digest(self, plan_generation: int) -> Optional[str]:
        self.require_initialized()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT content_digest FROM snapshot_packages "
                "WHERE plan_generation = ? "
                "ORDER BY imported_at_ms DESC, package_digest LIMIT 1",
                (plan_generation,),
            ).fetchone()
        return row["content_digest"] if row else None
