"""Command services joining the in-memory engine and durable state."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from .canonical import canonical_json, sha256_json
from .engine import MaintenanceEngine, OperationOutcome
from .errors import (
    BusinessError,
    E_BAD_OP,
    E_CLOCK_ROLLBACK,
    E_DUP_PK,
    E_INDEX_DIGEST_MISMATCH,
    E_INCONSISTENT_STATE,
    E_OP_CONFLICT,
    E_RANGE_WAIT_TIMEOUT,
    E_SAMPLE_MISMATCH,
    E_SPLIT_HEIGHT_OVERFLOW,
    E_UNFINISHED_ATTEMPT,
    E_UNIQUE_VIOLATION,
)
from .sample import Manifest, Operation, load_manifest, operation_signature
from .storage import (
    check_ledger_consistency,
    clear_attempts_for_seq,
    connect,
    database_path,
    delete_attempt,
    file_lock,
    initialize,
    lock_path,
    open_initialized,
    read_attempts,
    read_invalidations,
    read_ledger_events,
    read_manifest,
    read_meta,
    read_meta_int,
    read_operations,
    read_rows,
    read_snapshots,
    read_split_events,
    replace_rows,
    snapshot_state_digest,
    store_invalidations,
    store_ledger_event,
    store_operation,
    store_snapshot,
    store_split_events,
    update_progress_meta,
    upsert_attempt,
)


REJECTABLE_CODES = {
    E_DUP_PK,
    E_UNIQUE_VIOLATION,
    E_BAD_OP,
    E_SPLIT_HEIGHT_OVERFLOW,
    E_CLOCK_ROLLBACK,
}


class WorkspaceService:
    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        lock_timeout: float = 30.0,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.db_path = database_path(self.workspace)
        self.lock_path = lock_path(self.workspace)
        self.lock_timeout = lock_timeout

    def _lock(self) -> Any:
        return file_lock(self.lock_path, timeout=self.lock_timeout)

    def _verify_external_sample(
        self,
        stored: Manifest,
        sample_path: str | None,
    ) -> None:
        if sample_path is None:
            return
        external = load_manifest(sample_path)
        if external.sample_sha256 != stored.sample_sha256:
            raise BusinessError(
                E_SAMPLE_MISMATCH,
                "provided sample does not match the loaded workspace sample",
                {
                    "stored_sha256": stored.sample_sha256,
                    "provided_sha256": external.sample_sha256,
                },
            )

    def load(
        self,
        sample_path: str,
        *,
        reset: bool = False,
    ) -> dict[str, Any]:
        manifest = load_manifest(sample_path)
        with self._lock():
            if self.db_path.exists() and not reset:
                connection: sqlite3.Connection | None = None
                try:
                    connection = open_initialized(self.db_path)
                    stored = read_manifest(connection)
                    if stored.sample_sha256 != manifest.sample_sha256:
                        raise BusinessError(
                            E_SAMPLE_MISMATCH,
                            "workspace already contains a different sample",
                            {
                                "stored_sha256": stored.sample_sha256,
                                "provided_sha256": manifest.sample_sha256,
                            },
                        )
                    meta = read_meta(connection)
                    check_ledger_consistency(connection)
                    return {
                        "command": "load",
                        "ok": True,
                        "created": False,
                        "sample_id": manifest.sample_id,
                        "sample_sha256": manifest.sample_sha256,
                        "ledger_version": int(meta["ledger_version"]),
                        "watermark_seq": int(meta["watermark_seq"]),
                        "indexes": sorted(["pk"] + [index.name for index in manifest.indexes]),
                    }
                except BusinessError as exc:
                    if exc.code != "E_NOT_INITIALIZED":
                        raise
                finally:
                    if connection is not None:
                        connection.close()
            if reset:
                for name in (
                    self.db_path.name,
                    self.db_path.name + "-wal",
                    self.db_path.name + "-shm",
                ):
                    target = self.db_path.parent / name
                    if target.exists():
                        target.unlink()
            engine = MaintenanceEngine.initial(manifest)
            initialize(self.db_path, manifest, engine.all_snapshots())
            return {
                "command": "load",
                "ok": True,
                "created": True,
                "sample_id": manifest.sample_id,
                "sample_sha256": manifest.sample_sha256,
                "ledger_version": 0,
                "watermark_seq": 0,
                "indexes": sorted(["pk"] + [index.name for index in manifest.indexes]),
            }

    def _operation_map(self, manifest: Manifest) -> dict[int, Operation]:
        return {operation.seq: operation for operation in manifest.operations}

    def _check_operation_ledger(
        self,
        manifest: Manifest,
        operations: dict[int, dict[str, Any]],
        *,
        from_seq: int | None,
        to_seq: int | None,
    ) -> None:
        for operation in manifest.operations:
            if from_seq is not None and operation.seq < from_seq:
                continue
            if to_seq is not None and operation.seq > to_seq:
                continue
            existing = operations.get(operation.seq)
            if existing is not None and existing["digest"] != operation.content_digest:
                raise BusinessError(
                    E_OP_CONFLICT,
                    "an operation sequence was already committed with different content",
                    {
                        "seq": operation.seq,
                        "stored_digest": existing["digest"],
                        "provided_digest": operation.content_digest,
                    },
                )

    def _reject_operation(
        self,
        connection: sqlite3.Connection,
        *,
        operation: Operation,
        base_ledger_version: int,
        error: BusinessError,
        attempt_id: str,
        watermark_seq: int,
    ) -> dict[str, Any]:
        new_version = base_ledger_version + 1
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = check_ledger_consistency(connection)
            if current != base_ledger_version:
                raise BusinessError(
                    E_OP_CONFLICT,
                    "ledger changed while the operation was being prepared",
                    {"base": base_ledger_version, "current": current},
                )
            store_operation(
                connection,
                seq=operation.seq,
                digest=operation.content_digest,
                status="rejected",
                clock_ms=operation.clock_ms,
                ledger_version=new_version,
                result={"seq": operation.seq, "status": "rejected"},
                error=error.to_dict(),
            )
            store_ledger_event(
                connection,
                ledger_version=new_version,
                kind="operation_rejected",
                seq=operation.seq,
                detail={"error_code": error.code},
            )
            delete_attempt(connection, attempt_id)
            update_progress_meta(
                connection,
                ledger_version=new_version,
                watermark_seq=max(watermark_seq, operation.seq),
                logical_clock_ms=operation.clock_ms,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {
            "seq": operation.seq,
            "status": "rejected",
            "error": error.to_dict(),
        }

    def _commit_operation(
        self,
        connection: sqlite3.Connection,
        *,
        operation: Operation,
        engine: MaintenanceEngine,
        outcome: OperationOutcome,
        base_ledger_version: int,
        attempt_id: str,
        watermark_seq: int,
    ) -> dict[str, Any]:
        new_version = base_ledger_version + 1
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = check_ledger_consistency(connection)
            if current != base_ledger_version:
                raise BusinessError(
                    E_OP_CONFLICT,
                    "ledger changed while the operation was being prepared",
                    {"base": base_ledger_version, "current": current},
                )
            result = {
                "seq": operation.seq,
                "status": "committed",
                "affected_indexes": list(outcome.affected_indexes),
                "changed_snapshots": list(outcome.changed_snapshots),
                "split_events": len(outcome.split_events),
                "invalidations": len(outcome.invalidations),
                "clock_ms": operation.clock_ms,
            }
            store_operation(
                connection,
                seq=operation.seq,
                digest=operation.content_digest,
                status="committed",
                clock_ms=operation.clock_ms,
                ledger_version=new_version,
                result=result,
                error=None,
            )
            store_ledger_event(
                connection,
                ledger_version=new_version,
                kind="operation",
                seq=operation.seq,
                detail={
                    "digest": operation.content_digest,
                    "affected_indexes": list(outcome.affected_indexes),
                },
            )
            for name in outcome.changed_snapshots:
                store_snapshot(connection, name, engine.snapshot_for(name))
            replace_rows(connection, engine.rows)
            store_split_events(connection, new_version, outcome.split_events)
            store_invalidations(connection, new_version, outcome.invalidations)
            delete_attempt(connection, attempt_id)
            update_progress_meta(
                connection,
                ledger_version=new_version,
                watermark_seq=max(watermark_seq, operation.seq),
                logical_clock_ms=operation.clock_ms,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return result

    def _process_one_operation(
        self,
        connection: sqlite3.Connection,
        manifest: Manifest,
        operation: Operation,
        *,
        base_version: int,
        watermark_seq: int,
        crash_after_split: int | None,
        crash_after_secondary: int | None,
    ) -> dict[str, Any]:
        snapshots = read_snapshots(connection)
        rows = read_rows(connection)
        attempt_id = f"seq-{operation.seq}-base-{base_version}"
        split_seen = 0
        secondary_seen = 0

        def event_callback(event: dict[str, Any]) -> None:
            nonlocal split_seen
            split_seen += 1
            upsert_attempt(
                connection,
                attempt_id=attempt_id,
                seq=operation.seq,
                base_ledger_version=base_version,
                phase="split",
                staging={
                    "phase": "split",
                    "split_index": split_seen,
                    "event": event,
                },
                started_clock_ms=operation.clock_ms,
            )
            if crash_after_split is not None and split_seen >= crash_after_split:
                os._exit(97)

        def progress_callback(progress: dict[str, Any]) -> None:
            nonlocal secondary_seen
            if progress.get("phase") != "secondary":
                return
            secondary_seen += 1
            upsert_attempt(
                connection,
                attempt_id=attempt_id,
                seq=operation.seq,
                base_ledger_version=base_version,
                phase="secondary",
                staging={
                    "phase": "secondary",
                    "secondary_index": secondary_seen,
                    "progress": progress,
                },
                started_clock_ms=operation.clock_ms,
            )
            if (
                crash_after_secondary is not None
                and secondary_seen >= crash_after_secondary
            ):
                os._exit(97)

        connection.execute("BEGIN IMMEDIATE")
        try:
            clear_attempts_for_seq(connection, operation.seq)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        upsert_attempt(
            connection,
            attempt_id=attempt_id,
            seq=operation.seq,
            base_ledger_version=base_version,
            phase="started",
            staging={"phase": "started"},
            started_clock_ms=operation.clock_ms,
        )
        engine = MaintenanceEngine(
            manifest,
            snapshots=snapshots,
            rows=rows,
            event_callback=event_callback,
            progress_callback=progress_callback,
            verify_integrity=True,
        )
        try:
            outcome = engine.apply_operation(operation)
        except BusinessError as exc:
            if exc.code not in REJECTABLE_CODES:
                raise
            return self._reject_operation(
                connection,
                operation=operation,
                base_ledger_version=base_version,
                error=exc,
                attempt_id=attempt_id,
                watermark_seq=watermark_seq,
            )
        return self._commit_operation(
            connection,
            operation=operation,
            engine=engine,
            outcome=outcome,
            base_ledger_version=base_version,
            attempt_id=attempt_id,
            watermark_seq=watermark_seq,
        )

    def apply(
        self,
        *,
        sample_path: str | None = None,
        from_seq: int | None = None,
        to_seq: int | None = None,
        crash_after_split: int | None = None,
        crash_after_secondary: int | None = None,
        wait_timeout: float = 30.0,
    ) -> dict[str, Any]:
        if from_seq is not None and to_seq is not None and from_seq > to_seq:
            raise BusinessError(
                E_BAD_OP,
                "from_seq must not be greater than to_seq",
                {"from_seq": from_seq, "to_seq": to_seq},
            )
        applied: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        skipped: list[int] = []
        deadline = time.monotonic() + wait_timeout
        last_blocker: dict[str, Any] | None = None
        while True:
            progressed = False
            with self._lock():
                connection = open_initialized(self.db_path)
                try:
                    manifest = read_manifest(connection)
                    self._verify_external_sample(manifest, sample_path)
                    ledger_version = check_ledger_consistency(connection)
                    meta = read_meta(connection)
                    watermark_seq = int(meta["watermark_seq"])
                    operations_rows = read_operations(connection)
                    operations_by_seq = {
                        row["seq"]: row for row in operations_rows
                    }
                    self._check_operation_ledger(
                        manifest,
                        operations_by_seq,
                        from_seq=from_seq,
                        to_seq=to_seq,
                    )
                    requested_operations = [
                        operation
                        for operation in manifest.operations
                        if (from_seq is None or operation.seq >= from_seq)
                        and (to_seq is None or operation.seq <= to_seq)
                    ]
                    if requested_operations and all(
                        operation.seq in operations_by_seq
                        for operation in requested_operations
                    ):
                        break
                    next_pending = next(
                        (
                            operation
                            for operation in manifest.operations
                            if operation.seq not in operations_by_seq
                        ),
                        None,
                    )
                    if next_pending is None:
                        break
                    if from_seq is not None and next_pending.seq < from_seq:
                        last_blocker = {
                            "pending_seq": next_pending.seq,
                            "waiting_for_range_start": from_seq,
                        }
                    elif to_seq is not None and next_pending.seq > to_seq:
                        break
                    else:
                        if from_seq is not None and next_pending.seq < from_seq:
                            pass
                        else:
                            before_clock = int(meta["logical_clock_ms"])
                            clock_tolerance = int(meta["clock_tolerance_ms"])
                            if (
                                next_pending.clock_ms
                                < before_clock - clock_tolerance
                            ):
                                raise BusinessError(
                                    E_CLOCK_ROLLBACK,
                                    "operation logical clock moved backwards beyond tolerance",
                                    {
                                        "seq": next_pending.seq,
                                        "previous_clock_ms": before_clock,
                                        "clock_ms": next_pending.clock_ms,
                                        "tolerance_ms": clock_tolerance,
                                    },
                                )
                            result = self._process_one_operation(
                                connection,
                                manifest,
                                next_pending,
                                base_version=ledger_version,
                                watermark_seq=watermark_seq,
                                crash_after_split=crash_after_split,
                                crash_after_secondary=crash_after_secondary,
                            )
                            if result["status"] == "rejected":
                                rejected.append(result)
                            else:
                                applied.append(result)
                            progressed = True
                finally:
                    connection.close()
            if progressed:
                continue
            with self._lock():
                connection = open_initialized(self.db_path)
                try:
                    manifest = read_manifest(connection)
                    operations_by_seq = {
                        row["seq"]: row for row in read_operations(connection)
                    }
                    next_pending = next(
                        (
                            operation
                            for operation in manifest.operations
                            if operation.seq not in operations_by_seq
                        ),
                        None,
                    )
                    if next_pending is None:
                        break
                    if to_seq is not None and next_pending.seq > to_seq:
                        break
                    if from_seq is not None and next_pending.seq < from_seq:
                        skipped.extend(
                            operation.seq
                            for operation in manifest.operations
                            if from_seq <= operation.seq < next_pending.seq
                            and operation.seq in operations_by_seq
                        )
                finally:
                    connection.close()
            if time.monotonic() >= deadline:
                raise BusinessError(
                    E_RANGE_WAIT_TIMEOUT,
                    "timed out waiting for an earlier operation range to commit",
                    last_blocker or {"from_seq": from_seq, "to_seq": to_seq},
                )
            time.sleep(0.03)
        with self._lock():
            connection = open_initialized(self.db_path)
            try:
                summary = self._status_payload(connection)
            finally:
                connection.close()
        return {
            "command": "apply",
            "ok": True,
            "from_seq": from_seq,
            "to_seq": to_seq,
            "applied": applied,
            "rejected": rejected,
            "result": {
                "ledger_version": summary["ledger"]["version"],
                "watermark_seq": summary["ledger"]["watermark_seq"],
                "logical_clock_ms": summary["ledger"]["logical_clock_ms"],
                "state_digest": summary["state_digest"],
            },
        }

    def resume(
        self,
        *,
        sample_path: str | None = None,
        crash_after_split: int | None = None,
        crash_after_secondary: int | None = None,
    ) -> dict[str, Any]:
        discarded: list[dict[str, Any]] = []
        with self._lock():
            connection = open_initialized(self.db_path)
            try:
                manifest = read_manifest(connection)
                self._verify_external_sample(manifest, sample_path)
                check_ledger_consistency(connection)
                attempts = read_attempts(connection)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for attempt in attempts:
                        discarded.append(
                            {
                                "attempt_id": attempt["attempt_id"],
                                "seq": attempt["seq"],
                                "base_ledger_version": attempt["base_ledger_version"],
                                "phase": attempt["phase"],
                                "started_clock_ms": attempt["started_clock_ms"],
                            }
                        )
                        delete_attempt(connection, attempt["attempt_id"])
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            finally:
                connection.close()
        result = self.apply(
            sample_path=sample_path,
            crash_after_split=crash_after_split,
            crash_after_secondary=crash_after_secondary,
        )
        return {
            "command": "resume",
            "ok": True,
            "discarded_attempts": discarded,
            "applied": result["applied"],
            "rejected": result["rejected"],
            "result": result["result"],
        }

    def _status_payload(self, connection: sqlite3.Connection) -> dict[str, Any]:
        ledger_version = check_ledger_consistency(connection)
        manifest = read_manifest(connection)
        meta = read_meta(connection)
        snapshots = read_snapshots(connection)
        operations = read_operations(connection)
        attempts = read_attempts(connection)
        invalidations = read_invalidations(connection)
        indexes: dict[str, Any] = {}
        for name in sorted(snapshots):
            snapshot = snapshots[name]
            tree = snapshot["tree"]
            indexes[name] = {
                "kind": snapshot["kind"],
                "page_count": tree["page_count"],
                "height": tree["height"],
                "split_count": tree["split_count"],
                "chain_version": tree["chain_version"],
                "digest": snapshot["digest"],
                "tree_digest": tree["digest"],
                "last_change_seq": snapshot["last_change_seq"],
                "decision": snapshot["decision"],
                "fixed_point": {
                    "scale": manifest.config.fixed_scale,
                    "rounding": manifest.config.rounding,
                    "tolerance_units": manifest.config.tolerance_units,
                },
            }
        query_availability: dict[str, Any] = {}
        for query in sorted(manifest.query_templates, key=lambda item: item.name):
            query_availability[query.name] = {
                "available": True,
                "index": query.index,
                "last_action": "initial_build",
                "last_seq": 0,
                "last_recomputed_seq": 0,
                "last_recomputed_action": "initial_build",
                "safe_reuse": False,
            }
        invalidation_output: list[dict[str, Any]] = []
        for row in invalidations:
            record = row["record"]
            output = dict(record)
            output["ledger_version"] = row["ledger_version"]
            output["record_order"] = row["record_order"]
            invalidation_output.append(output)
            if record["kind"] == "query_availability":
                query_name = record["ref"]["query"]
                previous = query_availability[query_name]
                entry = {
                    "available": True,
                    "index": record["index"],
                    "last_action": record["action"],
                    "last_seq": record["seq"],
                    "last_recomputed_seq": previous["last_recomputed_seq"],
                    "last_recomputed_action": previous["last_recomputed_action"],
                    "safe_reuse": record["safe_reuse"],
                }
                if record["action"] in {"recompute", "rebuild"}:
                    entry["last_recomputed_seq"] = record["seq"]
                    entry["last_recomputed_action"] = record["action"]
                query_availability[query_name] = entry
        committed = sum(1 for operation in operations if operation["status"] == "committed")
        rejected = sum(1 for operation in operations if operation["status"] == "rejected")
        core = {
            "component": "local-index-maintenance",
            "format_version": 1,
            "sample_id": manifest.sample_id,
            "ledger": {
                "version": ledger_version,
                "watermark_seq": int(meta["watermark_seq"]),
                "logical_clock_ms": int(meta["logical_clock_ms"]),
                "sample_sha256": manifest.sample_sha256,
                "fixed_scale": manifest.config.fixed_scale,
                "rounding": manifest.config.rounding,
                "tolerance_units": manifest.config.tolerance_units,
            },
            "operations": {
                "committed": committed,
                "rejected": rejected,
                "total": len(operations),
            },
            "indexes": indexes,
            "queries": query_availability,
            "invalidations": {
                "count": len(invalidation_output),
                "items": invalidation_output,
            },
            "pending_attempts": attempts,
            "state_digest": snapshot_state_digest(snapshots),
        }
        core["status_digest"] = sha256_json(core)
        return core

    def status(self, *, sample_path: str | None = None) -> dict[str, Any]:
        with self._lock():
            connection = open_initialized(self.db_path)
            try:
                manifest = read_manifest(connection)
                self._verify_external_sample(manifest, sample_path)
                payload = self._status_payload(connection)
            finally:
                connection.close()
        return {"command": "status", "ok": True, "status": payload}

    def rebuild(
        self,
        *,
        index_name: str | None,
        rebuild_all: bool = False,
        sample_path: str | None = None,
    ) -> dict[str, Any]:
        if not rebuild_all and index_name is None:
            raise BusinessError(
                E_BAD_OP,
                "rebuild requires --index or --all",
            )
        with self._lock():
            connection = open_initialized(self.db_path)
            try:
                manifest = read_manifest(connection)
                self._verify_external_sample(manifest, sample_path)
                base_version = check_ledger_consistency(connection)
                attempts = read_attempts(connection)
                if attempts:
                    raise BusinessError(
                        E_UNFINISHED_ATTEMPT,
                        "manual rebuild requires unfinished attempts to be resumed first",
                        {
                            "attempts": [
                                {
                                    "attempt_id": attempt["attempt_id"],
                                    "seq": attempt["seq"],
                                    "phase": attempt["phase"],
                                }
                                for attempt in attempts
                            ]
                        },
                    )
                meta = read_meta(connection)
                clock_ms = int(meta["logical_clock_ms"])
                if rebuild_all:
                    targets = sorted(
                        [index.name for index in manifest.indexes]
                    )
                else:
                    assert index_name is not None
                    if index_name not in manifest.index_map:
                        raise BusinessError(
                            E_BAD_OP,
                            "rebuild target is not a declared secondary index",
                            {"index": index_name},
                        )
                    targets = [index_name]
                snapshots = read_snapshots(connection)
                rows = read_rows(connection)
                engine = MaintenanceEngine(
                    manifest,
                    snapshots=snapshots,
                    rows=rows,
                    verify_integrity=True,
                )
                target_results: list[dict[str, Any]] = []
                all_events: list[dict[str, Any]] = []
                all_invalidations: list[dict[str, Any]] = []
                for target in targets:
                    snapshot, records = engine.force_rebuild(
                        target,
                        clock_ms=clock_ms,
                        reason="manual_rebuild",
                    )
                    target_results.append(
                        {
                            "index": target,
                            "tree_digest": snapshot["tree"]["digest"],
                            "page_count": snapshot["tree"]["page_count"],
                            "height": snapshot["tree"]["height"],
                            "split_count": snapshot["tree"]["split_count"],
                            "actual_rebuild_splits": snapshot["decision"].get(
                                "actual_rebuild_splits", 0
                            ),
                        }
                    )
                    all_events.extend(engine.split_events)
                    all_invalidations.extend(records)
                for position, record in enumerate(all_invalidations, start=1):
                    record["record_order"] = position
                new_version = base_version + 1
                window_end = clock_ms + manifest.config.rebuild_window_ms
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current = check_ledger_consistency(connection)
                    if current != base_version:
                        raise BusinessError(
                            E_OP_CONFLICT,
                            "ledger changed while the rebuild was being prepared",
                            {"base": base_version, "current": current},
                        )
                    for target in targets:
                        store_snapshot(
                            connection,
                            target,
                            engine.snapshot_for(target),
                        )
                    store_split_events(connection, new_version, all_events)
                    store_invalidations(
                        connection,
                        new_version,
                        all_invalidations,
                    )
                    store_ledger_event(
                        connection,
                        ledger_version=new_version,
                        kind="manual_rebuild",
                        seq=None,
                        detail={
                            "indexes": targets,
                            "clock_ms": clock_ms,
                            "window_end_ms": window_end,
                        },
                    )
                    update_progress_meta(
                        connection,
                        ledger_version=new_version,
                        watermark_seq=int(meta["watermark_seq"]),
                        logical_clock_ms=window_end,
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                summary = self._status_payload(connection)
                return {
                    "command": "rebuild",
                    "ok": True,
                    "targets": target_results,
                    "started_clock_ms": clock_ms,
                    "window_end_ms": window_end,
                    "ledger_version": new_version,
                    "state_digest": summary["state_digest"],
                }
            finally:
                connection.close()

    def _expected_timeline(
        self,
        manifest: Manifest,
        ledger_events: list[dict[str, Any]],
        operations: dict[int, dict[str, Any]],
    ) -> tuple[
        MaintenanceEngine,
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        engine = MaintenanceEngine.initial(manifest)
        expected_split_events: list[dict[str, Any]] = []
        expected_invalidations: list[dict[str, Any]] = []
        operation_map = {operation.seq: operation for operation in manifest.operations}
        for event in ledger_events:
            kind = event["kind"]
            version = event["ledger_version"]
            if kind == "operation":
                seq = event["seq"]
                stored = operations.get(seq)
                if stored is None or stored["status"] != "committed":
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "ledger operation event has no committed operation row",
                        {"ledger_version": version, "seq": seq},
                    )
                operation = operation_map.get(seq)
                if operation is None:
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "ledger operation event references a missing manifest operation",
                        {"ledger_version": version, "seq": seq},
                    )
                outcome = engine.apply_operation(operation)
                for split_event in outcome.split_events:
                    expected_split_events.append(
                        {
                            "ledger_version": version,
                            "index_name": split_event["tree"],
                            "event_order": split_event["event_order"],
                            "event": split_event,
                        }
                    )
                for record in outcome.invalidations:
                    expected_invalidations.append(
                        {
                            "ledger_version": version,
                            "record_order": record["record_order"],
                            "record": record,
                        }
                    )
            elif kind == "manual_rebuild":
                detail = event["detail"]
                targets = detail.get("indexes", [])
                all_records: list[dict[str, Any]] = []
                for target in targets:
                    _, records = engine.force_rebuild(
                        target,
                        clock_ms=int(detail["clock_ms"]),
                        reason="manual_rebuild",
                    )
                    for split_event in engine.split_events:
                        expected_split_events.append(
                            {
                                "ledger_version": version,
                                "index_name": split_event["tree"],
                                "event_order": split_event["event_order"],
                                "event": split_event,
                            }
                        )
                    all_records.extend(records)
                for position, record in enumerate(all_records, start=1):
                    record["record_order"] = position
                    expected_invalidations.append(
                        {
                            "ledger_version": version,
                            "record_order": position,
                            "record": record,
                        }
                    )
            elif kind == "operation_rejected":
                continue
            else:
                raise BusinessError(
                    E_INCONSISTENT_STATE,
                    "unknown ledger event kind",
                    {"ledger_version": version, "kind": kind},
                )
        return engine, expected_split_events, expected_invalidations

    def verify(
        self,
        *,
        sample_path: str | None = None,
        expected_ledger_version: int | None = None,
    ) -> dict[str, Any]:
        with self._lock():
            connection = open_initialized(self.db_path)
            try:
                manifest = read_manifest(connection)
                self._verify_external_sample(manifest, sample_path)
                ledger_version = check_ledger_consistency(
                    connection,
                    expected_ledger_version=expected_ledger_version,
                )
                meta = read_meta(connection)
                attempts = read_attempts(connection)
                if attempts:
                    raise BusinessError(
                        E_UNFINISHED_ATTEMPT,
                        "commit verification requires unfinished attempts to be resumed first",
                        {
                            "attempts": [
                                {
                                    "attempt_id": attempt["attempt_id"],
                                    "seq": attempt["seq"],
                                    "phase": attempt["phase"],
                                }
                                for attempt in attempts
                            ]
                        },
                    )
                snapshots = read_snapshots(connection)
                rows = read_rows(connection)
                operations_rows = read_operations(connection)
                operations = {
                    row["seq"]: row for row in operations_rows
                }
                ledger_events = read_ledger_events(connection)
                if ledger_version == 0:
                    if ledger_events:
                        raise BusinessError(
                            E_LEDGER_ROLLBACK,
                            "ledger events exist while the ledger version is zero",
                            {"events": len(ledger_events)},
                        )
                else:
                    versions = [
                        int(event["ledger_version"]) for event in ledger_events
                    ]
                    if versions != list(range(1, ledger_version + 1)):
                        raise BusinessError(
                            E_LEDGER_ROLLBACK,
                            "ledger event versions are not a contiguous prefix of the ledger",
                            {
                                "ledger_version": ledger_version,
                                "event_versions": versions,
                            },
                        )
                operation_events = [
                    event
                    for event in ledger_events
                    if event["kind"] in {"operation", "operation_rejected"}
                ]
                event_seqs = sorted(int(event["seq"]) for event in operation_events)
                operation_seqs = sorted(int(row["seq"]) for row in operations_rows)
                if event_seqs != operation_seqs:
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "operation ledger rows and ledger events cover different sequences",
                        {
                            "operation_seqs": operation_seqs,
                            "event_seqs": event_seqs,
                        },
                    )
                expected_clock = manifest.config.logical_start_ms
                for event in ledger_events:
                    version = int(event["ledger_version"])
                    if event["kind"] == "operation":
                        seq = int(event["seq"])
                        stored_operation = operations[seq]
                        if stored_operation["status"] != "committed":
                            raise BusinessError(
                                E_INCONSISTENT_STATE,
                                "operation event points to a rejected operation row",
                                {"ledger_version": version, "seq": seq},
                            )
                        if (
                            event["detail"].get("digest")
                            != stored_operation["digest"]
                        ):
                            raise BusinessError(
                                E_INDEX_DIGEST_MISMATCH,
                                "operation ledger event digest differs from the operation row",
                                {"ledger_version": version, "seq": seq},
                            )
                        expected_clock = int(stored_operation["clock_ms"])
                    elif event["kind"] == "operation_rejected":
                        seq = int(event["seq"])
                        stored_operation = operations[seq]
                        if stored_operation["status"] != "rejected":
                            raise BusinessError(
                                E_INCONSISTENT_STATE,
                                "rejected event points to a committed operation row",
                                {"ledger_version": version, "seq": seq},
                            )
                        expected_clock = int(stored_operation["clock_ms"])
                    elif event["kind"] == "manual_rebuild":
                        expected_clock = int(
                            event["detail"]["window_end_ms"]
                        )
                if int(meta["logical_clock_ms"]) != expected_clock:
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "logical clock watermark does not match the finalized ledger",
                        {
                            "stored": int(meta["logical_clock_ms"]),
                            "expected": expected_clock,
                        },
                    )
                engine = MaintenanceEngine(
                    manifest,
                    snapshots=snapshots,
                    rows=rows,
                    verify_integrity=True,
                )
                replay_engine, expected_splits, expected_invalidations = (
                    self._expected_timeline(
                        manifest,
                        ledger_events,
                        operations,
                    )
                )
                expected_snapshots = replay_engine.all_snapshots()
                if canonical_json(expected_snapshots) != canonical_json(snapshots):
                    raise BusinessError(
                        E_INDEX_DIGEST_MISMATCH,
                        "replayed in-memory indexes do not match persisted snapshots",
                        {
                            "indexes": sorted(snapshots),
                            "expected_state_digest": snapshot_state_digest(
                                expected_snapshots
                            ),
                            "persisted_state_digest": snapshot_state_digest(
                                snapshots
                            ),
                        },
                    )
                if canonical_json(replay_engine.rows) != canonical_json(rows):
                    raise BusinessError(
                        E_INDEX_DIGEST_MISMATCH,
                        "replayed row state does not match persisted rows",
                        {
                            "expected_rows": len(replay_engine.rows),
                            "persisted_rows": len(rows),
                        },
                    )
                persisted_splits = read_split_events(connection)
                expected_splits_sorted = sorted(
                    expected_splits,
                    key=lambda row: (
                        row["ledger_version"],
                        row["index_name"],
                        row["event_order"],
                    ),
                )
                if canonical_json(expected_splits_sorted) != canonical_json(
                    persisted_splits
                ):
                    raise BusinessError(
                        E_INDEX_DIGEST_MISMATCH,
                        "replayed split chain events do not match persisted events",
                        {
                            "expected_events": len(expected_splits_sorted),
                            "persisted_events": len(persisted_splits),
                        },
                    )
                persisted_invalidations = read_invalidations(connection)
                if canonical_json(expected_invalidations) != canonical_json(
                    persisted_invalidations
                ):
                    raise BusinessError(
                        E_INDEX_DIGEST_MISMATCH,
                        "replayed invalidation records do not match persisted records",
                        {
                            "expected_records": len(expected_invalidations),
                            "persisted_records": len(persisted_invalidations),
                        },
                    )
                if (
                    int(meta["fixed_scale"]) != manifest.config.fixed_scale
                    or meta["rounding"] != manifest.config.rounding
                    or int(meta["tolerance_units"])
                    != manifest.config.tolerance_units
                    or int(meta["clock_tolerance_ms"])
                    != manifest.config.clock_tolerance_ms
                ):
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "stored fixed-point or logical-clock metadata differs from the manifest",
                        {
                            "stored_fixed_scale": int(meta["fixed_scale"]),
                            "manifest_fixed_scale": manifest.config.fixed_scale,
                            "stored_rounding": meta["rounding"],
                            "manifest_rounding": manifest.config.rounding,
                            "stored_tolerance_units": int(meta["tolerance_units"]),
                            "manifest_tolerance_units": manifest.config.tolerance_units,
                        },
                    )
                finalized_seqs = [row["seq"] for row in operations_rows]
                expected_watermark = max(finalized_seqs) if finalized_seqs else 0
                if int(meta["watermark_seq"]) != expected_watermark:
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "watermark does not match the finalized operation set",
                        {
                            "stored": int(meta["watermark_seq"]),
                            "expected": expected_watermark,
                        },
                    )
                for operation in manifest.operations:
                    stored = operations.get(operation.seq)
                    if stored is not None and stored["digest"] != operation.content_digest:
                        raise BusinessError(
                            E_INDEX_DIGEST_MISMATCH,
                            "operation ledger digest does not match manifest content",
                            {"seq": operation.seq},
                        )
                status = self._status_payload(connection)
                result = {
                    "command": "verify",
                    "ok": True,
                    "ledger_version": ledger_version,
                    "watermark_seq": expected_watermark,
                    "operations": {
                        "committed": sum(
                            1
                            for row in operations_rows
                            if row["status"] == "committed"
                        ),
                        "rejected": sum(
                            1
                            for row in operations_rows
                            if row["status"] == "rejected"
                        ),
                    },
                    "indexes": {
                        name: {
                            "digest": snapshot["digest"],
                            "tree_digest": snapshot["tree"]["digest"],
                            "page_count": snapshot["tree"]["page_count"],
                            "height": snapshot["tree"]["height"],
                            "split_count": snapshot["tree"]["split_count"],
                        }
                        for name, snapshot in sorted(snapshots.items())
                    },
                    "split_events": len(persisted_splits),
                    "invalidations": len(persisted_invalidations),
                    "state_digest": status["state_digest"],
                    "checks": {
                        "snapshot_digests": "ok",
                        "page_parent_and_sibling_links": "ok",
                        "operation_replay": "ok",
                        "split_event_replay": "ok",
                        "invalidation_replay": "ok",
                        "watermark_consistency": "ok",
                    },
                }
                result["verify_digest"] = sha256_json(result)
                return result
            finally:
                connection.close()
