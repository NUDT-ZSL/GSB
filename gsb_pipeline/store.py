"""Transactional SQLite state, two-phase snapshot replacement, and recovery."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    BusinessError,
    E_INPUT_SCHEMA,
    E_LOGIC_CLOCK_REGRESSION,
    E_PATCH_PAYLOAD_CONFLICT,
    E_PATCH_SEQUENCE_CONFLICT,
    E_PLAN_INTEGRITY,
    E_RESUME_CONFLICT,
    E_STATE_CONFLICT,
    E_STATE_EXISTS,
    E_STATE_NOT_INITIALIZED,
)
from .models import DefinitionSet, ViewDefinition, build_view_model
from .patches import (
    compute_patch_candidate,
    invalidation_result,
    normalize_patch_event,
)
from .sqlplan import verify_plan_integrity
from .utils import (
    canonical_json,
    sha256_json,
    sha256_text,
)


SCHEMA_VERSION = 1


@dataclass
class StoredView:
    view_name: str
    definition: dict[str, Any]
    sql: str
    executable_sql: str
    output_columns: list[str]
    column_dependencies: dict[str, list[dict[str, str]]]
    dependencies: list[str]
    plan: dict[str, Any]
    plan_json: str
    plan_summary: str
    stale: bool
    dirty_nodes: list[str]

    @property
    def declared_upstreams(self) -> list[str]:
        return list(self.definition.get("declared_upstreams", []))


@dataclass
class CurrentState:
    generation: int
    logical_clock_watermark: int
    last_sequence: int
    clock_tolerance_ms: int
    sources: dict[str, list[str]]
    rows: dict[str, StoredView]
    model: DefinitionSet


@contextlib.contextmanager
def writer_lock(state_db: str | Path) -> Iterator[None]:
    """Serialize state writers; readers continue under WAL."""

    db_path = Path(state_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(db_path) + ".lock")
    with open(lock_path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _connect_writer(state_db: str | Path) -> sqlite3.Connection:
    path = Path(state_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _connect_reader(state_db: str | Path) -> sqlite3.Connection:
    path = Path(state_db)
    if not path.exists():
        raise BusinessError(
            E_STATE_NOT_INITIALIZED,
            "状态数据库不存在，请先执行 load",
            path=str(path),
        )
    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=30.0,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise BusinessError(
            E_STATE_NOT_INITIALIZED,
            "无法打开状态数据库",
            path=str(path),
            error=str(exc),
        ) from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            plan_generation INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            parent_generation INTEGER,
            patch_id TEXT,
            logical_clock_ms INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            committed_at_ms INTEGER,
            record_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshot_views (
            plan_generation INTEGER NOT NULL,
            view_name TEXT NOT NULL,
            definition_json TEXT NOT NULL,
            sql TEXT NOT NULL,
            executable_sql TEXT NOT NULL,
            output_columns_json TEXT NOT NULL,
            column_dependencies_json TEXT NOT NULL,
            dependencies_json TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            plan_summary TEXT NOT NULL,
            stale INTEGER NOT NULL DEFAULT 0,
            dirty_nodes_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (plan_generation, view_name),
            FOREIGN KEY (plan_generation) REFERENCES snapshots(plan_generation)
        );
        CREATE TABLE IF NOT EXISTS pointer (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            plan_generation INTEGER NOT NULL,
            logical_clock_watermark INTEGER NOT NULL,
            last_sequence INTEGER NOT NULL,
            stream_clock_tolerance_ms INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS patch_runs (
            patch_id TEXT PRIMARY KEY,
            sequence INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            base_generation INTEGER NOT NULL,
            target_generation INTEGER,
            status TEXT NOT NULL,
            result_json TEXT,
            error_json TEXT,
            stage_hash TEXT,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS patch_runs_status_idx
            ON patch_runs(status, sequence);
        CREATE TABLE IF NOT EXISTS invalidation_events (
            event_id TEXT PRIMARY KEY,
            patch_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            plan_generation INTEGER NOT NULL,
            view_name TEXT NOT NULL,
            path_json TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS invalidation_patch_idx
            ON invalidation_events(patch_id, sequence);
        """
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _get_setting(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,),
    ).fetchone()
    return None if row is None else row["value"]


def _set_setting(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _plan_summary_from_document(plan: dict[str, Any]) -> str:
    return sha256_json(
        {
            "format": plan.get("format"),
            "view": plan.get("view"),
            "root": plan.get("root", {}).get("summary"),
            "output_columns": plan.get("output_columns"),
            "column_dependencies": plan.get("column_dependencies"),
            "dependencies": plan.get("dependencies"),
        }
    )


def _verify_node(node: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    payload = {
        "type": node["type"],
        "children": [child["summary"] for child in node.get("children", [])],
        "output_columns": node["output_columns"],
        "predicate_columns": sorted(set(node.get("predicate_columns", []))),
        "join_keys": node.get("join_keys", []),
        "references": node.get("references", []),
        "details": node.get("details"),
    }
    expected = sha256_json(payload)
    if expected != node.get("summary"):
        errors.append(f"node_summary:{node.get('node_id', '?')}")
    expected_node_id = f"nod_{node.get('summary')}"
    if node.get("node_id") != expected_node_id:
        errors.append(f"node_id:{node.get('node_id', '?')}")
    for child in node.get("children", []):
        errors.extend(_verify_node(child))
    return errors


def _verify_plan_document(plan: dict[str, Any]) -> list[str]:
    errors = verify_plan_integrity(plan)
    root = plan.get("root")
    if not isinstance(root, dict):
        return errors + ["plan.root"]
    errors.extend(_verify_node(root))
    expected_summary = _plan_summary_from_document(plan)
    if expected_summary != plan.get("plan_summary"):
        errors.append("plan_summary")
    return sorted(errors)


def _row_from_stored(row: sqlite3.Row) -> StoredView:
    plan = json.loads(row["plan_json"])
    return StoredView(
        view_name=row["view_name"],
        definition=json.loads(row["definition_json"]),
        sql=row["sql"],
        executable_sql=row["executable_sql"],
        output_columns=json.loads(row["output_columns_json"]),
        column_dependencies=json.loads(row["column_dependencies_json"]),
        dependencies=json.loads(row["dependencies_json"]),
        plan=plan,
        plan_json=row["plan_json"],
        plan_summary=row["plan_summary"],
        stale=bool(row["stale"]),
        dirty_nodes=json.loads(row["dirty_nodes_json"]),
    )


def _read_rows(
    connection: sqlite3.Connection,
    generation: int,
) -> dict[str, StoredView]:
    rows = connection.execute(
        "SELECT * FROM snapshot_views WHERE plan_generation=? ORDER BY view_name",
        (generation,),
    ).fetchall()
    return {row["view_name"]: _row_from_stored(row) for row in rows}


def _load_state(connection: sqlite3.Connection) -> CurrentState:
    if not _table_exists(connection, "pointer"):
        raise BusinessError(E_STATE_NOT_INITIALIZED, "状态数据库未初始化")
    pointer = connection.execute("SELECT * FROM pointer WHERE singleton=1").fetchone()
    if pointer is None:
        raise BusinessError(E_STATE_NOT_INITIALIZED, "状态指针不存在")
    sources_json = _get_setting(connection, "sources_json")
    if sources_json is None:
        raise BusinessError(E_STATE_NOT_INITIALIZED, "状态数据库缺少 sources_json")
    sources = json.loads(sources_json)
    rows = _read_rows(connection, pointer["plan_generation"])
    sql_map = {name: row.sql for name, row in rows.items()}
    declared_map = {name: row.declared_upstreams for name, row in rows.items()}
    model = build_view_model(
        sources=sources,
        sql_map=sql_map,
        declared_upstreams=declared_map,
        raw={"state_generation": pointer["plan_generation"]},
    )
    stored_views: dict[str, ViewDefinition] = {}
    for name, row in rows.items():
        plan_errors = _verify_plan_document(row.plan)
        if plan_errors:
            raise BusinessError(
                E_PLAN_INTEGRITY,
                f"状态中的计划文档损坏: {name}",
                view=name,
                errors=plan_errors,
                edit_round=pointer["last_sequence"],
            )
        if (
            row.plan.get("plan_summary") != row.plan_summary
            or row.plan.get("output_columns") != row.output_columns
            or row.plan.get("column_dependencies") != row.column_dependencies
        ):
            raise BusinessError(
                E_PLAN_INTEGRITY,
                f"状态中的计划摘要/列契约不一致: {name}",
                view=name,
                edit_round=pointer["last_sequence"],
            )
        rebuilt = model.views[name]
        stored_views[name] = ViewDefinition(
            name=name,
            sql=row.sql,
            declared_upstreams=rebuilt.declared_upstreams,
            dependencies=rebuilt.dependencies,
            plan=row.plan,
            columns=row.output_columns,
            column_dependencies=row.column_dependencies,
            executable_sql=row.executable_sql,
        )
    return CurrentState(
        generation=pointer["plan_generation"],
        logical_clock_watermark=pointer["logical_clock_watermark"],
        last_sequence=pointer["last_sequence"],
        clock_tolerance_ms=pointer["stream_clock_tolerance_ms"],
        sources=sources,
        rows=rows,
        model=DefinitionSet(
            sources=sources,
            views=stored_views,
            order=model.order,
            raw={"state_generation": pointer["plan_generation"]},
        ),
    )


def _definition_json(
    name: str,
    sql: str,
    declared_upstreams: list[str],
    dependencies: list[str],
) -> str:
    return canonical_json(
        {
            "name": name,
            "sql": sql,
            "declared_upstreams": sorted(declared_upstreams),
            "dependencies": sorted(dependencies),
        }
    )


def _row_fields(
    *,
    view_name: str,
    definition_json: str,
    sql: str,
    executable_sql: str,
    output_columns_json: str,
    column_dependencies_json: str,
    dependencies_json: str,
    plan_json: str,
    plan_summary: str,
    stale: bool,
    dirty_nodes_json: str,
) -> dict[str, Any]:
    return {
        "view_name": view_name,
        "definition_json": definition_json,
        "sql": sql,
        "executable_sql": executable_sql,
        "output_columns_json": output_columns_json,
        "column_dependencies_json": column_dependencies_json,
        "dependencies_json": dependencies_json,
        "plan_json": plan_json,
        "plan_summary": plan_summary,
        "stale": 1 if stale else 0,
        "dirty_nodes_json": dirty_nodes_json,
    }


def _new_row_from_plan(
    *,
    name: str,
    sql: str,
    declared_upstreams: list[str],
    dependencies: list[str],
    columns: list[str],
    column_dependencies: dict[str, list[dict[str, str]]],
    plan: dict[str, Any],
    stale: bool = False,
    dirty_nodes: list[str] | None = None,
) -> dict[str, Any]:
    plan_json = canonical_json(plan)
    return _row_fields(
        view_name=name,
        definition_json=_definition_json(
            name,
            sql,
            declared_upstreams,
            dependencies,
        ),
        sql=sql,
        executable_sql=sql,
        output_columns_json=canonical_json(columns),
        column_dependencies_json=canonical_json(column_dependencies),
        dependencies_json=canonical_json(sorted(dependencies)),
        plan_json=plan_json,
        plan_summary=plan["plan_summary"],
        stale=stale,
        dirty_nodes_json=canonical_json(sorted(dirty_nodes or [])),
    )


def _snapshot_record_hash(
    *,
    generation: int,
    parent_generation: int | None,
    patch_id: str | None,
    logical_clock_ms: int,
    rows: list[dict[str, Any]],
) -> str:
    return sha256_json(
        {
            "generation": generation,
            "parent_generation": parent_generation,
            "patch_id": patch_id,
            "logical_clock_ms": logical_clock_ms,
            "rows": sorted(rows, key=lambda item: item["view_name"]),
        }
    )


def _insert_snapshot_rows(
    connection: sqlite3.Connection,
    generation: int,
    rows: list[dict[str, Any]],
) -> None:
    connection.execute("DROP TABLE IF EXISTS temp.staged_plan_rows")
    connection.execute(
        "CREATE TEMP TABLE staged_plan_rows AS "
        "SELECT * FROM snapshot_views WHERE 0"
    )
    connection.executemany(
        "INSERT INTO temp.staged_plan_rows(plan_generation, view_name, definition_json, "
        "sql, executable_sql, output_columns_json, column_dependencies_json, "
        "dependencies_json, plan_json, plan_summary, stale, dirty_nodes_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                generation,
                row["view_name"],
                row["definition_json"],
                row["sql"],
                row["executable_sql"],
                row["output_columns_json"],
                row["column_dependencies_json"],
                row["dependencies_json"],
                row["plan_json"],
                row["plan_summary"],
                row["stale"],
                row["dirty_nodes_json"],
            )
            for row in sorted(rows, key=lambda item: item["view_name"])
        ],
    )
    connection.execute(
        "INSERT INTO snapshot_views SELECT * FROM temp.staged_plan_rows"
    )
    connection.execute("DROP TABLE temp.staged_plan_rows")


def initialize_state(
    state_db: str | Path,
    merged: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    db_path = Path(state_db)
    with writer_lock(db_path):
        if db_path.exists() and not force:
            existing_fingerprint: str | None = None
            has_settings = False
            connection = _connect_reader(db_path)
            try:
                has_settings = _table_exists(connection, "settings")
                if has_settings:
                    existing_fingerprint = _get_setting(connection, "source_fingerprint")
                    if existing_fingerprint == merged["source_fingerprint"]:
                        pointer = connection.execute(
                            "SELECT * FROM pointer WHERE singleton=1"
                        ).fetchone()
                        return {
                            "ok": True,
                            "initialized": True,
                            "already_initialized": True,
                            "state_db": str(db_path),
                            "plan_generation": pointer["plan_generation"] if pointer else None,
                            "logical_clock_watermark": (
                                pointer["logical_clock_watermark"] if pointer else None
                            ),
                            "view_count": len(merged["views"]),
                        }
                    raise BusinessError(
                        E_STATE_EXISTS,
                        "状态数据库已存在且来源指纹不同，使用 --force 重建",
                        path=str(db_path),
                        existing_fingerprint=existing_fingerprint,
                        incoming_fingerprint=merged["source_fingerprint"],
                    )
            finally:
                connection.close()
        if force:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(db_path) + suffix)
                if candidate.exists():
                    candidate.unlink()
        connection = _connect_writer(db_path)
        try:
            _create_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                _set_setting(connection, "state_format", "gsb-state-v1")
                _set_setting(connection, "schema_version", str(SCHEMA_VERSION))
                _set_setting(connection, "source_fingerprint", merged["source_fingerprint"])
                _set_setting(connection, "v1_base_clock_ms", str(merged.get("v1_base_clock_ms")))
                # Source schemas are reconstructed from the merged definitions'
                # base sources by rebuilding the model.
                merged_model = build_view_model(
                    sources=_base_sources_from_merged(merged),
                    sql_map={view["name"]: view["sql"] for view in merged["views"]},
                    declared_upstreams={
                        view["name"]: view.get("declared_upstreams", [])
                        for view in merged["views"]
                    },
                    raw={"initial_merge": True},
                )
                _set_setting(
                    connection,
                    "sources_json",
                    canonical_json(merged_model.sources),
                )
                rows: list[dict[str, Any]] = []
                for name in sorted(merged_model.views):
                    view = merged_model.views[name]
                    rows.append(
                        _new_row_from_plan(
                            name=name,
                            sql=view.executable_sql,
                            declared_upstreams=view.declared_upstreams,
                            dependencies=view.dependencies,
                            columns=view.columns,
                            column_dependencies=view.column_dependencies,
                            plan=view.plan,
                        )
                    )
                logical_clock = int(merged["logical_clock_ms"])
                record_hash = _snapshot_record_hash(
                    generation=1,
                    parent_generation=None,
                    patch_id=None,
                    logical_clock_ms=logical_clock,
                    rows=rows,
                )
                connection.execute(
                    "INSERT INTO snapshots(plan_generation, status, parent_generation, "
                    "patch_id, logical_clock_ms, created_at_ms, committed_at_ms, record_hash) "
                    "VALUES (1, 'COMMITTED', NULL, NULL, ?, ?, ?, ?)",
                    (logical_clock, logical_clock, logical_clock, record_hash),
                )
                _insert_snapshot_rows(connection, 1, rows)
                connection.execute(
                    "INSERT INTO pointer(singleton, plan_generation, "
                    "logical_clock_watermark, last_sequence, stream_clock_tolerance_ms) "
                    "VALUES (1, 1, ?, 0, 0)",
                    (logical_clock,),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
    return {
        "ok": True,
        "initialized": True,
        "already_initialized": False,
        "state_db": str(db_path),
        "plan_generation": 1,
        "logical_clock_watermark": int(merged["logical_clock_ms"]),
        "view_count": len(merged["views"]),
    }


def _base_sources_from_merged(merged: dict[str, Any]) -> dict[str, list[str]]:
    """Recover base-source schemas from the merge's dependency universe.

    The merge result does not duplicate base schemas, so the source columns are
    read from a temporary marker written by the CLI when available.  In normal
    operation ``initialize_state_with_definitions`` is used instead; this helper
    keeps the low-level API usable with a merged-only payload by accepting the
    serialized schema in ``merged['sources']``.
    """

    sources = merged.get("sources")
    if not isinstance(sources, dict):
        raise BusinessError(
            E_INPUT_SCHEMA,
            "初始化状态需要 merged.sources 基础表模式",
            field="sources",
        )
    return sources


def initialize_state_with_definitions(
    state_db: str | Path,
    definitions: DefinitionSet,
    merged: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    enriched = dict(merged)
    enriched["sources"] = definitions.sources
    return initialize_state(state_db, enriched, force=force)


def _read_patch_run(connection: sqlite3.Connection, patch_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM patch_runs WHERE patch_id=?",
        (patch_id,),
    ).fetchone()


def _insert_run(
    connection: sqlite3.Connection,
    event: dict[str, Any],
    *,
    base_generation: int,
) -> str:
    payload_json = canonical_json(event)
    payload_hash = sha256_text(payload_json)
    connection.execute(
        "INSERT INTO patch_runs(patch_id, sequence, payload_json, payload_hash, "
        "base_generation, target_generation, status, created_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, ?, ?, NULL, 'PREPARED', ?, ?)",
        (
            event["patch_id"],
            event["sequence"],
            payload_json,
            payload_hash,
            base_generation,
            event["logical_clock_ms"],
            event["logical_clock_ms"],
        ),
    )
    return payload_hash


def _failure_payload(
    event: dict[str, Any],
    error: BusinessError,
    *,
    generation: int,
    duplicate: bool,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "duplicate": duplicate,
        "patch_id": event["patch_id"],
        "sequence": event["sequence"],
        "op": event["op"],
        "view": event["view"],
        "plan_generation": generation,
        "error": error.to_dict(),
    }


def _record_failure(
    connection: sqlite3.Connection,
    *,
    patch_id: str,
    event: dict[str, Any],
    error: BusinessError,
    generation: int,
) -> dict[str, Any]:
    payload = _failure_payload(event, error, generation=generation, duplicate=False)
    connection.execute(
        "UPDATE patch_runs SET status='FAILED', error_json=?, result_json=?, "
        "updated_at_ms=? WHERE patch_id=?",
        (
            canonical_json(error.to_dict()),
            canonical_json(payload),
            event["logical_clock_ms"],
            patch_id,
        ),
    )
    return payload


def _check_pending_runs(
    connection: sqlite3.Connection,
    *,
    exclude_patch_id: str | None = None,
) -> list[str]:
    rows = connection.execute(
        "SELECT patch_id FROM patch_runs "
        "WHERE status IN ('PREPARED', 'STAGED') "
        "AND (? IS NULL OR patch_id <> ?) ORDER BY sequence, patch_id",
        (exclude_patch_id, exclude_patch_id),
    ).fetchall()
    return [row["patch_id"] for row in rows]


def _check_edit_round_conflict(
    connection: sqlite3.Connection,
    event: dict[str, Any],
) -> None:
    rows = connection.execute(
        "SELECT patch_id, payload_json FROM patch_runs WHERE sequence=?",
        (event["sequence"],),
    ).fetchall()
    for row in rows:
        if row["patch_id"] == event["patch_id"]:
            continue
        payload = json.loads(row["payload_json"])
        if payload.get("view") == event["view"]:
            raise BusinessError(
                E_PATCH_SEQUENCE_CONFLICT,
                "同一视图的同一编辑轮次已由其他补丁占用",
                view=event["view"],
                edit_round=event["sequence"],
                existing_patch_id=row["patch_id"],
                incoming_patch_id=event["patch_id"],
            )


def _build_committed_rows(
    current: CurrentState,
    computed: dict[str, Any],
) -> list[dict[str, Any]]:
    candidate_model = computed["candidate_model"]
    direct_name = computed["view"]
    op = computed["op"]
    affected = {
        item["view"]: item
        for item in computed["invalidation"]["affected_views"]
    }
    rows: list[dict[str, Any]] = []
    for name in sorted(candidate_model.views):
        view = candidate_model.views[name]
        old_row = current.rows.get(name)
        direct_changed = name == direct_name and op != "delete"
        recompiled_contract_change = (
            name in affected
            and affected[name].get("reason") == "upstream_schema_changed"
        )
        if direct_changed or recompiled_contract_change or old_row is None:
            rows.append(
                _new_row_from_plan(
                    name=name,
                    sql=view.executable_sql,
                    declared_upstreams=view.declared_upstreams,
                    dependencies=view.dependencies,
                    columns=view.columns,
                    column_dependencies=view.column_dependencies,
                    plan=view.plan,
                    stale=False,
                    dirty_nodes=[],
                )
            )
            continue
        assert old_row is not None
        stale = old_row.stale
        dirty_nodes = set(old_row.dirty_nodes)
        if name in affected:
            stale = True
            dirty_nodes.update(affected[name].get("dirty_nodes", []))
        # Copy every byte-bearing field directly.  Unaffected cached plans and
        # executable SQL are therefore unchanged by construction.
        rows.append(
            _row_fields(
                view_name=name,
                definition_json=old_row.definition if isinstance(old_row.definition, str) else canonical_json(old_row.definition),
                sql=old_row.sql,
                executable_sql=old_row.executable_sql,
                output_columns_json=canonical_json(old_row.output_columns),
                column_dependencies_json=canonical_json(old_row.column_dependencies),
                dependencies_json=canonical_json(old_row.dependencies),
                plan_json=old_row.plan_json,
                plan_summary=old_row.plan_summary,
                stale=stale,
                dirty_nodes_json=canonical_json(sorted(dirty_nodes)),
            )
        )
    return rows


def _stage_snapshot(
    connection: sqlite3.Connection,
    *,
    current: CurrentState,
    event: dict[str, Any],
    target_generation: int,
    result: dict[str, Any],
    rows: list[dict[str, Any]],
) -> str:
    logical_clock = max(current.logical_clock_watermark, event["logical_clock_ms"])
    record_hash = _snapshot_record_hash(
        generation=target_generation,
        parent_generation=current.generation,
        patch_id=event["patch_id"],
        logical_clock_ms=logical_clock,
        rows=rows,
    )
    connection.execute(
        "INSERT INTO snapshots(plan_generation, status, parent_generation, patch_id, "
        "logical_clock_ms, created_at_ms, committed_at_ms, record_hash) "
        "VALUES (?, 'STAGED', ?, ?, ?, ?, NULL, ?)",
        (
            target_generation,
            current.generation,
            event["patch_id"],
            logical_clock,
            event["logical_clock_ms"],
            record_hash,
        ),
    )
    _insert_snapshot_rows(connection, target_generation, rows)
    connection.execute(
        "UPDATE patch_runs SET target_generation=?, status='STAGED', "
        "result_json=?, stage_hash=?, base_generation=?, updated_at_ms=? "
        "WHERE patch_id=?",
        (
            target_generation,
            canonical_json(result),
            record_hash,
            current.generation,
            event["logical_clock_ms"],
            event["patch_id"],
        ),
    )
    return record_hash


def _finalize_staged(
    connection: sqlite3.Connection,
    *,
    run: sqlite3.Row,
    duplicate: bool = False,
) -> dict[str, Any]:
    if run["status"] == "COMMITTED":
        result = json.loads(run["result_json"])
        result["duplicate"] = True
        return result
    if run["status"] != "STAGED":
        raise BusinessError(
            E_RESUME_CONFLICT,
            "只有 STAGED 运行可以完成提交",
            edit_round=run["sequence"],
            patch_id=run["patch_id"],
            status=run["status"],
        )
    target_generation = run["target_generation"]
    snapshot = connection.execute(
        "SELECT * FROM snapshots WHERE plan_generation=?",
        (target_generation,),
    ).fetchone()
    if snapshot is None or snapshot["status"] != "STAGED":
        raise BusinessError(
            E_RESUME_CONFLICT,
            "STAGED 运行缺少对应的半提交快照",
            edit_round=run["sequence"],
            patch_id=run["patch_id"],
            plan_generation=target_generation,
        )
    pointer = connection.execute("SELECT * FROM pointer WHERE singleton=1").fetchone()
    if pointer["plan_generation"] == target_generation:
        result = json.loads(run["result_json"])
        event_payload = json.loads(run["payload_json"])
        for item in result.get("invalidation", {}).get("affected_views", []):
            event_id = f"invalidation:{run['patch_id']}:{item['view']}"
            connection.execute(
                "INSERT OR IGNORE INTO invalidation_events(event_id, patch_id, sequence, "
                "plan_generation, view_name, path_json, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    run["patch_id"],
                    run["sequence"],
                    target_generation,
                    item["view"],
                    canonical_json(item["dependency_path"]),
                    canonical_json(item),
                ),
            )
        connection.execute(
            "UPDATE patch_runs SET status='COMMITTED', result_json=?, updated_at_ms=? "
            "WHERE patch_id=?",
            (
                canonical_json({**result, "duplicate": True}),
                event_payload["logical_clock_ms"],
                run["patch_id"],
            ),
        )
        result["duplicate"] = True
        return result
    if pointer["plan_generation"] != run["base_generation"]:
        raise BusinessError(
            E_RESUME_CONFLICT,
            "状态指针已越过半提交快照的基线",
            edit_round=run["sequence"],
            patch_id=run["patch_id"],
            base_generation=run["base_generation"],
            current_generation=pointer["plan_generation"],
            target_generation=target_generation,
        )
    result = json.loads(run["result_json"])
    invalidation = result.get("invalidation", {})
    event_payload = json.loads(run["payload_json"])
    for item in invalidation.get("affected_views", []):
        event_id = f"invalidation:{run['patch_id']}:{item['view']}"
        connection.execute(
            "INSERT OR IGNORE INTO invalidation_events(event_id, patch_id, sequence, "
            "plan_generation, view_name, path_json, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                run["patch_id"],
                run["sequence"],
                target_generation,
                item["view"],
                canonical_json(item["dependency_path"]),
                canonical_json(item),
            ),
        )
    connection.execute(
        "UPDATE snapshots SET status='COMMITTED', committed_at_ms=? "
        "WHERE plan_generation=? AND status='STAGED'",
        (event_payload["logical_clock_ms"], target_generation),
    )
    connection.execute(
        "UPDATE pointer SET plan_generation=?, logical_clock_watermark=?, "
        "last_sequence=? WHERE singleton=1",
        (
            target_generation,
            max(pointer["logical_clock_watermark"], event_payload["logical_clock_ms"]),
            max(pointer["last_sequence"], run["sequence"]),
        ),
    )
    result["duplicate"] = duplicate
    connection.execute(
        "UPDATE patch_runs SET status='COMMITTED', result_json=?, updated_at_ms=? "
        "WHERE patch_id=?",
        (
            canonical_json(result),
            event_payload["logical_clock_ms"],
            run["patch_id"],
        ),
    )
    return result


def _apply_event_locked(
    state_db: Path,
    event: dict[str, Any],
    *,
    clock_tolerance_ms: int | None,
) -> dict[str, Any]:
    connection = _connect_writer(state_db)
    try:
        existing = _read_patch_run(connection, event["patch_id"])
        payload_hash = sha256_text(canonical_json(event))
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise BusinessError(
                    E_PATCH_PAYLOAD_CONFLICT,
                    "同一 patch_id 对应了不同载荷",
                    view=event["view"],
                    edit_round=event["sequence"],
                    patch_id=event["patch_id"],
                )
            if existing["status"] == "COMMITTED":
                result = json.loads(existing["result_json"])
                result["duplicate"] = True
                return result
            if existing["status"] == "FAILED":
                error_payload = json.loads(existing["error_json"])
                return {
                    "status": "failed",
                    "duplicate": True,
                    "patch_id": event["patch_id"],
                    "sequence": event["sequence"],
                    "op": event["op"],
                    "view": event["view"],
                    "plan_generation": _load_state(connection).generation,
                    "error": error_payload,
                }
            if existing["status"] == "STAGED":
                connection.execute("BEGIN IMMEDIATE")
                try:
                    result = _finalize_staged(connection, run=existing, duplicate=True)
                    connection.execute("COMMIT")
                    return result
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        else:
            pending = _check_pending_runs(connection)
            if pending:
                raise BusinessError(
                    E_STATE_CONFLICT,
                    "存在未完成的补丁运行，请先执行 resume",
                    pending_patch_ids=pending,
                    edit_round=event["sequence"],
                )
            _check_edit_round_conflict(connection, event)

        connection.execute("BEGIN IMMEDIATE")
        try:
            if existing is None:
                state = _load_state(connection)
                _insert_run(connection, event, base_generation=state.generation)
                connection.execute("COMMIT")
                if os.environ.get("GSB_CRASH_AFTER_PREPARE") == "1":
                    os._exit(97)
                connection.execute("BEGIN IMMEDIATE")
            state = _load_state(connection)
            run = _read_patch_run(connection, event["patch_id"])
            if run is None:
                raise BusinessError(
                    E_STATE_CONFLICT,
                    "补丁运行记录丢失",
                    patch_id=event["patch_id"],
                )
            if run["status"] == "STAGED":
                result = _finalize_staged(connection, run=run)
                connection.execute("COMMIT")
                return result
            tolerance = (
                state.clock_tolerance_ms
                if clock_tolerance_ms is None
                else clock_tolerance_ms
            )
            if event["logical_clock_ms"] < state.logical_clock_watermark - tolerance:
                error = BusinessError(
                    E_LOGIC_CLOCK_REGRESSION,
                    "补丁逻辑时钟回拨超过容差",
                    view=event["view"],
                    edit_round=event["sequence"],
                    logical_clock_ms=event["logical_clock_ms"],
                    watermark_ms=state.logical_clock_watermark,
                    tolerance_ms=tolerance,
                )
                result = _record_failure(
                    connection,
                    patch_id=event["patch_id"],
                    event=event,
                    error=error,
                    generation=state.generation,
                )
                connection.execute("COMMIT")
                return result
            try:
                computed = compute_patch_candidate(state.model, event)
            except BusinessError as exc:
                if exc.edit_round is None:
                    exc.edit_round = event["sequence"]
                result = _record_failure(
                    connection,
                    patch_id=event["patch_id"],
                    event=event,
                    error=exc,
                    generation=state.generation,
                )
                connection.execute("COMMIT")
                return result
            target_generation = state.generation + 1
            rows = _build_committed_rows(state, computed)
            result = invalidation_result(
                computed,
                base_generation=state.generation,
                target_generation=target_generation,
            )
            _stage_snapshot(
                connection,
                current=state,
                event=event,
                target_generation=target_generation,
                result=result,
                rows=rows,
            )
            connection.execute("COMMIT")
            if os.environ.get("GSB_CRASH_AFTER_STAGE") == "1":
                os._exit(98)
            connection.execute("BEGIN IMMEDIATE")
            run = _read_patch_run(connection, event["patch_id"])
            if run is None:
                raise BusinessError(
                    E_STATE_CONFLICT,
                    "补丁运行记录丢失",
                    patch_id=event["patch_id"],
                )
            final_result = _finalize_staged(connection, run=run)
            connection.execute("COMMIT")
            return final_result
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


def apply_patch_stream(
    state_db: str | Path,
    events: list[dict[str, Any]],
    *,
    clock_tolerance_ms: int | None = None,
) -> dict[str, Any]:
    db_path = Path(state_db)
    if not db_path.exists():
        raise BusinessError(E_STATE_NOT_INITIALIZED, "状态数据库不存在，请先执行 load")
    results: list[dict[str, Any]] = []
    with writer_lock(db_path):
        reader = _connect_reader(db_path)
        try:
            if not _table_exists(reader, "pointer"):
                raise BusinessError(E_STATE_NOT_INITIALIZED, "状态数据库未初始化")
        finally:
            reader.close()
        for raw_event in events:
            try:
                event = normalize_patch_event(raw_event)
                result = _apply_event_locked(
                    db_path,
                    event,
                    clock_tolerance_ms=clock_tolerance_ms,
                )
                results.append(result)
            except BusinessError as exc:
                sequence = raw_event.get("sequence") if isinstance(raw_event, dict) else None
                results.append(
                    {
                        "status": "failed",
                        "duplicate": False,
                        "patch_id": (
                            raw_event.get("patch_id")
                            if isinstance(raw_event, dict)
                            else None
                        ),
                        "sequence": sequence,
                        "error": exc.to_dict(),
                    }
                )
    connection = _connect_reader(db_path)
    try:
        pointer = connection.execute("SELECT * FROM pointer WHERE singleton=1").fetchone()
    finally:
        connection.close()
    committed = [item for item in results if item.get("status") == "committed"]
    failed = [item for item in results if item.get("status") == "failed"]
    return {
        "ok": True,
        "state_db": str(db_path),
        "processed": len(results),
        "committed_count": len(committed),
        "failed_count": len(failed),
        "duplicate_count": sum(1 for item in committed if item.get("duplicate")),
        "plan_generation": pointer["plan_generation"] if pointer else None,
        "logical_clock_watermark": (
            pointer["logical_clock_watermark"] if pointer else None
        ),
        "results": results,
    }


def resume_state(
    state_db: str | Path,
    *,
    clock_tolerance_ms: int | None = None,
) -> dict[str, Any]:
    db_path = Path(state_db)
    if not db_path.exists():
        raise BusinessError(E_STATE_NOT_INITIALIZED, "状态数据库不存在，请先执行 load")
    recovered: list[dict[str, Any]] = []
    with writer_lock(db_path):
        connection = _connect_writer(db_path)
        try:
            pending = connection.execute(
                "SELECT * FROM patch_runs WHERE status IN ('PREPARED', 'STAGED') "
                "ORDER BY sequence, patch_id"
            ).fetchall()
            for run in pending:
                payload = json.loads(run["payload_json"])
                try:
                    if run["status"] == "STAGED":
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            current_run = _read_patch_run(connection, run["patch_id"])
                            result = _finalize_staged(connection, run=current_run)
                            connection.execute("COMMIT")
                        except BaseException:
                            if connection.in_transaction:
                                connection.execute("ROLLBACK")
                            raise
                    else:
                        result = _apply_event_locked(
                            db_path,
                            payload,
                            clock_tolerance_ms=clock_tolerance_ms,
                        )
                    recovered.append(
                        {
                            "patch_id": run["patch_id"],
                            "sequence": run["sequence"],
                            "status": result.get("status"),
                            "plan_generation": result.get("plan_generation"),
                            "recovered_from": run["status"],
                        }
                    )
                except BusinessError as exc:
                    recovered.append(
                        {
                            "patch_id": run["patch_id"],
                            "sequence": run["sequence"],
                            "status": "failed",
                            "recovered_from": run["status"],
                            "error": exc.to_dict(),
                        }
                    )
        finally:
            connection.close()
    status = status_state(db_path)
    return {
        "ok": True,
        "state_db": str(db_path),
        "recovered_count": len(recovered),
        "recovered": recovered,
        "plan_generation": status["plan_generation"],
        "logical_clock_watermark": status["logical_clock_watermark"],
        "pending_runs": status["pending_runs"],
    }


def status_state(state_db: str | Path) -> dict[str, Any]:
    connection = _connect_reader(state_db)
    try:
        pointer = connection.execute("SELECT * FROM pointer WHERE singleton=1").fetchone()
        if pointer is None:
            raise BusinessError(E_STATE_NOT_INITIALIZED, "状态指针不存在")
        rows = _read_rows(connection, pointer["plan_generation"])
        pending = connection.execute(
            "SELECT patch_id, sequence, status, target_generation FROM patch_runs "
            "WHERE status IN ('PREPARED', 'STAGED') ORDER BY sequence, patch_id"
        ).fetchall()
        run_counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM patch_runs GROUP BY status"
            ).fetchall()
        }
        event_count = connection.execute(
            "SELECT COUNT(*) AS count FROM invalidation_events"
        ).fetchone()["count"]
        return {
            "ok": True,
            "state_db": str(Path(state_db)),
            "initialized": True,
            "plan_generation": pointer["plan_generation"],
            "logical_clock_watermark": pointer["logical_clock_watermark"],
            "last_sequence": pointer["last_sequence"],
            "clock_tolerance_ms": pointer["stream_clock_tolerance_ms"],
            "view_count": len(rows),
            "stale_views": sorted(
                name for name, row in rows.items() if row.stale
            ),
            "views": [
                {
                    "name": name,
                    "plan_summary": rows[name].plan_summary,
                    "stale": rows[name].stale,
                    "dirty_nodes": rows[name].dirty_nodes,
                }
                for name in sorted(rows)
            ],
            "patch_run_counts": dict(sorted(run_counts.items())),
            "invalidation_event_count": event_count,
            "pending_runs": [
                {
                    "patch_id": row["patch_id"],
                    "sequence": row["sequence"],
                    "status": row["status"],
                    "target_generation": row["target_generation"],
                }
                for row in pending
            ],
        }
    finally:
        connection.close()


def verify_state(state_db: str | Path) -> dict[str, Any]:
    connection = _connect_reader(state_db)
    checks: list[dict[str, Any]] = []

    def add_check(name: str, passed: bool, **details: Any) -> None:
        checks.append(
            {
                "check": name,
                "ok": bool(passed),
                **details,
            }
        )

    try:
        integrity = [
            row[0]
            for row in connection.execute("PRAGMA integrity_check").fetchall()
        ]
        add_check("sqlite_integrity", integrity == ["ok"], result=integrity)
        pointer = connection.execute("SELECT * FROM pointer WHERE singleton=1").fetchone()
        add_check("pointer_exists", pointer is not None)
        if pointer is None:
            return {"ok": False, "checks": checks}
        expected_tables = {
            "settings",
            "snapshots",
            "snapshot_views",
            "pointer",
            "patch_runs",
            "invalidation_events",
        }
        existing_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        add_check(
            "required_tables",
            expected_tables.issubset(existing_tables),
            missing=sorted(expected_tables - existing_tables),
        )
        snapshot = connection.execute(
            "SELECT * FROM snapshots WHERE plan_generation=?",
            (pointer["plan_generation"],),
        ).fetchone()
        add_check(
            "pointer_snapshot",
            snapshot is not None and snapshot["status"] == "COMMITTED",
            plan_generation=pointer["plan_generation"],
            status=None if snapshot is None else snapshot["status"],
        )
        rows = connection.execute(
            "SELECT * FROM snapshot_views WHERE plan_generation=? ORDER BY view_name",
            (pointer["plan_generation"],),
        ).fetchall()
        add_check("snapshot_has_views", len(rows) > 0, view_count=len(rows))
        plan_errors: list[dict[str, str]] = []
        summary_errors: list[str] = []
        contract_errors: list[str] = []
        dirty_node_errors: list[dict[str, Any]] = []
        for row in rows:
            try:
                plan = json.loads(row["plan_json"])
            except json.JSONDecodeError:
                plan_errors.append({"view": row["view_name"], "error": "invalid_json"})
                continue
            errors = _verify_plan_document(plan)
            if errors:
                plan_errors.append(
                    {"view": row["view_name"], "errors": sorted(errors)}
                )
            if plan.get("plan_summary") != row["plan_summary"]:
                summary_errors.append(row["view_name"])
            if (
                plan.get("output_columns") != json.loads(row["output_columns_json"])
                or plan.get("column_dependencies")
                != json.loads(row["column_dependencies_json"])
            ):
                contract_errors.append(row["view_name"])
            available_nodes = {
                node["node_id"] for node in plan.get("node_index", [])
            }
            missing_dirty = sorted(
                set(json.loads(row["dirty_nodes_json"])) - available_nodes
            )
            if missing_dirty:
                dirty_node_errors.append(
                    {"view": row["view_name"], "missing_node_ids": missing_dirty}
                )
        add_check("plan_documents", not plan_errors, errors=plan_errors)
        add_check("plan_summaries", not summary_errors, views=summary_errors)
        add_check("plan_contracts", not contract_errors, views=contract_errors)
        add_check(
            "dirty_nodes_exist",
            not dirty_node_errors,
            errors=dirty_node_errors,
        )
        if snapshot is not None:
            hash_rows = [
                _row_fields(
                    view_name=row["view_name"],
                    definition_json=row["definition_json"],
                    sql=row["sql"],
                    executable_sql=row["executable_sql"],
                    output_columns_json=row["output_columns_json"],
                    column_dependencies_json=row["column_dependencies_json"],
                    dependencies_json=row["dependencies_json"],
                    plan_json=row["plan_json"],
                    plan_summary=row["plan_summary"],
                    stale=bool(row["stale"]),
                    dirty_nodes_json=row["dirty_nodes_json"],
                )
                for row in rows
            ]
            expected_record_hash = _snapshot_record_hash(
                generation=snapshot["plan_generation"],
                parent_generation=snapshot["parent_generation"],
                patch_id=snapshot["patch_id"],
                logical_clock_ms=snapshot["logical_clock_ms"],
                rows=hash_rows,
            )
            add_check(
                "snapshot_record_hash",
                expected_record_hash == snapshot["record_hash"],
                expected=snapshot["record_hash"],
                actual=expected_record_hash,
            )
        add_check(
            "snapshot_view_count",
            len(rows) == int(
                connection.execute(
                    "SELECT COUNT(*) FROM snapshot_views WHERE plan_generation=?",
                    (pointer["plan_generation"],),
                ).fetchone()[0]
            ),
            view_count=len(rows),
        )
        mixed = connection.execute(
            "SELECT COUNT(*) AS count FROM snapshot_views "
            "WHERE plan_generation=? AND plan_json = ''",
            (pointer["plan_generation"],),
        ).fetchone()["count"]
        add_check("no_partial_rows", mixed == 0, empty_plan_rows=mixed)
        unique_events = connection.execute(
            "SELECT COUNT(*) AS total, COUNT(DISTINCT event_id) AS unique_count "
            "FROM invalidation_events"
        ).fetchone()
        add_check(
            "unique_invalidation_events",
            unique_events["total"] == unique_events["unique_count"],
            total=unique_events["total"],
            unique=unique_events["unique_count"],
        )
        future_events = connection.execute(
            "SELECT COUNT(*) AS count FROM invalidation_events "
            "WHERE plan_generation > ?",
            (pointer["plan_generation"],),
        ).fetchone()["count"]
        add_check("no_future_events", future_events == 0, count=future_events)
        staged = connection.execute(
            "SELECT COUNT(*) AS count FROM snapshots WHERE status='STAGED'"
        ).fetchone()["count"]
        add_check("no_unfinished_snapshots", staged == 0, staged_count=staged)
        pending = connection.execute(
            "SELECT COUNT(*) AS count FROM patch_runs "
            "WHERE status IN ('PREPARED', 'STAGED')"
        ).fetchone()["count"]
        add_check("no_pending_runs", pending == 0, pending_count=pending)
        committed = connection.execute(
            "SELECT COUNT(*) AS count FROM patch_runs WHERE status='COMMITTED'"
        ).fetchone()["count"]
        add_check(
            "committed_run_generations",
            connection.execute(
                "SELECT COUNT(*) AS count FROM patch_runs "
                "WHERE status='COMMITTED' AND (target_generation IS NULL "
                "OR target_generation > ?)",
                (pointer["plan_generation"],),
            ).fetchone()["count"]
            == 0,
            committed_count=committed,
        )
        return {
            "ok": all(check["ok"] for check in checks),
            "state_db": str(Path(state_db)),
            "plan_generation": pointer["plan_generation"],
            "view_count": len(rows),
            "checks": checks,
        }
    finally:
        connection.close()

