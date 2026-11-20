from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .dependency import operation_summaries
from .diagnostics import analyze_templates
from .errors import WorkbenchError, error
from .migration_engine import (
    apply_operation_atomically,
    collect_all_conversion_logs,
    finalize_stage_schema,
    initialize_operation_log,
    read_operation_log,
)
from .model import (
    Bundle,
    apply_operation,
    bundle_digest,
    load_bundle,
    operation_digest,
    operation_target,
    schema_for_digest,
    schema_object_map,
    schema_digest,
    validate_name,
)
from .snapshot import (
    calculate_snapshot_digests,
    connect_snapshot,
    create_initial_snapshot,
    data_digest,
    verify_snapshot,
)
from .storage import (
    WorkspacePaths,
    bundle_to_dict,
    current_pointer,
    diagnostics_row,
    has_bundle,
    initialize_catalog,
    journal_row,
    next_sequence,
    open_catalog,
    read_bundle,
    replace_pointer,
    store_bundle,
    write_version_objects,
)
from .util import (
    FileLock,
    canonical_json,
    digest_json,
    fsync_dir,
    fsync_path,
    pretty_json,
    sha256_text,
)
from .validate import validate_bundle, validate_migration_plan


REPORT_FORMAT = "schema-diagnostics-report/v1"
MAX_SAFE_STEPS = 2_000_000
HARD_KILL_EXIT = 91


def _paths(workspace: Path) -> WorkspacePaths:
    return WorkspacePaths(workspace.expanduser().resolve())


def _lock(paths: WorkspacePaths, timeout: float = 30.0) -> FileLock:
    return FileLock(paths.lock_file, timeout=timeout)


@contextmanager
def _writer_lock(paths: WorkspacePaths, timeout: float = 30.0):
    try:
        with _lock(paths, timeout=timeout):
            yield
    except TimeoutError:
        raise error("E_LOCK_TIMEOUT", "timed out waiting for the workspace writer lock")


def _object_counts(schema: Dict[str, Any]) -> Dict[str, int]:
    columns = sum(len(table.get("columns", [])) for table in schema.get("tables", []))
    return {
        "tables": len(schema.get("tables", [])),
        "columns": columns,
        "indexes": len(schema.get("indexes", [])),
        "views": len(schema.get("views", [])),
        "triggers": len(schema.get("triggers", [])),
    }


def _object_diff(source: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
    left = schema_object_map(source)
    right = schema_object_map(target)
    added = [node for node in sorted(right) if node not in left]
    removed = [node for node in sorted(left) if node not in right]
    changed = [
        node
        for node in sorted(set(left) & set(right))
        if left[node]["digest"] != right[node]["digest"]
    ]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": len(set(left) & set(right)) - len(changed),
    }


def _migration_digest(migration: Dict[str, Any]) -> str:
    return digest_json(migration)


def _migration_record(conn: sqlite3.Connection, migration_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM migrations WHERE migration_id=?", (migration_id,)
    ).fetchone()
    return None if row is None else dict(row)


def _stored_report(conn: sqlite3.Connection, migration_id: str) -> Optional[Dict[str, Any]]:
    row = _migration_record(conn, migration_id)
    if row is None:
        return None
    return json.loads(row["report_json"])


def _idempotent_output(
    command: str,
    report: Dict[str, Any],
    pointer: Dict[str, Any],
    stale: bool = False,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "command": command,
        "migration_id": report["migration_id"],
        "change_kind": "IDEMPOTENT",
        "idempotent": True,
        "stale": stale,
        "current_version": pointer["version"],
        "content_digest": pointer["content_digest"],
        "report": report,
    }


def load_workspace(
    workspace: Path,
    schemas_path: Path,
    migrations_path: Path,
    queries_path: Path,
    tolerance: Optional[float] = None,
) -> Dict[str, Any]:
    bundle = load_bundle(
        schemas_path,
        migrations_path,
        queries_path,
        default_tolerance=0.000001 if tolerance is None else tolerance,
    )
    if tolerance is not None:
        bundle.default_tolerance = float(tolerance)
    validate_bundle(bundle)
    incoming_digest = bundle_digest(bundle)
    paths = _paths(workspace)
    with _writer_lock(paths):
        if has_bundle(paths):
            conn = open_catalog(paths, read_only=True)
            try:
                existing_digest = conn.execute(
                    "SELECT value FROM catalog_meta WHERE key='bundle_digest'"
                ).fetchone()
                pointer = current_pointer(conn)
            finally:
                conn.close()
            if existing_digest is None or existing_digest[0] != incoming_digest:
                raise error(
                    "E_ALREADY_LOADED",
                    "workspace already contains a different sample bundle",
                    current_bundle_digest=None if existing_digest is None else existing_digest[0],
                    incoming_bundle_digest=incoming_digest,
                )
            if pointer is None:
                raise error("E_RECOVERY_STATE", "workspace has a bundle but no current pointer")
            return {
                "ok": True,
                "command": "load",
                "change_kind": "IDEMPOTENT",
                "idempotent": True,
                "current_version": pointer["version"],
                "schema_digest": pointer["schema_digest"],
                "data_digest": pointer["data_digest"],
                "content_digest": pointer["content_digest"],
                "bundle_digest": incoming_digest,
                "object_counts": _object_counts(bundle.schemas[pointer["version"]]),
            }

        initialize_catalog(paths)
        first_version = bundle.first_version
        schema = bundle.schemas[first_version]
        stage_path = paths.snapshots / f".load-v{first_version}.sqlite"
        try:
            stage_path.unlink()
        except FileNotFoundError:
            pass
        create_initial_snapshot(schema, stage_path)
        digests = calculate_snapshot_digests(stage_path, schema)
        final_relative = paths.snapshot_relative(first_version, digests["content_digest"])
        final_path = paths.resolve(final_relative)
        if final_path.exists():
            existing = calculate_snapshot_digests(final_path, schema)
            if existing["content_digest"] != digests["content_digest"]:
                raise error(
                    "E_HASH_MISMATCH",
                    "existing initial snapshot has a different content digest",
                    expected=digests["content_digest"],
                    actual=existing["content_digest"],
                )
            stage_path.unlink()
        else:
            os.replace(stage_path, final_path)
            fsync_path(final_path)
            fsync_dir(final_path.parent)

        conn = open_catalog(paths)
        try:
            conn.execute("BEGIN IMMEDIATE")
            pointer = current_pointer(conn)
            if pointer is not None:
                raise error("E_RECOVERY_STATE", "catalog pointer appeared during initial load")
            store_bundle(conn, bundle, incoming_digest)
            sequence = next_sequence(conn)
            conn.execute(
                """
                INSERT INTO versions(
                  version, snapshot_path, schema_digest, data_digest, content_digest,
                  schema_json, migration_id, change_kind, created_seq
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'INITIAL', ?)
                """,
                (
                    first_version,
                    final_relative,
                    digests["schema_digest"],
                    digests["data_digest"],
                    digests["content_digest"],
                    canonical_json(schema),
                    sequence,
                ),
            )
            write_version_objects(conn, first_version, schema)
            replace_pointer(
                conn,
                {
                    "version": first_version,
                    "snapshot_path": final_relative,
                    "schema_digest": digests["schema_digest"],
                    "data_digest": digests["data_digest"],
                    "content_digest": digests["content_digest"],
                    "migration_id": None,
                    "change_kind": "INITIAL",
                    "previous_version": None,
                    "updated_seq": sequence,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "ok": True,
            "command": "load",
            "change_kind": "INITIAL",
            "idempotent": False,
            "current_version": first_version,
            "schema_digest": digests["schema_digest"],
            "data_digest": digests["data_digest"],
            "content_digest": digests["content_digest"],
            "bundle_digest": incoming_digest,
            "object_counts": _object_counts(schema),
        }


def _classify_transition(
    pointer: Dict[str, Any],
    migration: Dict[str, Any],
    migration_record: Optional[Dict[str, Any]],
) -> str:
    target = int(migration["to_version"])
    source = int(migration["from_version"])
    current = int(pointer["version"])
    if migration_record is not None:
        return "IDEMPOTENT"
    if target < current:
        return "VERSION_ROLLBACK"
    if target == current:
        return "VERSION_CONFLICT"
    if source != current:
        return "MIGRATION_ORDER"
    if migration.get("mode") == "FULL_REPLACE":
        return "FULL_REPLACE"
    return "INCREMENTAL_APPEND"


def _update_journal_progress(
    conn: sqlite3.Connection,
    migration_id: str,
    stage_path: Path,
    status: str = "RUNNING",
    error_payload: Optional[Dict[str, Any]] = None,
) -> None:
    logs = read_operation_log(stage_path)
    applied = [item["op_id"] for item in logs]
    conversions = collect_all_conversion_logs(stage_path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE journal
            SET applied_ops_json=?, conversion_log_json=?, status=?, error_json=?
            WHERE migration_id=?
            """,
            (
                canonical_json(applied),
                canonical_json(conversions),
                status,
                None if error_payload is None else canonical_json(error_payload),
                migration_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _set_stage_user_version(stage_path: Path, version: int) -> None:
    conn = connect_snapshot(stage_path, read_only=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"PRAGMA user_version={int(version)}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _build_report(
    migration: Dict[str, Any],
    change_kind: str,
    base_schema: Dict[str, Any],
    target_schema: Dict[str, Any],
    base_snapshot: Path,
    stage_snapshot: Path,
    conversion_log: Sequence[Dict[str, Any]],
    bundle: Bundle,
    digests: Dict[str, str],
    base_content_digest: str,
) -> Dict[str, Any]:
    tolerance = float(bundle.default_tolerance)
    analysis = analyze_templates(
        migration,
        base_schema,
        target_schema,
        base_snapshot,
        stage_snapshot,
        bundle.templates,
        tolerance,
        conversion_log,
    )
    target_verification = verify_snapshot(target_schema, stage_snapshot)
    if not target_verification["ok"]:
        raise error(
            "E_TARGET_SCHEMA_DRIFT",
            "migration stage does not match the declared target schema",
            issues=target_verification["issues"],
        )
    for name, value in target_verification.get("digests", {}).items():
        if digests.get(name) != value:
            raise error(
                "E_HASH_MISMATCH",
                "snapshot digest changed between report generation and verification",
                name=name,
                expected=digests.get(name),
                actual=value,
            )
    summaries = operation_summaries(migration)
    operations = [
        {
            **summary,
            "digest": operation_digest(operation),
        }
        for summary, operation in zip(summaries, migration["operations"])
    ]
    operation_summary_digest = digest_json(
        [{"op": item["op"], "target": item["target"], "digest": item["digest"]} for item in operations]
    )
    invalidation = analysis["invalidations"]
    counts = {
        "invalidated_indexes": len(invalidation["indexes"]),
        "invalidated_views": len(invalidation["views"]),
        "invalidated_triggers": len(invalidation["triggers"]),
        "invalidated_templates": len(invalidation["templates"]),
        "reusable_indexes": len(invalidation["safe_reuse"]["indexes"]),
        "reusable_views": len(invalidation["safe_reuse"]["views"]),
        "reusable_triggers": len(invalidation["safe_reuse"]["triggers"]),
        "reusable_templates": len(invalidation["safe_reuse"]["templates"]),
    }
    status_counts: Dict[str, int] = {}
    for template in analysis["templates"]:
        status_counts[template["status"]] = status_counts.get(template["status"], 0) + 1
    counts["templates_by_status"] = status_counts
    unaffected_identical = all(
        template["result_check"]["equal"]
        for template in analysis["templates"]
        if template["status"] == "REUSABLE"
    )
    body = {
        "report_format": REPORT_FORMAT,
        "migration_id": migration["id"],
        "from_version": int(migration["from_version"]),
        "to_version": int(migration["to_version"]),
        "change_kind": change_kind,
        "migration_digest": _migration_digest(migration),
        "operation_summary_digest": operation_summary_digest,
        "operations": operations,
        "version_transition": {
            "from_version": int(migration["from_version"]),
            "to_version": int(migration["to_version"]),
            "change_kind": change_kind,
            "base_content_digest": base_content_digest,
            "object_diff": _object_diff(base_schema, target_schema),
        },
        "digests": {
            "schema_digest": digests["schema_digest"],
            "data_digest": digests["data_digest"],
            "content_digest": digests["content_digest"],
        },
        "tolerance": tolerance,
        "conversion_log": list(conversion_log),
        "invalidations": invalidation,
        "safe_reuse": invalidation["safe_reuse"],
        "templates": analysis["templates"],
        "precision_sensitivity_pairs": analysis["precision_sensitivity_pairs"],
        "counts": counts,
        "verification": {
            "target_schema_ok": True,
            "foreign_keys_ok": True,
            "unaffected_templates_identical": unaffected_identical,
            "query_result_checks": len(analysis["templates"]),
        },
    }
    body["run_id"] = digest_json(body)
    return body


def _ensure_final_file(
    paths: WorkspacePaths,
    journal: Dict[str, Any],
    target_schema: Dict[str, Any],
) -> Path:
    final_path = paths.resolve(journal["final_path"])
    stage_path = paths.resolve(journal["stage_path"])
    expected = journal["content_digest"]
    if final_path.exists():
        existing = calculate_snapshot_digests(final_path, target_schema)
        if existing["content_digest"] != expected:
            raise error(
                "E_HASH_MISMATCH",
                "final snapshot exists with a different digest",
                expected=expected,
                actual=existing["content_digest"],
            )
        if stage_path.exists():
            stage_path.unlink()
        return final_path
    if not stage_path.exists():
        raise error(
            "E_RECOVERY_STATE",
            "neither the ready final snapshot nor the migration stage exists",
            migration=journal["migration_id"],
        )
    finalize_stage_schema(stage_path, target_schema)
    verification = verify_snapshot(target_schema, stage_path)
    if not verification["ok"]:
        raise error(
            "E_TARGET_SCHEMA_DRIFT",
            "migration stage failed final verification",
            issues=verification["issues"],
        )
    existing = calculate_snapshot_digests(stage_path, target_schema)
    if existing["content_digest"] != expected:
        raise error(
            "E_HASH_MISMATCH",
            "finalized stage content digest differs from the ready journal",
            expected=expected,
            actual=existing["content_digest"],
        )
    os.replace(stage_path, final_path)
    fsync_path(final_path)
    fsync_dir(final_path.parent)
    return final_path


def _cleanup_journal(conn: sqlite3.Connection, migration_id: str, stage_path: Optional[Path]) -> None:
    if stage_path is not None and stage_path.exists():
        stage_path.unlink()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM journal WHERE migration_id=?", (migration_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _commit_pointer(
    paths: WorkspacePaths,
    conn: sqlite3.Connection,
    journal: Dict[str, Any],
    bundle: Bundle,
    report: Dict[str, Any],
) -> Dict[str, Any]:
    migration_id = journal["migration_id"]
    target_version = int(journal["to_version"])
    final_path = paths.resolve(journal["final_path"])
    target_schema = bundle.schemas[target_version]
    expected = calculate_snapshot_digests(final_path, target_schema)
    if expected["content_digest"] != journal["content_digest"]:
        raise error(
            "E_HASH_MISMATCH",
            "snapshot digest does not match the ready journal",
            expected=journal["content_digest"],
            actual=expected["content_digest"],
        )
    pointer = current_pointer(conn)
    if pointer is None:
        raise error("E_RECOVERY_STATE", "catalog pointer disappeared during migration")
    if int(pointer["version"]) == target_version:
        if pointer["content_digest"] != expected["content_digest"]:
            raise error(
                "E_VERSION_CONFLICT",
                "target version is already committed with different content",
                version=target_version,
            )
        stored = _stored_report(conn, migration_id)
        if stored is None:
            raise error("E_RECOVERY_STATE", "pointer advanced but migration report is missing")
        _cleanup_journal(conn, migration_id, paths.resolve(journal["stage_path"]))
        return {
            "report": stored,
            "already_committed": True,
        }
    if int(pointer["version"]) > target_version:
        raise error(
            "E_VERSION_ROLLBACK",
            "version pointer has advanced beyond this migration",
            current_version=pointer["version"],
            target_version=target_version,
        )
    if (
        int(pointer["version"]) != int(journal["base_version"])
        or pointer["content_digest"] != journal["base_content_digest"]
    ):
        raise error(
            "E_CONCURRENT_MODIFICATION",
            "current pointer changed while the migration stage was being prepared",
            expected_version=journal["base_version"],
            current_version=pointer["version"],
        )
    migration = bundle.migrations[migration_id]
    conn.execute("BEGIN IMMEDIATE")
    try:
        pointer = current_pointer(conn)
        if pointer is None:
            raise error("E_RECOVERY_STATE", "catalog pointer disappeared before commit")
        if int(pointer["version"]) == target_version:
            conn.rollback()
            stored = _stored_report(conn, migration_id)
            if stored is None:
                raise error("E_RECOVERY_STATE", "concurrent commit lacks a report")
            _cleanup_journal(conn, migration_id, paths.resolve(journal["stage_path"]))
            return {"report": stored, "already_committed": True}
        if (
            int(pointer["version"]) != int(journal["base_version"])
            or pointer["content_digest"] != journal["base_content_digest"]
        ):
            raise error(
                "E_CONCURRENT_MODIFICATION",
                "current pointer changed before the atomic pointer replacement",
                expected_version=journal["base_version"],
                current_version=pointer["version"],
            )
        sequence = next_sequence(conn)
        conn.execute(
            """
            INSERT INTO versions(
              version, snapshot_path, schema_digest, data_digest, content_digest,
              schema_json, migration_id, change_kind, created_seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_version,
                journal["final_path"],
                expected["schema_digest"],
                expected["data_digest"],
                expected["content_digest"],
                canonical_json(target_schema),
                migration_id,
                journal["report_json"]
                and json.loads(journal["report_json"])["change_kind"]
                or "INCREMENTAL_APPEND",
                sequence,
            ),
        )
        write_version_objects(conn, target_version, target_schema)
        change_kind = json.loads(journal["report_json"])["change_kind"]
        conn.execute(
            """
            INSERT INTO migrations(
              migration_id, migration_digest, from_version, to_version,
              content_digest, status, report_json, change_kind, created_seq
            ) VALUES (?, ?, ?, ?, ?, 'COMMITTED', ?, ?, ?)
            """,
            (
                migration_id,
                journal["migration_digest"],
                int(migration["from_version"]),
                target_version,
                expected["content_digest"],
                journal["report_json"],
                change_kind,
                sequence,
            ),
        )
        conn.execute(
            """
            INSERT INTO diagnostics(
              run_id, migration_id, from_version, to_version, report_json, created_seq
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                report["run_id"],
                migration_id,
                int(migration["from_version"]),
                target_version,
                journal["report_json"],
                sequence,
            ),
        )
        replace_pointer(
            conn,
            {
                "version": target_version,
                "snapshot_path": journal["final_path"],
                "schema_digest": expected["schema_digest"],
                "data_digest": expected["data_digest"],
                "content_digest": expected["content_digest"],
                "migration_id": migration_id,
                "change_kind": change_kind,
                "previous_version": int(pointer["version"]),
                "updated_seq": sequence,
            },
        )
        conn.execute("DELETE FROM journal WHERE migration_id=?", (migration_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"report": report, "already_committed": False}


def _continue_journal_locked(
    paths: WorkspacePaths,
    conn: sqlite3.Connection,
    bundle: Bundle,
    journal: Dict[str, Any],
    failpoint: Optional[str] = None,
) -> Dict[str, Any]:
    migration_id = journal["migration_id"]
    if migration_id not in bundle.migrations:
        raise error(
            "E_RECOVERY_STATE",
            "journal references a migration that is absent from the stored bundle",
            migration=migration_id,
        )
    migration = bundle.migrations[migration_id]
    target_schema = json.loads(journal["target_schema_json"])
    stage_path = paths.resolve(journal["stage_path"])
    pointer = current_pointer(conn)
    if pointer is None:
        raise error("E_RECOVERY_STATE", "catalog has no current pointer")
    if int(pointer["version"]) == int(journal["to_version"]):
        stored = _stored_report(conn, migration_id)
        if stored is None:
            raise error("E_RECOVERY_STATE", "pointer is at target but report is absent")
        _cleanup_journal(conn, migration_id, stage_path)
        return {
            "report": stored,
            "already_committed": True,
            "reconciled_operations": [],
        }
    if int(pointer["version"]) > int(journal["to_version"]):
        raise error(
            "E_VERSION_ROLLBACK",
            "current version is newer than the pending migration",
            current_version=pointer["version"],
            migration_id=migration_id,
        )
    if (
        int(pointer["version"]) != int(journal["base_version"])
        or pointer["content_digest"] != journal["base_content_digest"]
    ):
        raise error(
            "E_CONCURRENT_MODIFICATION",
            "current pointer does not match the pending migration base",
            expected_version=journal["base_version"],
            current_version=pointer["version"],
        )

    if journal["status"] == "READY":
        if not journal.get("report_json") or not journal.get("final_path"):
            raise error("E_RECOVERY_STATE", "READY journal is missing its report or final path")
        report = json.loads(journal["report_json"])
        _ensure_final_file(paths, journal, target_schema)
        committed = _commit_pointer(paths, conn, journal, bundle, report)
        return {
            "report": committed["report"],
            "already_committed": committed["already_committed"],
            "reconciled_operations": [],
        }

    if not stage_path.exists():
        raise error(
            "E_RECOVERY_STATE",
            "pending migration stage is missing",
            migration=migration_id,
            stage_path=journal["stage_path"],
        )

    base_schema = schema_for_digest(bundle.schemas[int(migration["from_version"])])
    logical = copy.deepcopy(base_schema)
    logs = read_operation_log(stage_path)
    logged_indices = [int(item["op_index"]) for item in logs]
    if logged_indices != list(range(len(logged_indices))):
        raise error(
            "E_RECOVERY_STATE",
            "migration operation log is not a contiguous prefix",
            logged_indices=logged_indices,
        )
    planned_ids = [item["op_id"] for item in operation_summaries(migration)]
    for item in logs:
        expected_id = planned_ids[item["op_index"]]
        if item["op_id"] != expected_id:
            raise error(
                "E_RECOVERY_STATE",
                "migration operation log does not match the planned operation sequence",
                index=item["op_index"],
                expected=expected_id,
                actual=item["op_id"],
            )
    logged = {int(item["op_index"]): item for item in logs}
    reconciled: List[str] = []
    for op_index, operation in enumerate(migration["operations"]):
        op_id = planned_ids[op_index]
        after_schema = apply_operation(
            logical,
            operation,
            f"migration:{migration_id}.operations[{op_index}]",
        )
        if op_index not in logged:
            try:
                apply_operation_atomically(
                    stage_path,
                    operation,
                    logical,
                    after_schema,
                    op_id,
                    op_index,
                )
            except Exception as exc:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "UPDATE journal SET status='FAILED', error_json=? WHERE migration_id=?",
                        (
                            canonical_json(
                                {
                                    "code": getattr(exc, "code", "E_MIGRATION_EXECUTION"),
                                    "message": str(exc),
                                }
                            ),
                            migration_id,
                        ),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                if isinstance(exc, WorkbenchError):
                    raise
                raise error(
                    "E_MIGRATION_EXECUTION",
                    "migration operation failed and its transaction was rolled back",
                    operation_id=op_id,
                    operation=operation["op"],
                    detail=str(exc),
                )
            reconciled.append(op_id)
            if failpoint and failpoint == operation["op"]:
                os._exit(HARD_KILL_EXIT)
        logical = after_schema
        _update_journal_progress(conn, migration_id, stage_path)

    logical["version"] = int(migration["to_version"])
    if canonical_json(schema_for_digest(logical)) != canonical_json(schema_for_digest(target_schema)):
        raise error(
            "E_MIGRATION_TARGET_MISMATCH",
            "recovered operation sequence does not produce the declared target schema",
            migration=migration_id,
        )

    conversion_log = collect_all_conversion_logs(stage_path)
    _set_stage_user_version(stage_path, int(migration["to_version"]))
    verification = verify_snapshot(target_schema, stage_path)
    if not verification["ok"]:
        raise error(
            "E_TARGET_SCHEMA_DRIFT",
            "prepared stage does not match the target schema",
            issues=verification["issues"],
        )
    digests = calculate_snapshot_digests(stage_path, target_schema)
    change_kind = "INCREMENTAL_APPEND"
    if migration.get("mode") == "FULL_REPLACE":
        change_kind = "FULL_REPLACE"
    report = _build_report(
        migration,
        change_kind,
        base_schema,
        target_schema,
        paths.resolve(pointer["snapshot_path"]),
        stage_path,
        conversion_log,
        bundle,
        digests,
        pointer["content_digest"],
    )
    final_relative = paths.snapshot_relative(int(migration["to_version"]), digests["content_digest"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE journal
            SET status='READY', final_path=?, schema_digest=?, data_digest=?,
                content_digest=?, report_json=?, conversion_log_json=?,
                error_json=NULL
            WHERE migration_id=?
            """,
            (
                final_relative,
                digests["schema_digest"],
                digests["data_digest"],
                digests["content_digest"],
                canonical_json(report),
                canonical_json(conversion_log),
                migration_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    journal = journal_row(conn)
    if journal is None:
        raise error("E_RECOVERY_STATE", "journal disappeared after READY update")
    _ensure_final_file(paths, journal, target_schema)
    committed = _commit_pointer(paths, conn, journal, bundle, report)
    return {
        "report": committed["report"],
        "already_committed": committed["already_committed"],
        "reconciled_operations": reconciled,
    }


def run_migration(
    workspace: Path,
    migration_id: str,
    failpoint: Optional[str] = None,
) -> Dict[str, Any]:
    paths = _paths(workspace)
    validate_name(migration_id, "migration", "migration")
    with _writer_lock(paths):
        conn = open_catalog(paths)
        try:
            pointer = current_pointer(conn)
            if pointer is None:
                raise error("E_NOT_LOADED", "workspace has no current schema pointer")
            bundle = read_bundle(paths, conn)
            if migration_id not in bundle.migrations:
                raise error("E_MIGRATION_NOT_FOUND", "migration id is not declared", migration=migration_id)
            migration = bundle.migrations[migration_id]
            migration_hash = _migration_digest(migration)
            record = _migration_record(conn, migration_id)
            if record is not None and record["migration_digest"] != migration_hash:
                raise error(
                    "E_MIGRATION_CONFLICT",
                    "migration id was already committed with a different digest",
                    migration=migration_id,
                )
            pending = journal_row(conn)
            if pending is not None:
                if pending["migration_id"] != migration_id:
                    raise error(
                        "E_JOURNAL_PENDING",
                        "another migration has an unfinished recovery journal",
                        pending_migration=pending["migration_id"],
                        status=pending["status"],
                    )
                outcome = _continue_journal_locked(paths, conn, bundle, pending, None)
                return {
                    "ok": True,
                    "command": "migrate",
                    "migration_id": migration_id,
                    "change_kind": "RECOVERED",
                    "idempotent": False,
                    "recovered": True,
                    "current_version": current_pointer(conn)["version"],
                    "report": outcome["report"],
                }
            if record is not None:
                stored = _stored_report(conn, migration_id)
                if stored is None:
                    raise error("E_RECOVERY_STATE", "committed migration has no stored report")
                return _idempotent_output(
                    "migrate",
                    stored,
                    pointer,
                    stale=int(pointer["version"]) > int(migration["to_version"]),
                )
            change_kind = _classify_transition(pointer, migration, record)
            if change_kind == "VERSION_ROLLBACK":
                raise error(
                    "E_VERSION_ROLLBACK",
                    "migration target version is older than the current version",
                    current_version=pointer["version"],
                    target_version=migration["to_version"],
                )
            if change_kind == "VERSION_CONFLICT":
                raise error(
                    "E_VERSION_CONFLICT",
                    "target version is already current without this migration",
                    version=migration["to_version"],
                )
            if change_kind == "MIGRATION_ORDER":
                raise error(
                    "E_MIGRATION_ORDER",
                    "migration source does not match the current version",
                    current_version=pointer["version"],
                    from_version=migration["from_version"],
                )
            validate_migration_plan(migration, bundle.schemas)
            base_snapshot = paths.resolve(pointer["snapshot_path"])
            if not base_snapshot.exists():
                raise error("E_SNAPSHOT_MISSING", "base snapshot is missing", snapshot=pointer["snapshot_path"])
            stage_relative = paths.stage_relative(migration_id, migration_hash)
            stage_path = paths.resolve(stage_relative)
            if stage_path.exists():
                stage_path.unlink()
            copy_temp = stage_path.with_name(stage_path.name + ".copying")
            if copy_temp.exists():
                copy_temp.unlink()
            shutil.copy2(base_snapshot, copy_temp)
            os.replace(copy_temp, stage_path)
            initialize_operation_log(stage_path, int(pointer["version"]))
            planned = [
                {**summary, "digest": operation_digest(operation)}
                for summary, operation in zip(operation_summaries(migration), migration["operations"])
            ]
            conn.execute("BEGIN IMMEDIATE")
            try:
                sequence = next_sequence(conn)
                conn.execute(
                    """
                    INSERT INTO journal(
                      migration_id, migration_digest, from_version, to_version,
                      base_version, base_content_digest, stage_path, final_path,
                      planned_ops_json, applied_ops_json, conversion_log_json,
                      target_schema_json, schema_digest, data_digest, content_digest,
                      report_json, status, error_json, created_seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, '[]', '[]', ?, NULL, NULL, NULL,
                              NULL, 'RUNNING', NULL, ?)
                    """,
                    (
                        migration_id,
                        migration_hash,
                        int(migration["from_version"]),
                        int(migration["to_version"]),
                        int(pointer["version"]),
                        pointer["content_digest"],
                        stage_relative,
                        canonical_json(planned),
                        canonical_json(bundle.schemas[int(migration["to_version"])]),
                        sequence,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            journal = journal_row(conn)
            if journal is None:
                raise error("E_RECOVERY_STATE", "new journal row is missing")
            outcome = _continue_journal_locked(paths, conn, bundle, journal, failpoint)
            pointer = current_pointer(conn)
            return {
                "ok": True,
                "command": "migrate",
                "migration_id": migration_id,
                "change_kind": change_kind,
                "idempotent": False,
                "recovered": False,
                "current_version": pointer["version"],
                "content_digest": pointer["content_digest"],
                "report": outcome["report"],
            }
        finally:
            conn.close()


def run_migration_all(
    workspace: Path,
    target_version: Optional[int] = None,
    failpoint: Optional[str] = None,
) -> Dict[str, Any]:
    paths = _paths(workspace)
    conn = open_catalog(paths, read_only=True)
    try:
        pointer = current_pointer(conn)
        bundle = read_bundle(paths, conn)
    finally:
        conn.close()
    if pointer is None:
        raise error("E_NOT_LOADED", "workspace has no current schema pointer")
    current = int(pointer["version"])
    final_target = bundle.last_version if target_version is None else int(target_version)
    if final_target < current:
        raise error(
            "E_VERSION_ROLLBACK",
            "requested target is older than the current version",
            current_version=current,
            target_version=final_target,
        )
    if final_target not in bundle.schemas:
        raise error("E_VERSION_NOT_FOUND", "requested target version is unknown", version=final_target)
    results = []
    while current < final_target:
        migration = bundle.migration_for_target(current + 1)
        if migration is None:
            raise error("E_MIGRATION_GAP", "no migration for the next version", version=current + 1)
        result = run_migration(workspace, migration["id"], failpoint=failpoint)
        results.append(result)
        current = int(result["current_version"])
        if failpoint:
            failpoint = None
    return {
        "ok": True,
        "command": "migrate",
        "change_kind": "BATCH",
        "idempotent": all(item.get("idempotent", False) for item in results),
        "current_version": current,
        "results": results,
    }


def resume_workspace(workspace: Path, failpoint: Optional[str] = None) -> Dict[str, Any]:
    paths = _paths(workspace)
    with _writer_lock(paths):
        conn = open_catalog(paths)
        try:
            pointer = current_pointer(conn)
            if pointer is None:
                raise error("E_NOT_LOADED", "workspace has no current schema pointer")
            pending = journal_row(conn)
            if pending is None:
                return {
                    "ok": True,
                    "command": "resume",
                    "change_kind": "NOOP",
                    "resumed": False,
                    "current_version": pointer["version"],
                    "content_digest": pointer["content_digest"],
                }
            bundle = read_bundle(paths, conn)
            outcome = _continue_journal_locked(paths, conn, bundle, pending, failpoint)
            pointer = current_pointer(conn)
            return {
                "ok": True,
                "command": "resume",
                "change_kind": "RECOVERED",
                "resumed": True,
                "reconciled_operations": outcome["reconciled_operations"],
                "current_version": pointer["version"],
                "content_digest": pointer["content_digest"],
                "report": outcome["report"],
            }
        finally:
            conn.close()


def status_workspace(workspace: Path) -> Dict[str, Any]:
    paths = _paths(workspace)
    conn = open_catalog(paths, read_only=True)
    try:
        conn.execute("BEGIN")
        pointer = current_pointer(conn)
        if pointer is None:
            raise error("E_NOT_LOADED", "workspace has no current schema pointer")
        version_row = conn.execute(
            "SELECT * FROM versions WHERE version=?", (pointer["version"],)
        ).fetchone()
        if version_row is None:
            raise error("E_RECOVERY_STATE", "current pointer has no version metadata")
        schema = json.loads(version_row["schema_json"])
        current_version = int(pointer["version"])
        object_rows = conn.execute(
            "SELECT version, object_type, object_name, object_digest "
            "FROM version_objects WHERE version<=? ORDER BY version, object_type, object_name",
            (current_version,),
        ).fetchall()
        histories: Dict[Tuple[str, str], List[Tuple[int, str]]] = {}
        for row in object_rows:
            histories.setdefault((row["object_type"], row["object_name"]), []).append(
                (int(row["version"]), row["object_digest"])
            )
        object_versions = []
        for node, item in sorted(schema_object_map(schema).items()):
            history = histories.get((item["type"], item["name"]), [])
            if not history:
                continue
            latest_digest = history[-1][1]
            since_version = history[-1][0]
            last_changed_version = history[0][0]
            previous_digest = None
            for version, digest in history:
                if digest != previous_digest:
                    last_changed_version = version
                previous_digest = digest
            for version, digest in reversed(history):
                if digest == latest_digest:
                    since_version = version
                else:
                    break
            object_versions.append(
                {
                    "node": node,
                    "type": item["type"],
                    "name": item["name"],
                    "digest": latest_digest,
                    "since_version": since_version,
                    "last_changed_version": last_changed_version,
                }
            )
        latest_diag = conn.execute(
            "SELECT * FROM diagnostics WHERE to_version=? ORDER BY created_seq DESC LIMIT 1",
            (current_version,),
        ).fetchone()
        invalidations = None
        if latest_diag is not None:
            report = json.loads(latest_diag["report_json"])
            invalidations = report.get("counts", {})
        pending = journal_row(conn)
        counts = _object_counts(schema)
        return {
            "ok": True,
            "command": "status",
            "current_version": current_version,
            "schema_digest": pointer["schema_digest"],
            "data_digest": pointer["data_digest"],
            "content_digest": pointer["content_digest"],
            "snapshot": Path(pointer["snapshot_path"]).name,
            "migration_id": pointer["migration_id"],
            "change_kind": pointer["change_kind"],
            "object_counts": counts,
            "object_versions": object_versions,
            "invalidations": invalidations,
            "pending_journal": None
            if pending is None
            else {
                "migration_id": pending["migration_id"],
                "status": pending["status"],
                "applied_operations": json.loads(pending["applied_ops_json"]),
            },
        }
    finally:
        conn.close()


def verify_workspace(workspace: Path) -> Dict[str, Any]:
    paths = _paths(workspace)
    conn = open_catalog(paths, read_only=True)
    checks: List[Dict[str, Any]] = []
    try:
        conn.execute("BEGIN")
        pointer = current_pointer(conn)
        if pointer is None:
            raise error("E_NOT_LOADED", "workspace has no current schema pointer")
        bundle = read_bundle(paths, conn)
        bundle_json = conn.execute(
            "SELECT value FROM catalog_meta WHERE key='bundle_json'"
        ).fetchone()
        bundle_digest_row = conn.execute(
            "SELECT value FROM catalog_meta WHERE key='bundle_digest'"
        ).fetchone()
        checks.append(
            {
                "name": "bundle_digest",
                "ok": bundle_digest_row is not None
                and bundle_digest_row[0] == bundle_digest(bundle),
                "expected": None if bundle_digest_row is None else bundle_digest_row[0],
                "actual": bundle_digest(bundle),
            }
        )
        version_row = conn.execute(
            "SELECT * FROM versions WHERE version=?", (pointer["version"],)
        ).fetchone()
        checks.append({"name": "version_row", "ok": version_row is not None, "version": pointer["version"]})
        if version_row is None:
            return {"ok": False, "command": "verify", "checks": checks}
        schema = json.loads(version_row["schema_json"])
        expected_schema = bundle.schemas[pointer["version"]]
        checks.append(
            {
                "name": "stored_schema_matches_bundle",
                "ok": canonical_json(schema_for_digest(schema))
                == canonical_json(schema_for_digest(expected_schema)),
            }
        )
        checks.append(
            {
                "name": "pointer_version_metadata",
                "ok": version_row["snapshot_path"] == pointer["snapshot_path"]
                and version_row["content_digest"] == pointer["content_digest"],
            }
        )
        snapshot = paths.resolve(pointer["snapshot_path"])
        snapshot_check = verify_snapshot(schema, snapshot)
        checks.append(
            {
                "name": "sqlite_schema",
                "ok": snapshot_check["ok"],
                "issues": snapshot_check["issues"],
            }
        )
        computed = snapshot_check.get("digests", {})
        for name in ("schema_digest", "data_digest", "content_digest"):
            checks.append(
                {
                    "name": name,
                    "ok": computed.get(name) == pointer[name],
                    "expected": pointer[name],
                    "actual": computed.get(name),
                }
            )
        expected_objects = schema_object_map(schema)
        stored_objects = {
            (row["object_type"], row["object_name"]): row["object_digest"]
            for row in conn.execute(
                "SELECT object_type, object_name, object_digest "
                "FROM version_objects WHERE version=?",
                (pointer["version"],),
            )
        }
        expected_object_rows = {
            (item["type"], item["name"]): item["digest"] for item in expected_objects.values()
        }
        checks.append(
            {
                "name": "object_versions",
                "ok": stored_objects == expected_object_rows,
                "expected_count": len(expected_object_rows),
                "actual_count": len(stored_objects),
            }
        )
        versions = conn.execute(
            "SELECT version, migration_id, change_kind FROM versions ORDER BY version"
        ).fetchall()
        expected_versions = list(range(bundle.first_version, int(pointer["version"]) + 1))
        checks.append(
            {
                "name": "version_chain",
                "ok": [int(row["version"]) for row in versions] == expected_versions,
                "versions": [int(row["version"]) for row in versions],
            }
        )
        migration_rows = conn.execute(
            "SELECT migration_id, from_version, to_version FROM migrations ORDER BY to_version"
        ).fetchall()
        chain_ok = True
        previous = bundle.first_version
        for row in migration_rows:
            if int(row["from_version"]) != previous or int(row["to_version"]) != previous + 1:
                chain_ok = False
                break
            previous = int(row["to_version"])
        checks.append(
            {
                "name": "migration_history",
                "ok": chain_ok and previous == int(pointer["version"]),
                "migrations": [row["migration_id"] for row in migration_rows],
            }
        )
        pending = journal_row(conn)
        checks.append(
            {
                "name": "no_pending_journal",
                "ok": pending is None,
                "warning": pending is not None,
                "pending_migration": None if pending is None else pending["migration_id"],
            }
        )
        ok = all(
            item["ok"]
            for item in checks
            if item["name"] != "no_pending_journal"
        )
        return {
            "ok": ok,
            "command": "verify",
            "current_version": pointer["version"],
            "content_digest": pointer["content_digest"],
            "checks": checks,
            "pending_journal": pending is not None,
        }
    finally:
        conn.close()


def explain_workspace(
    workspace: Path,
    template_id: str,
    migration_id: Optional[str] = None,
) -> Dict[str, Any]:
    paths = _paths(workspace)
    conn = open_catalog(paths, read_only=True)
    try:
        conn.execute("BEGIN")
        pointer = current_pointer(conn)
        if pointer is None:
            raise error("E_NOT_LOADED", "workspace has no current schema pointer")
        if migration_id is not None:
            row = diagnostics_row(conn, migration_id=migration_id)
        else:
            row = diagnostics_row(conn)
        if row is None:
            raise error("E_DIAGNOSTIC_NOT_FOUND", "no migration diagnostic report exists")
        report = json.loads(row["report_json"])
        template = next(
            (item for item in report.get("templates", []) if item["id"] == template_id),
            None,
        )
        if template is None:
            raise error(
                "E_TEMPLATE_NOT_FOUND",
                "template is not present in the selected migration diagnostic",
                template=template_id,
                migration_id=report["migration_id"],
            )
        related = []
        path_nodes = {
            node
            for reason in template.get("dependency_paths", [])
            for node in reason.get("path", [])
        }
        for category in ("indexes", "views", "triggers"):
            for item in report.get("invalidations", {}).get(category, []):
                if item["node"] in path_nodes:
                    related.append(
                        {
                            "category": category,
                            "name": item["name"],
                            "present_after": item["present_after"],
                            "reasons": item["reasons"],
                        }
                    )
        related.sort(key=lambda item: (item["category"], item["name"]))
        return {
            "ok": True,
            "command": "explain",
            "migration_id": report["migration_id"],
            "from_version": report["from_version"],
            "to_version": report["to_version"],
            "template_id": template_id,
            "status": template["status"],
            "invalidated": template["invalidated"],
            "dependency_paths": template["dependency_paths"],
            "invalidated_dependencies": related,
            "rewritten_sql": template["rewrite"]["sql"],
            "rewrite_changed": template["rewrite"]["changed"],
            "first_affected_column": template["rewrite"]["first_affected_column"],
            "result_check": template["result_check"],
            "precision_sensitive": template["precision_sensitive"],
            "error_source_chain": template["error_source_chain"],
            "current_version": pointer["version"],
        }
    finally:
        conn.close()

