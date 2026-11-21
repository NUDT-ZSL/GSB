"""SQLite persistence, atomic refresh transactions, replacement, and recovery."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .definitions import source_digest
from .errors import EngineError, fail
from .logs import ChangeEvent, LogPackage, parse_logs, validate_log_against_index
from .merge import parse_index_payload
from .models import UnifiedIndex, ViewSpec
from .package import DefinitionPackage, load_package
from .runtime import (
    ViewState,
    apply_event,
    empty_base_rows,
    empty_view_state,
    empty_states,
    recompute_from_base_rows,
    replay,
    state_mismatch,
)
from .util import (
    FileLock,
    canonical_json,
    digest_json,
    file_lock,
    pretty_json,
    require_int,
)

SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_version INTEGER NOT NULL,
    index_digest TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    generation_floor INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    definition_hashes_json TEXT NOT NULL,
    index_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS view_specs (
    definition_id INTEGER NOT NULL,
    view_name TEXT NOT NULL,
    status TEXT NOT NULL,
    generation INTEGER NOT NULL,
    execution_digest TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    PRIMARY KEY (definition_id, view_name)
);
CREATE TABLE IF NOT EXISTS current_pointer (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    definition_id INTEGER NOT NULL,
    package_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS package_history (
    package_version INTEGER PRIMARY KEY,
    definition_id INTEGER NOT NULL,
    generation_floor INTEGER NOT NULL,
    definition_digest TEXT NOT NULL,
    definition_hashes_json TEXT NOT NULL,
    log_segment_hashes_json TEXT NOT NULL,
    log_start_seq INTEGER,
    log_end_seq INTEGER,
    log_digest TEXT,
    classification TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_decisions (
    definition_id INTEGER NOT NULL,
    view_name TEXT NOT NULL,
    used_in_index INTEGER NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (definition_id, view_name)
);
CREATE TABLE IF NOT EXISTS replacement_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_definition_id INTEGER,
    to_definition_id INTEGER NOT NULL,
    from_package_version INTEGER,
    to_package_version INTEGER NOT NULL,
    classification TEXT NOT NULL,
    actions_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS base_rows (
    base_table TEXT NOT NULL,
    row_key TEXT NOT NULL,
    row_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    PRIMARY KEY (base_table, row_key)
);
CREATE TABLE IF NOT EXISTS view_states (
    view_name TEXT PRIMARY KEY,
    definition_id INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    applied_seq INTEGER NOT NULL,
    result_digest TEXT NOT NULL,
    recompute_count INTEGER NOT NULL,
    state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applied_changes (
    seq INTEGER PRIMARY KEY,
    content_digest TEXT NOT NULL,
    base_table TEXT NOT NULL,
    row_key TEXT NOT NULL,
    op TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    segment_file TEXT NOT NULL,
    line_number INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invalidation_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    definition_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    view_name TEXT NOT NULL,
    action TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    path_json TEXT NOT NULL,
    result_changed INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    UNIQUE (definition_id, seq, view_name)
);
CREATE TABLE IF NOT EXISTS refresh_runs (
    run_id TEXT PRIMARY KEY,
    definition_id INTEGER NOT NULL,
    start_watermark INTEGER NOT NULL,
    target_seq INTEGER,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


def _lock_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".lock")


def _connect_write(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _connect_read(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise fail("E_DB_NOT_FOUND", "database does not exist", path=str(db_path))
    connection = sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro",
        uri=True,
        timeout=30.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA query_only=ON")
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    existing = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "schema_meta" not in existing:
        if existing:
            raise fail(
                "E_DB_SCHEMA",
                "database contains unrelated tables and is not an MV engine database",
                tables=sorted(existing),
            )
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        return
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None or int(row["value"]) != SCHEMA_VERSION:
        raise fail(
            "E_DB_SCHEMA",
            "unsupported database schema version",
            actual=None if row is None else row["value"],
            expected=SCHEMA_VERSION,
        )


@contextmanager
def write_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = _connect_write(db_path)
    try:
        _ensure_schema(connection)
        yield connection
    finally:
        connection.close()


@contextmanager
def read_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = _connect_read(db_path)
    try:
        _ensure_schema_readonly(connection)
        yield connection
    finally:
        connection.close()


def _ensure_schema_readonly(connection: sqlite3.Connection) -> None:
    existing = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "schema_meta" not in existing:
        raise fail("E_DB_SCHEMA", "database is not an MV engine database")
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None or int(row["value"]) != SCHEMA_VERSION:
        raise fail("E_DB_SCHEMA", "unsupported database schema version")


def _meta_get(connection: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = connection.execute("SELECT value_json FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise fail(
            "E_STATE_CORRUPT",
            "persisted metadata value is not valid JSON",
            key=key,
            detail=str(exc),
        )


def _meta_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT INTO meta(key, value_json) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
        (key, canonical_json(value)),
    )


def _definition_hashes(package: DefinitionPackage) -> dict[str, str]:
    hashes = {
        "v1": package.v1.file_sha256,
        "v2_manifest": package.v2.manifest_sha256,
    }
    for shard, value in sorted(package.v2.shard_hashes.items()):
        hashes[f"shard:{shard}"] = value
    return hashes


def _log_ranges(package: DefinitionPackage) -> tuple[dict[str, str], int | None, int | None]:
    if package.logs is None:
        return {}, None, None
    return dict(package.logs.segment_hashes), package.logs.start_seq, package.logs.end_seq


def _insert_package_history(
    connection: sqlite3.Connection,
    *,
    package: DefinitionPackage,
    definition_id: int,
    classification: str,
) -> None:
    segment_hashes, start_seq, end_seq = _log_ranges(package)
    connection.execute(
        """
        INSERT INTO package_history(
            package_version, definition_id, generation_floor,
            definition_digest, definition_hashes_json,
            log_segment_hashes_json, log_start_seq, log_end_seq,
            log_digest, classification
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            package.package_version,
            definition_id,
            package.view_generation_floor,
            package.definition_digest,
            canonical_json(_definition_hashes(package)),
            canonical_json(segment_hashes),
            start_seq,
            end_seq,
            package.log_digest,
            classification,
        ),
    )
    _meta_set(connection, "current_package_path", str(package.manifest_path.resolve()))
    if package.logs is not None:
        _meta_set(connection, "current_log_path", str(package.logs.path.resolve()))


def _insert_definition_rows(
    connection: sqlite3.Connection,
    *,
    package: DefinitionPackage,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO definitions(
            package_version, index_digest, definition_digest,
            generation_floor, logical_clock_ms,
            definition_hashes_json, index_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            package.package_version,
            package.index.index_digest,
            package.definition_digest,
            package.view_generation_floor,
            package.logical_clock_ms,
            canonical_json(_definition_hashes(package)),
            canonical_json(package.index.payload),
        ),
    )
    definition_id = int(cursor.lastrowid)
    for name, spec in sorted(package.index.views.items()):
        connection.execute(
            """
            INSERT INTO view_specs(
                definition_id, view_name, status, generation,
                execution_digest, spec_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                definition_id,
                name,
                spec.status,
                spec.generation,
                spec.state_digest,
                canonical_json(spec.as_index_dict()),
            ),
        )
    for migration in package.index.migrations:
        connection.execute(
            """
            INSERT INTO migration_decisions(
                definition_id, view_name, used_in_index, decision_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                definition_id,
                migration["view_name"],
                1 if migration.get("used_in_index") else 0,
                canonical_json(migration),
            ),
        )
    return definition_id


def _insert_initial_states(
    connection: sqlite3.Connection,
    *,
    definition_id: int,
    index: UnifiedIndex,
) -> None:
    for name, spec in sorted(index.active_views().items()):
        state = empty_view_state_for_write(spec)
        _write_state(connection, definition_id=definition_id, state=state)


def empty_view_state_for_write(spec: ViewSpec) -> ViewState:
    return empty_view_state(spec)


def _write_state(
    connection: sqlite3.Connection,
    *,
    definition_id: int,
    state: ViewState,
) -> None:
    connection.execute(
        """
        INSERT INTO view_states(
            view_name, definition_id, generation, status,
            applied_seq, result_digest, recompute_count, state_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(view_name) DO UPDATE SET
            definition_id=excluded.definition_id,
            generation=excluded.generation,
            status=excluded.status,
            applied_seq=excluded.applied_seq,
            result_digest=excluded.result_digest,
            recompute_count=excluded.recompute_count,
            state_json=excluded.state_json
        """,
        (
            state.view_name,
            definition_id,
            state.generation,
            state.status,
            state.applied_seq,
            state.result_digest,
            state.recompute_count,
            canonical_json(state.as_json()),
        ),
    )


def _read_view_states(connection: sqlite3.Connection) -> dict[str, ViewState]:
    result: dict[str, ViewState] = {}
    for row in connection.execute(
        "SELECT view_name, state_json FROM view_states ORDER BY view_name"
    ):
        try:
            value = json.loads(row["state_json"])
            state = ViewState.from_json(value)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise fail(
                "E_STATE_CORRUPT",
                "persisted view state is malformed",
                view=row["view_name"],
                detail=str(exc),
            )
        if state.result_digest != digest_json(state.fingerprint_payload()):
            raise fail(
                "E_STATE_CORRUPT",
                "persisted view result digest is inconsistent",
                view=state.view_name,
            )
        result[row["view_name"]] = state
    return result


def _read_base_rows(connection: sqlite3.Connection) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for row in connection.execute(
        "SELECT base_table, row_key, row_json, source_json FROM base_rows "
        "ORDER BY base_table, row_key"
    ):
        try:
            result.setdefault(row["base_table"], {})[row["row_key"]] = {
                "row": json.loads(row["row_json"]),
                "source": json.loads(row["source_json"]),
            }
        except (json.JSONDecodeError, TypeError) as exc:
            raise fail(
                "E_STATE_CORRUPT",
                "persisted base row is malformed",
                table=row["base_table"],
                key=row["row_key"],
                detail=str(exc),
            )
    return result


def _write_base_event(
    connection: sqlite3.Connection,
    event: ChangeEvent,
) -> None:
    if event.op == "delete":
        connection.execute(
            "DELETE FROM base_rows WHERE base_table=? AND row_key=?",
            (event.base_table, event.key),
        )
        return
    source = {
        "line_number": event.line_number,
        "seq": event.seq,
        "segment_file": event.segment_file,
        "segment_id": event.segment_id,
    }
    connection.execute(
        """
        INSERT INTO base_rows(base_table, row_key, row_json, source_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(base_table, row_key) DO UPDATE SET
            row_json=excluded.row_json,
            source_json=excluded.source_json
        """,
        (
            event.base_table,
            event.key,
            canonical_json(event.after),
            canonical_json(source),
        ),
    )


def _current_definition(
    connection: sqlite3.Connection,
) -> tuple[int, int, int, int, UnifiedIndex]:
    row = connection.execute(
        """
        SELECT d.id AS definition_id, p.package_version AS package_version,
               d.generation_floor,
               d.logical_clock_ms, d.index_json
        FROM current_pointer p
        JOIN definitions d ON d.id = p.definition_id
        WHERE p.singleton = 1
        """
    ).fetchone()
    if row is None:
        raise fail("E_DEFINITION_NOT_LOADED", "no current definition pointer is installed")
    index = parse_index_payload(json.loads(row["index_json"]))
    return (
        int(row["definition_id"]),
        int(row["package_version"]),
        int(row["generation_floor"]),
        int(row["logical_clock_ms"]),
        index,
    )


def _current_package_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT ph.*
        FROM current_pointer p
        JOIN package_history ph ON ph.package_version = p.package_version
        WHERE p.singleton = 1
        """
    ).fetchone()


def initialize(package: DefinitionPackage, db_path: Path, *, timeout: float = 30.0) -> dict[str, Any]:
    with file_lock(_lock_path(db_path), timeout=timeout):
        with write_connection(db_path) as connection:
            existing = connection.execute(
                "SELECT COUNT(*) AS count FROM current_pointer"
            ).fetchone()["count"]
            if existing:
                raise fail(
                    "E_DB_ALREADY_LOADED",
                    "database already has a current definition pointer",
                    path=str(db_path),
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                definition_id = _insert_definition_rows(connection, package=package)
                _insert_initial_states(
                    connection,
                    definition_id=definition_id,
                    index=package.index,
                )
                connection.execute(
                    "INSERT INTO current_pointer(singleton, definition_id, package_version) "
                    "VALUES(1, ?, ?)",
                    (definition_id, package.package_version),
                )
                _insert_package_history(
                    connection,
                    package=package,
                    definition_id=definition_id,
                    classification="initial",
                )
                _meta_set(connection, "watermark_seq", 0)
                _meta_set(connection, "logical_clock_ms", 0)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            return {
                "classification": "initial",
                "definition_id": definition_id,
                "index_digest": package.index.index_digest,
                "package_version": package.package_version,
                "view_generation_floor": package.view_generation_floor,
                "views": sorted(package.index.views),
            }


def classify_package(
    *,
    connection: sqlite3.Connection,
    package: DefinitionPackage,
) -> str:
    current = _current_definition(connection)
    _, current_version, current_floor, _, current_index = current
    if package.package_version < current_version or package.view_generation_floor < current_floor:
        raise fail(
            "E_VERSION_ROLLBACK",
            "package version or view generation floor would move backwards",
            incoming_package_version=package.package_version,
            current_package_version=current_version,
            incoming_generation_floor=package.view_generation_floor,
            current_generation_floor=current_floor,
        )
    current_row = _current_package_row(connection)
    assert current_row is not None
    current_hashes = json.loads(current_row["definition_hashes_json"])
    incoming_hashes = _definition_hashes(package)
    same_definition = (
        package.definition_digest == current_index.index_digest
        and incoming_hashes == current_hashes
    )
    incoming_log_hashes, _, incoming_end = _log_ranges(package)
    current_log_hashes = json.loads(current_row["log_segment_hashes_json"])
    log_extends = all(
        incoming_log_hashes.get(key) == value
        for key, value in current_log_hashes.items()
    )
    same_logs = incoming_log_hashes == current_log_hashes
    if package.package_version == current_version:
        if same_definition and (package.logs is None or same_logs):
            return "no_op"
        if same_definition and log_extends:
            raise fail(
                "E_VERSION_CONFLICT",
                "a new log segment requires a higher package version",
                package_version=package.package_version,
            )
        raise fail(
            "E_VERSION_CONFLICT",
            "package version is unchanged but its definition or log identity differs",
            package_version=package.package_version,
            current_definition_digest=current_index.index_digest,
            incoming_definition_digest=package.definition_digest,
        )
    if same_definition:
        if package.logs is None:
            return "append"
        if log_extends:
            return "append"
        raise fail(
            "E_VERSION_CONFLICT",
            "new log package does not preserve the committed segment prefix",
            current_segments=sorted(current_log_hashes),
            incoming_segments=sorted(incoming_log_hashes),
        )
    return "full_replace"


def _append_package(
    connection: sqlite3.Connection,
    *,
    package: DefinitionPackage,
) -> dict[str, Any]:
    definition_id, current_version, _, _, _ = _current_definition(connection)
    connection.execute(
        "UPDATE current_pointer SET package_version=? WHERE singleton=1",
        (package.package_version,),
    )
    _insert_package_history(
        connection,
        package=package,
        definition_id=definition_id,
        classification="append",
    )
    return {
        "classification": "append",
        "definition_id": definition_id,
        "from_package_version": current_version,
        "package_version": package.package_version,
        "replacement_actions": [],
    }


def _stage_definition(
    connection: sqlite3.Connection,
    *,
    package: DefinitionPackage,
) -> int:
    """Stage immutable definition rows in temporary tables before pointer switch."""

    connection.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS definition_stage (
            index_digest TEXT NOT NULL,
            definition_digest TEXT NOT NULL,
            package_version INTEGER NOT NULL,
            generation_floor INTEGER NOT NULL,
            logical_clock_ms INTEGER NOT NULL,
            definition_hashes_json TEXT NOT NULL,
            index_json TEXT NOT NULL
        )
        """
    )
    connection.execute("DELETE FROM definition_stage")
    connection.execute(
        "INSERT INTO definition_stage VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            package.index.index_digest,
            package.definition_digest,
            package.package_version,
            package.view_generation_floor,
            package.logical_clock_ms,
            canonical_json(_definition_hashes(package)),
            canonical_json(package.index.payload),
        ),
    )
    staged = connection.execute("SELECT * FROM definition_stage").fetchone()
    if staged is None:
        raise fail("E_REPLACE_STAGE_FAILED", "definition staging table is empty")
    definition_id = _insert_definition_rows(connection, package=package)
    connection.execute("DELETE FROM definition_stage")
    return definition_id


def _replacement_actions(
    *,
    old_index: UnifiedIndex,
    new_index: UnifiedIndex,
    old_states: dict[str, ViewState],
) -> list[dict[str, Any]]:
    old_specs = old_index.views
    actions: list[dict[str, Any]] = []
    for name, spec in sorted(new_index.views.items()):
        if spec.status != "active":
            continue
        old_spec = old_specs.get(name)
        old_state = old_states.get(name)
        if old_spec is None or old_spec.status != "active" or old_state is None:
            actions.append(
                {
                    "action": "recomputed",
                    "reason": "new_active_view",
                    "view_name": name,
                }
            )
        elif old_spec.state_digest == spec.state_digest:
            actions.append(
                {
                    "action": "preserved",
                    "reason": "execution_spec_unchanged",
                    "view_name": name,
                }
            )
        else:
            actions.append(
                {
                    "action": "recomputed",
                    "reason": "execution_spec_changed",
                    "view_name": name,
                }
            )
    for name, spec in sorted(old_specs.items()):
        if spec.status != "active":
            continue
        new_spec = new_index.views.get(name)
        if new_spec is None:
            actions.append({"action": "removed", "reason": "view_absent", "view_name": name})
        elif new_spec.status == "tombstoned":
            actions.append({"action": "removed", "reason": "tombstoned", "view_name": name})
    return sorted(actions, key=lambda item: (item["view_name"], item["action"]))


def _apply_full_replacement(
    connection: sqlite3.Connection,
    *,
    package: DefinitionPackage,
) -> dict[str, Any]:
    (
        old_definition_id,
        current_version,
        _,
        _,
        old_index,
    ) = _current_definition(connection)
    old_states = _read_view_states(connection)
    base_rows = _read_base_rows(connection)
    watermark = _meta_get(connection, "watermark_seq", 0)
    logical_clock = _meta_get(connection, "logical_clock_ms", 0)
    actions = _replacement_actions(
        old_index=old_index,
        new_index=package.index,
        old_states=old_states,
    )
    action_by_name = {action["view_name"]: action for action in actions}

    new_definition_id = _stage_definition(connection, package=package)
    new_specs = package.index.active_views()
    new_states: dict[str, ViewState] = {}
    old_active_names = set(old_index.active_views())
    changed_results: dict[str, list[str]] = {}
    for name in package.index.topo_order():
        spec = new_specs[name]
        action = action_by_name.get(name)
        if action is None:
            raise fail("E_REPLACE_STAGE_FAILED", "missing replacement decision", view=name)
        upstream_paths = [
            changed_results[upstream] + [f"view:{name}"]
            for upstream in spec.upstream_views
            if upstream in changed_results
        ]
        if action["action"] == "preserved" and not upstream_paths:
            state = old_states[name]
            state.generation = spec.generation
            state.status = "active"
        else:
            if action["action"] == "preserved":
                action["action"] = "recomputed"
                action["reason"] = "upstream_result_changed"
                action["path"] = sorted(upstream_paths)[0]
            state = recompute_from_base_rows(
                spec,
                base_rows,
                applied_seq=watermark,
                logical_clock_ms=logical_clock,
                trigger="replace:definition",
                path=action.get("path", ["replace:definition", f"view:{name}"]),
                increment_recompute=True,
            )
        old_state = old_states.get(name)
        if (
            old_state is None
            or old_state.result_digest != state.result_digest
            or action["reason"] == "new_active_view"
        ):
            changed_results[name] = action.get(
                "path",
                ["replace:definition", f"view:{name}"],
            )
        new_states[name] = state
    for name in sorted(old_active_names - set(new_specs)):
        connection.execute("DELETE FROM view_states WHERE view_name=?", (name,))
    for name, state in sorted(new_states.items()):
        _write_state(connection, definition_id=new_definition_id, state=state)

    connection.execute(
        "UPDATE current_pointer SET definition_id=?, package_version=? WHERE singleton=1",
        (new_definition_id, package.package_version),
    )
    _insert_package_history(
        connection,
        package=package,
        definition_id=new_definition_id,
        classification="full_replace",
    )
    connection.execute(
        """
        INSERT INTO replacement_decisions(
            from_definition_id, to_definition_id, from_package_version,
            to_package_version, classification, actions_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            old_definition_id,
            new_definition_id,
            current_version,
            package.package_version,
            "full_replace",
            canonical_json(actions),
        ),
    )
    return {
        "classification": "full_replace",
        "definition_id": new_definition_id,
        "from_definition_id": old_definition_id,
        "from_package_version": current_version,
        "index_digest": package.index.index_digest,
        "package_version": package.package_version,
        "replacement_actions": actions,
    }


def replace_package(
    *,
    db_path: Path,
    package: DefinitionPackage,
    timeout: float = 30.0,
    failpoint: str | None = None,
) -> dict[str, Any]:
    with file_lock(_lock_path(db_path), timeout=timeout):
        with write_connection(db_path) as connection:
            classification = classify_package(connection=connection, package=package)
            if classification == "no_op":
                return {
                    "classification": "no_op",
                    "index_digest": package.index.index_digest,
                    "package_version": package.package_version,
                    "replacement_actions": [],
                }
            connection.execute("BEGIN IMMEDIATE")
            try:
                if classification == "append":
                    result = _append_package(connection, package=package)
                else:
                    result = _apply_full_replacement(connection, package=package)
                    if failpoint == "replace_after_stage":
                        os._exit(137)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            return result


def _next_expected_seq(connection: sqlite3.Connection, watermark: int, event: ChangeEvent) -> None:
    if event.seq <= watermark:
        return
    if event.seq != watermark + 1:
        raise fail(
            "E_LOG_GAP",
            "change sequence is not contiguous with the persisted watermark",
            watermark=watermark,
            seq=event.seq,
        )


def refresh_logs(
    *,
    db_path: Path,
    logs: LogPackage,
    until_seq: int | None = None,
    kill_after: int | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    events = tuple(
        event for event in logs.events
        if until_seq is None or event.seq <= until_seq
    )
    if not events:
        raise fail("E_LOG_EMPTY", "no log events are available for the requested range")
    if not db_path.is_file():
        raise fail("E_DB_NOT_FOUND", "database does not exist", path=str(db_path))
    with read_connection(db_path) as preflight:
        _, _, _, _, preflight_index = _current_definition(preflight)
        validate_log_against_index(events, preflight_index)
    with file_lock(_lock_path(db_path), timeout=timeout):
        with write_connection(db_path) as connection:
            definition_id, _, _, _, index = _current_definition(connection)
            validate_log_against_index(events, index)
            watermark = _meta_get(connection, "watermark_seq", 0)
            if watermark == 0 and logs.start_seq != 1:
                raise fail(
                    "E_LOG_GAP",
                    "initial refresh requires a log chain beginning at seq 1",
                    actual_start=logs.start_seq,
                )
            applied_rows = {
                int(row["seq"]): row["content_digest"]
                for row in connection.execute(
                    "SELECT seq, content_digest FROM applied_changes"
                )
            }
            pending: list[ChangeEvent] = []
            skipped: list[int] = []
            for event in events:
                if event.seq in applied_rows:
                    if applied_rows[event.seq] != event.content_digest:
                        raise fail(
                            "E_LOG_SEQ_CONFLICT",
                            "same change seq was submitted with a different digest",
                            seq=event.seq,
                            existing=applied_rows[event.seq],
                            incoming=event.content_digest,
                        )
                    skipped.append(event.seq)
                    continue
                if event.seq <= watermark:
                    raise fail(
                        "E_STATE_CORRUPT",
                        "watermark is ahead of the applied-change ledger",
                        seq=event.seq,
                        watermark=watermark,
                    )
                pending.append(event)
            if not pending:
                return {
                    "applied": [],
                    "decisions": [],
                    "logical_clock_ms": _meta_get(connection, "logical_clock_ms", 0),
                    "run_status": "no_op",
                    "skipped": skipped,
                    "watermark_seq": watermark,
                }
            specs = index.active_views()
            order = index.topo_order()
            base_rows = _read_base_rows(connection)
            for table in index.base_schema:
                base_rows.setdefault(table, {})
            states = _read_view_states(connection)
            for name, spec in specs.items():
                if name not in states:
                    states[name] = empty_view_state_for_write(spec)
            run_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO refresh_runs(
                    run_id, definition_id, start_watermark, target_seq,
                    status, detail_json
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    definition_id,
                    watermark,
                    events[-1].seq,
                    canonical_json({"segments": sorted(logs.segment_hashes)}),
                ),
            )
            applied: list[int] = []
            decisions_out: list[dict[str, Any]] = []
            commit_count = 0
            for event in pending:
                _next_expected_seq(connection, watermark, event)
                decision = apply_event(
                    index=index,
                    specs=specs,
                    topo_order=order,
                    base_rows=base_rows,
                    states=states,
                    event=event,
                )
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current_id = connection.execute(
                        "SELECT definition_id FROM current_pointer WHERE singleton=1"
                    ).fetchone()["definition_id"]
                    if int(current_id) != definition_id:
                        raise fail(
                            "E_DEFINITION_CHANGED",
                            "definition pointer changed during refresh",
                        )
                    prior = connection.execute(
                        "SELECT content_digest FROM applied_changes WHERE seq=?",
                        (event.seq,),
                    ).fetchone()
                    if prior is not None:
                        if prior["content_digest"] != event.content_digest:
                            raise fail(
                                "E_LOG_SEQ_CONFLICT",
                                "same change seq was committed with a different digest",
                                seq=event.seq,
                            )
                    else:
                        _write_base_event(connection, event)
                        for name in order:
                            _write_state(
                                connection,
                                definition_id=definition_id,
                                state=states[name],
                            )
                        connection.execute(
                            """
                            INSERT INTO applied_changes(
                                seq, content_digest, base_table, row_key, op,
                                logical_clock_ms, segment_id, segment_file, line_number
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                event.seq,
                                event.content_digest,
                                event.base_table,
                                event.key,
                                event.op,
                                event.logical_clock_ms,
                                event.segment_id,
                                event.segment_file,
                                event.line_number,
                            ),
                        )
                        for item in decision["decisions"]:
                            connection.execute(
                                """
                                INSERT INTO invalidation_decisions(
                                    definition_id, seq, view_name, action,
                                    trigger_source, path_json, result_changed,
                                    segment_id
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    definition_id,
                                    event.seq,
                                    item["view_name"],
                                    item["action"],
                                    item["trigger"],
                                    canonical_json(item["path"]),
                                    1 if item["result_changed"] else 0,
                                    event.segment_id,
                                ),
                            )
                        _meta_set(connection, "watermark_seq", event.seq)
                        _meta_set(connection, "logical_clock_ms", event.logical_clock_ms)
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                watermark = event.seq
                applied.append(event.seq)
                decisions_out.append(decision)
                commit_count += 1
                if kill_after is not None and commit_count >= kill_after:
                    os._exit(137)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE refresh_runs SET status='success', detail_json=? WHERE run_id=?",
                    (canonical_json({"applied_count": len(applied)}), run_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            return {
                "applied": applied,
                "decisions": decisions_out,
                "logical_clock_ms": _meta_get(connection, "logical_clock_ms", 0),
                "run_status": "success",
                "skipped": skipped,
                "watermark_seq": watermark,
            }


def resume(
    *,
    db_path: Path,
    package: DefinitionPackage | None = None,
    logs: LogPackage | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if logs is None:
        if package is not None:
            logs = package.logs
        else:
            with write_connection(db_path) as connection:
                stored = _meta_get(connection, "current_log_path")
            if not stored:
                raise fail("E_RESUME_NO_LOG", "no log package is recorded for resume")
            logs = parse_logs(Path(stored))
    if logs is None:
        raise fail("E_RESUME_NO_LOG", "resume requires a log package")
    result = refresh_logs(db_path=db_path, logs=logs, timeout=timeout)
    with file_lock(_lock_path(db_path), timeout=timeout):
        with write_connection(db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                recovered = connection.execute(
                    "SELECT COUNT(*) AS count FROM refresh_runs WHERE status='running'"
                ).fetchone()["count"]
                connection.execute(
                    "UPDATE refresh_runs SET status='recovered', detail_json=? "
                    "WHERE status='running'",
                    (canonical_json({"recovered_by": "resume"}),),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
    result["recovered_runs"] = int(recovered)
    result["run_status"] = "resumed"
    return result


def status(db_path: Path) -> dict[str, Any]:
    with read_connection(db_path) as connection:
        connection.execute("BEGIN")
        try:
            definition_id, package_version, floor, definition_clock, index = _current_definition(connection)
            watermark = _meta_get(connection, "watermark_seq", 0)
            logical_clock = _meta_get(connection, "logical_clock_ms", 0)
            states = _read_view_states(connection)
            migrations = connection.execute(
                "SELECT COUNT(*) AS count FROM migration_decisions WHERE definition_id=?",
                (definition_id,),
            ).fetchone()["count"]
            running = connection.execute(
                "SELECT COUNT(*) AS count FROM refresh_runs WHERE status='running'"
            ).fetchone()["count"]
            views: list[dict[str, Any]] = []
            for name, spec in sorted(index.views.items()):
                state = states.get(name)
                views.append(
                    {
                        "absolute_refresh_ms": spec.absolute_refresh_ms,
                        "applied_seq": None if state is None else state.applied_seq,
                        "due_at_clock": spec.absolute_refresh_ms <= logical_clock,
                        "generation": spec.generation,
                        "last_recomputed_seq": None if state is None else state.last_recomputed_seq,
                        "name": name,
                        "recompute_count": None if state is None else state.recompute_count,
                        "result_digest": None if state is None else state.result_digest,
                        "status": spec.status,
                    }
                )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return {
        "current_package_version": package_version,
        "definition_digest": index.definition_digest,
        "definition_logical_clock_ms": definition_clock,
        "incomplete_refresh_runs": int(running),
        "index_digest": index.index_digest,
        "logical_clock_ms": logical_clock,
        "migration_decision_count": int(migrations),
        "view_generation_floor": floor,
        "views": views,
        "watermark_seq": watermark,
    }


def query(
    *,
    db_path: Path,
    view_name: str | None = None,
    clock_ms: int | None = None,
) -> dict[str, Any]:
    if clock_ms is not None and clock_ms < 0:
        raise fail("E_SCHEMA_INVALID", "clock_ms must be non-negative")
    with read_connection(db_path) as connection:
        connection.execute("BEGIN")
        try:
            _, _, _, _, index = _current_definition(connection)
            states = _read_view_states(connection)
            watermark = _meta_get(connection, "watermark_seq", 0)
            effective_clock = (
                _meta_get(connection, "logical_clock_ms", 0)
                if clock_ms is None
                else clock_ms
            )
            if view_name is not None:
                spec = index.views.get(view_name)
                if spec is None or spec.status != "active":
                    raise fail("E_VIEW_NOT_FOUND", "active view does not exist", view=view_name)
                selected = {view_name: spec}
            else:
                selected = index.active_views()
            views: list[dict[str, Any]] = []
            for name, spec in sorted(selected.items()):
                state = states.get(name)
                if state is None:
                    raise fail("E_STATE_CORRUPT", "active view has no persisted state", view=name)
                visible = effective_clock >= spec.absolute_refresh_ms
                views.append(
                    {
                        "absolute_refresh_ms": spec.absolute_refresh_ms,
                        "applied_seq": state.applied_seq,
                        "due": visible,
                        "generation": spec.generation,
                        "metrics": dict(state.metrics) if visible else None,
                        "name": name,
                        "result_digest": state.result_digest if visible else None,
                        "rows": state.row_entries() if visible else [],
                        "visible": visible,
                    }
                )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return {
        "logical_clock_ms": effective_clock,
        "views": views,
        "watermark_seq": watermark,
    }


def _load_snapshot(path: Path) -> dict[str, Any]:
    from .util import read_json, require_dict, require_int, require_str

    value = read_json(path, code="E_SNAPSHOT_INVALID")
    value = require_dict(value, "snapshot", code="E_SNAPSHOT_INVALID")
    if value.get("format") != "mv-snapshot-v1":
        raise fail("E_SNAPSHOT_INVALID", "snapshot must declare format=mv-snapshot-v1")
    require_int(value.get("snapshot_version"), "snapshot_version", minimum=0, code="E_SNAPSHOT_INVALID")
    require_int(value.get("watermark_seq"), "watermark_seq", minimum=0, code="E_SNAPSHOT_INVALID")
    require_str(value.get("definition_digest"), "definition_digest", code="E_SNAPSHOT_INVALID")
    require_dict(value.get("views"), "snapshot views", code="E_SNAPSHOT_INVALID")
    return value


def _snapshot_state_map(snapshot: dict[str, Any]) -> dict[str, ViewState]:
    result: dict[str, ViewState] = {}
    for name, item in snapshot["views"].items():
        result[name] = ViewState.from_json(item)
    return result


def verify(
    *,
    db_path: Path,
    logs: LogPackage,
    snapshots_dir: Path | None = None,
) -> dict[str, Any]:
    with read_connection(db_path) as connection:
        connection.execute("BEGIN")
        try:
            _, _, _, _, index = _current_definition(connection)
            actual_states = _read_view_states(connection)
            watermark = _meta_get(connection, "watermark_seq", 0)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    validate_log_against_index(logs.events, index)
    if logs.start_seq != 1:
        raise fail(
            "E_LOG_GAP",
            "deep verification requires a complete log chain beginning at seq 1",
            actual_start=logs.start_seq,
        )
    _, expected_states, _ = replay(index=index, events=logs.events)
    event_by_seq = {event.seq: event for event in logs.events}
    first = state_mismatch(
        expected=expected_states,
        actual=actual_states,
        event_by_seq=event_by_seq,
    )
    snapshot_results: list[dict[str, Any]] = []
    if snapshots_dir is not None:
        if not snapshots_dir.is_dir():
            raise fail("E_INPUT_NOT_FOUND", "snapshot directory does not exist", path=str(snapshots_dir))
        for path in sorted(snapshots_dir.glob("*.json")):
            snapshot = _load_snapshot(path)
            if snapshot["definition_digest"] != index.definition_digest:
                snapshot_results.append(
                    {
                        "definition_match": False,
                        "name": path.name,
                        "ok": False,
                    }
                )
                continue
            _, states_at_snapshot, _ = replay(
                index=index,
                events=logs.events,
                through_seq=snapshot["watermark_seq"],
            )
            mismatch = state_mismatch(
                expected=_snapshot_state_map(snapshot),
                actual=states_at_snapshot,
                event_by_seq=event_by_seq,
            )
            snapshot_results.append(
                {
                    "definition_match": True,
                    "first_difference": mismatch,
                    "name": path.name,
                    "ok": mismatch is None,
                    "watermark_seq": snapshot["watermark_seq"],
                }
            )
    snapshot_ok = all(item["ok"] for item in snapshot_results)
    watermark_match = watermark == logs.end_seq
    return {
        "actual_watermark_seq": watermark,
        "expected_watermark_seq": logs.end_seq,
        "first_difference": first,
        "ok": first is None and snapshot_ok and watermark_match,
        "snapshot_results": snapshot_results,
        "watermark_match": watermark_match,
    }

