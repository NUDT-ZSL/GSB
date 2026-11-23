from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .checkpoints import load_and_merge_checkpoints, merge_checkpoints
from .errors import AppError, ErrorCode
from .models import CheckpointVersion, SnapshotRow, WalRow
from .package import write_v2_package
from .store import Store
from .util import canonical_json, sha256_obj
from .wal import (
    compute_content_hash,
    load_wal,
    row_from_db,
    validate_rows_against_hashes,
    validate_wal_dependencies,
    validate_wal_ordering,
    validate_wal_rows,
)

FAULT_EXIT_CODE = 75


def _fault(point: str, txn_id: int | None = None) -> None:
    requested = os.environ.get("WALCP_FAULT_POINT")
    if requested != point:
        return
    target_text = os.environ.get("WALCP_FAULT_TXN", "")
    if target_text:
        target = int(target_text)
        if txn_id != target:
            return
    os._exit(FAULT_EXIT_CODE)


def _decode_json(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def _encode_value(value: dict[str, Any] | None) -> str | None:
    return canonical_json(value) if value is not None else None


def _row_value_map(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "table": row["table_name"],
        "key": row["row_key"],
        "version": int(row["version"]),
        "txn_id": int(row["txn_id"]),
        "deleted": bool(row["deleted"]),
        "value": _decode_json(row["value_json"]),
    }


class AppService:
    def __init__(self, db_path: Path, lock_timeout_seconds: float = 30.0) -> None:
        self.db_path = Path(db_path)
        self.lock_timeout_seconds = lock_timeout_seconds

    def _store(self) -> Store:
        return Store(self.db_path, self.lock_timeout_seconds)

    def load(
        self,
        samples_dir: Path,
        *,
        now_ms: int,
        anchor_ms: int | None = None,
        tolerance_ms: int = 0,
    ) -> dict[str, Any]:
        samples_dir = Path(samples_dir)
        effective_anchor = now_ms if anchor_ms is None else int(anchor_ms)
        wal_rows = load_wal(samples_dir / "wal")
        merge = load_and_merge_checkpoints(
            samples_dir / "checkpoints", effective_anchor
        )
        validate_wal_rows(wal_rows)
        validate_wal_dependencies(wal_rows, merge.generations)
        materialization = None
        if (
            merge.baseline is not None
            and merge.baseline.applied_watermark > 0
            and not merge.baseline.snapshot_rows
        ):
            prefix = [
                row
                for row in wal_rows
                if row.txn_id <= merge.baseline.applied_watermark
            ]
            try:
                state = _replay_records_to_map(
                    [row.as_record() for row in prefix]
                )
            except AppError as exc:
                raise AppError(
                    ErrorCode.CHECKPOINT_INVALID,
                    "checkpoint summary-only baseline cannot be materialized "
                    "from the WAL prefix",
                    {
                        "generation": merge.baseline.generation,
                        "watermark": merge.baseline.applied_watermark,
                        "reason": str(exc),
                    },
                ) from exc
            merge.baseline.snapshot_rows = [
                SnapshotRow(
                    table=value["table"],
                    key=value["key"],
                    version=value["version"],
                    txn_id=value["txn_id"],
                    deleted=value["deleted"],
                    value=value["value"],
                )
                for _, value in sorted(state.items())
            ]
            _validate_materialized_baseline(merge.baseline)
            materialization = {
                "reason": "checkpoint_format_contains_summaries_only",
                "source_format": merge.baseline.source_format,
                "generation": merge.baseline.generation,
                "watermark": merge.baseline.applied_watermark,
                "rows": len(merge.baseline.snapshot_rows),
            }
        with self._store() as store:
            with store.lock:
                store.connect(create=True)
                with store.transaction():
                    counts = store.replace_with_checkpoint(
                        merge, wal_rows, now_ms
                    )
                    store.check_clock(now_ms, tolerance_ms)
                pointer = store.current_pointer()
                return {
                    "ok": True,
                    "command": "load",
                    "counts": counts,
                    "watermark": store.get_meta_int("applied_watermark"),
                    "checkpoint_generation": (
                        pointer["generation"] if pointer is not None else 0
                    ),
                    "checkpoint_source_format": (
                        merge.baseline.source_format
                        if merge.baseline is not None
                        else None
                    ),
                    "baseline_materialization": materialization,
                    "merge": merge.report(),
                }

    def _read_effective_wal(self, store: Store) -> list[WalRow]:
        rows = store.wal_rows()
        validate_wal_ordering(rows)
        validate_wal_rows(rows)
        validate_wal_dependencies(rows, store.checkpoint_generations())
        return rows

    @staticmethod
    def _raw_wal(store: Store, minimum_txn: int = 0) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in store.db.execute(
                "SELECT * FROM _wal_rows WHERE txn_id > ? "
                "ORDER BY segment_no, line_no, txn_id",
                (minimum_txn,),
            )
        ]

    @staticmethod
    def _txn_status(store: Store, txn_id: int) -> str | None:
        row = store.db.execute(
            "SELECT status FROM _txn WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        return row["status"] if row is not None else None

    @staticmethod
    def _stage_pending(
        store: Store, raw: dict[str, Any], now_ms: int, tolerance_ms: int
    ) -> None:
        with store.transaction():
            store.check_clock(now_ms, tolerance_ms)
            store.db.execute(
                """
                INSERT INTO _txn
                (txn_id, status, commit_ms, checkpoint_gen, segment, line_no,
                 input_hash, replay_ms, latency_ms, precision_ms,
                 updated_clock_ms, note)
                VALUES (?, 'pending', ?, ?, ?, ?, ?, NULL, NULL, NULL, ?,
                        'registered before applying rows')
                ON CONFLICT(txn_id) DO UPDATE SET
                    status = CASE
                        WHEN _txn.status = 'committed' THEN _txn.status
                        ELSE 'pending'
                    END,
                    updated_clock_ms = excluded.updated_clock_ms,
                    note = excluded.note
                """,
                (
                    raw["txn_id"],
                    raw["commit_ms"],
                    raw["checkpoint_gen"],
                    raw["segment"],
                    raw["line_no"],
                    raw["content_hash"],
                    now_ms,
                ),
            )

    @staticmethod
    def _apply_row_state(store: Store, row: WalRow) -> None:
        current = store.db.execute(
            "SELECT * FROM _rows WHERE table_name = ? AND row_key = ?",
            (row.table, row.key),
        ).fetchone()
        if row.op in ("update", "delete"):
            if current is None or bool(current["deleted"]):
                raise AppError(
                    ErrorCode.REPLAY_ROW_MISSING,
                    f"{row.op} targets a missing live row during replay",
                    {
                        "txn_id": row.txn_id,
                        "table": row.table,
                        "key": row.key,
                        "op": row.op,
                    },
                )
        if row.op == "insert" and current is not None and not bool(current["deleted"]):
            raise AppError(
                ErrorCode.REPLAY_INCONSISTENCY,
                "insert targets a row that is already live",
                {
                    "txn_id": row.txn_id,
                    "table": row.table,
                    "key": row.key,
                    "existing_txn_id": int(current["txn_id"]),
                },
            )
        if current is not None:
            if row.txn_id <= int(current["txn_id"]):
                raise AppError(
                    ErrorCode.REPLAY_INCONSISTENCY,
                    "WAL transaction is not newer than the current row version",
                    {
                        "txn_id": row.txn_id,
                        "current_txn_id": int(current["txn_id"]),
                        "table": row.table,
                        "key": row.key,
                    },
                )
            version = int(current["version"]) + 1
        else:
            version = 1
        value = row.before if row.op == "delete" else row.after
        deleted = 1 if row.op == "delete" else 0
        value_json = _encode_value(value)
        history = store.db.execute(
            """
            SELECT txn_id FROM _row_history
            WHERE table_name = ? AND row_key = ? AND version = ?
            """,
            (row.table, row.key, version),
        ).fetchone()
        if history is not None and int(history["txn_id"]) != row.txn_id:
            raise AppError(
                ErrorCode.REPLAY_INCONSISTENCY,
                "row version is already owned by a different transaction",
                {
                    "table": row.table,
                    "key": row.key,
                    "version": version,
                    "existing_txn_id": int(history["txn_id"]),
                    "txn_id": row.txn_id,
                },
            )
        store.db.execute(
            """
            INSERT INTO _rows
            (table_name, row_key, version, txn_id, deleted, value_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(table_name, row_key) DO UPDATE SET
                version = excluded.version,
                txn_id = excluded.txn_id,
                deleted = excluded.deleted,
                value_json = excluded.value_json
            """,
            (row.table, row.key, version, row.txn_id, deleted, value_json),
        )
        store.db.execute(
            """
            INSERT INTO _row_history
            (table_name, row_key, version, txn_id, deleted, value_json,
             commit_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(table_name, row_key, version) DO UPDATE SET
                txn_id = excluded.txn_id,
                deleted = excluded.deleted,
                value_json = excluded.value_json,
                commit_ms = excluded.commit_ms
            """,
            (
                row.table,
                row.key,
                version,
                row.txn_id,
                deleted,
                value_json,
                row.commit_ms,
            ),
        )

    @staticmethod
    def _existing_reusable_delay(
        store: Store, txn_id: int
    ) -> dict[str, Any] | None:
        row = store.db.execute(
            "SELECT * FROM _delays WHERE txn_id = ? AND valid = 1",
            (txn_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _write_delay(
        store: Store,
        row: WalRow,
        replay_ms: int,
        now_ms: int,
    ) -> dict[str, Any]:
        if replay_ms < row.commit_ms:
            raise AppError(
                ErrorCode.CLOCK_BEFORE_COMMIT,
                "injected replay clock is earlier than transaction commit time",
                {
                    "txn_id": row.txn_id,
                    "commit_ms": row.commit_ms,
                    "replay_ms": replay_ms,
                },
            )
        latency_ms = replay_ms - row.commit_ms
        store.db.execute(
            """
            INSERT INTO _delays
            (txn_id, commit_ms, replay_ms, latency_ms, precision_ms,
             rounding, valid, source_clock_ms)
            VALUES (?, ?, ?, ?, 1, 'millisecond_exact', 1, ?)
            ON CONFLICT(txn_id) DO UPDATE SET
                commit_ms = excluded.commit_ms,
                replay_ms = excluded.replay_ms,
                latency_ms = excluded.latency_ms,
                precision_ms = excluded.precision_ms,
                rounding = excluded.rounding,
                valid = 1,
                source_clock_ms = excluded.source_clock_ms
            """,
            (
                row.txn_id,
                row.commit_ms,
                replay_ms,
                latency_ms,
                now_ms,
            ),
        )
        return {
            "txn_id": row.txn_id,
            "commit_ms": row.commit_ms,
            "replay_ms": replay_ms,
            "latency_ms": latency_ms,
            "precision_ms": 1,
            "rounding": "millisecond_exact",
            "reused": False,
        }

    def _apply_one(
        self,
        store: Store,
        raw: dict[str, Any],
        *,
        now_ms: int,
        replay_ms: int | None,
        tolerance_ms: int,
        force: bool,
        allow_delay_reuse: bool,
        operation_id: str,
    ) -> dict[str, Any]:
        txn_id = int(raw["txn_id"])
        status = self._txn_status(store, txn_id)
        if not force and status == "committed":
            return {"txn_id": txn_id, "status": "already_committed"}
        if bool(raw["deleted"]):
            with store.transaction():
                store.check_clock(now_ms, tolerance_ms)
                store.db.execute(
                    """
                    INSERT INTO _txn
                    (txn_id, status, commit_ms, checkpoint_gen, segment,
                     line_no, input_hash, replay_ms, latency_ms, precision_ms,
                     updated_clock_ms, note)
                    VALUES (?, 'deleted', ?, ?, ?, ?, ?, NULL, NULL, NULL, ?,
                            'WAL row deleted by user edit')
                    ON CONFLICT(txn_id) DO UPDATE SET
                        status = 'deleted',
                        updated_clock_ms = excluded.updated_clock_ms,
                        note = excluded.note
                    """,
                    (
                        txn_id,
                        raw["commit_ms"],
                        raw["checkpoint_gen"],
                        raw["segment"],
                        raw["line_no"],
                        raw["content_hash"],
                        now_ms,
                    ),
                )
                watermark = store.get_meta_int("applied_watermark")
                if txn_id > watermark:
                    store.set_meta("applied_watermark", txn_id)
            return {"txn_id": txn_id, "status": "deleted_noop"}
        row = row_from_db(raw)
        reusable = (
            self._existing_reusable_delay(store, txn_id)
            if allow_delay_reuse
            else None
        )
        if reusable is not None:
            effective_replay_ms = int(reusable["replay_ms"])
        elif replay_ms is not None:
            effective_replay_ms = replay_ms
        else:
            raise AppError(
                ErrorCode.RECOVERY_CONFLICT,
                "new replay time is required when no reusable delay exists",
                {"txn_id": txn_id},
            )

        if status != "pending":
            self._stage_pending(store, raw, now_ms, tolerance_ms)
            _fault("apply_after_pending", txn_id)

        with store.transaction():
            store.check_clock(now_ms, tolerance_ms)
            inner_status = self._txn_status(store, txn_id)
            if not force and inner_status == "committed":
                return {"txn_id": txn_id, "status": "already_committed"}
            self._apply_row_state(store, row)
            if reusable is not None:
                delay = {
                    "txn_id": txn_id,
                    "commit_ms": int(reusable["commit_ms"]),
                    "replay_ms": int(reusable["replay_ms"]),
                    "latency_ms": int(reusable["latency_ms"]),
                    "precision_ms": int(reusable["precision_ms"]),
                    "rounding": reusable["rounding"],
                    "reused": True,
                }
            else:
                store.check_clock(effective_replay_ms, tolerance_ms)
                delay = self._write_delay(
                    store, row, effective_replay_ms, now_ms
                )
            store.db.execute(
                """
                INSERT INTO _txn
                (txn_id, status, commit_ms, checkpoint_gen, segment, line_no,
                 input_hash, replay_ms, latency_ms, precision_ms,
                 updated_clock_ms, note)
                VALUES (?, 'committed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(txn_id) DO UPDATE SET
                    status = 'committed',
                    commit_ms = excluded.commit_ms,
                    checkpoint_gen = excluded.checkpoint_gen,
                    segment = excluded.segment,
                    line_no = excluded.line_no,
                    input_hash = excluded.input_hash,
                    replay_ms = excluded.replay_ms,
                    latency_ms = excluded.latency_ms,
                    precision_ms = excluded.precision_ms,
                    updated_clock_ms = excluded.updated_clock_ms,
                    note = excluded.note
                """,
                (
                    txn_id,
                    row.commit_ms,
                    row.checkpoint_gen,
                    row.segment,
                    row.line_no,
                    row.content_hash,
                    delay["replay_ms"],
                    delay["latency_ms"],
                    delay["precision_ms"],
                    now_ms,
                    "delay reused during rebuild" if delay["reused"] else None,
                ),
            )
            if txn_id > store.get_meta_int("applied_watermark"):
                store.set_meta("applied_watermark", txn_id)
            if not delay["reused"]:
                store.set_meta("last_replay_clock_ms", delay["replay_ms"])
            _fault("apply_after_rows", txn_id)
        return {"txn_id": txn_id, "status": "committed", "delay": delay}

    def _run_rebuild(
        self,
        store: Store,
        operation_id: str,
        *,
        now_ms: int,
        clock_step_ms: int,
        tolerance_ms: int,
        final_status: str,
    ) -> dict[str, Any]:
        baseline = store.current_version()
        if (
            baseline is not None
            and baseline.applied_watermark > 0
            and not baseline.snapshot_rows
        ):
            prefix = [
                row_from_db(raw).as_record()
                for raw in self._raw_wal(store)
                if int(raw["txn_id"]) <= baseline.applied_watermark
                and not bool(raw["deleted"])
            ]
            state = _replay_records_to_map(prefix)
            baseline.snapshot_rows = [
                SnapshotRow(
                    table=value["table"],
                    key=value["key"],
                    version=value["version"],
                    txn_id=value["txn_id"],
                    deleted=value["deleted"],
                    value=value["value"],
                )
                for _, value in sorted(state.items())
            ]
        with store.transaction():
            store.check_clock(now_ms, tolerance_ms)
            if baseline is not None:
                store._seed_baseline(baseline)
                watermark = baseline.applied_watermark
            else:
                store.db.execute("DELETE FROM _rows")
                store.db.execute("DELETE FROM _row_history")
                watermark = 0
            store.db.execute("DELETE FROM _txn")
            prefix = self._raw_wal(store)
            for raw in prefix:
                if int(raw["txn_id"]) <= watermark:
                    if bool(raw["deleted"]):
                        continue
                    store.db.execute(
                        """
                        INSERT INTO _txn
                        (txn_id, status, commit_ms, checkpoint_gen, segment,
                         line_no, input_hash, replay_ms, latency_ms,
                         precision_ms, updated_clock_ms, note)
                        VALUES (?, 'checkpointed', ?, ?, ?, ?, ?, NULL, NULL,
                                NULL, ?, 'restored from rebuild baseline')
                        """,
                        (
                            raw["txn_id"],
                            raw["commit_ms"],
                            raw["checkpoint_gen"],
                            raw["segment"],
                            raw["line_no"],
                            raw["content_hash"],
                            now_ms,
                        ),
                    )
                else:
                    store.db.execute(
                        """
                        INSERT INTO _txn
                        (txn_id, status, commit_ms, checkpoint_gen, segment,
                         line_no, input_hash, replay_ms, latency_ms,
                         precision_ms, updated_clock_ms, note)
                        VALUES (?, 'needs_replay', ?, ?, ?, ?, ?, NULL, NULL,
                                NULL, ?, 'scheduled after edit invalidation')
                        """,
                        (
                            raw["txn_id"],
                            raw["commit_ms"],
                            raw["checkpoint_gen"],
                            raw["segment"],
                            raw["line_no"],
                            raw["content_hash"],
                            now_ms,
                        ),
                    )
            store.set_meta("applied_watermark", watermark)
            store.set_meta("needs_rebuild", 1)
        _fault("rebuild_after_reset")
        results: list[dict[str, Any]] = []
        candidates = self._raw_wal(store, watermark)
        for index, raw in enumerate(candidates):
            txn_id = int(raw["txn_id"])
            reusable = self._existing_reusable_delay(store, txn_id)
            replay_ms = (
                None if reusable is not None else now_ms + index * clock_step_ms
            )
            result = self._apply_one(
                store,
                raw,
                now_ms=now_ms,
                replay_ms=replay_ms,
                tolerance_ms=tolerance_ms,
                force=True,
                allow_delay_reuse=True,
                operation_id=operation_id,
            )
            results.append(result)
        with store.transaction():
            store.check_clock(now_ms, tolerance_ms)
            store.set_meta("needs_rebuild", 0)
            store.set_meta("rebuild_from_txn", 0)
            store.finish_operation(
                operation_id,
                final_status,
                now_ms,
                {
                    "rebuilt": True,
                    "applied_txns": [
                        result["txn_id"]
                        for result in results
                        if result["status"] == "committed"
                    ],
                    "reused_delays": [
                        result["txn_id"]
                        for result in results
                        if result.get("delay", {}).get("reused")
                    ],
                },
            )
        return {
            "rebuilt": True,
            "baseline_generation": (
                baseline.generation if baseline is not None else 0
            ),
            "applied_txns": [
                result["txn_id"]
                for result in results
                if result["status"] == "committed"
            ],
            "reused_delays": [
                result["txn_id"]
                for result in results
                if result.get("delay", {}).get("reused")
            ],
        }

    def _run_apply(
        self,
        store: Store,
        operation_id: str,
        *,
        now_ms: int,
        clock_step_ms: int,
        tolerance_ms: int,
        final_status: str,
    ) -> dict[str, Any]:
        if store.get_meta("needs_rebuild") == "1":
            result = self._run_rebuild(
                store,
                operation_id,
                now_ms=now_ms,
                clock_step_ms=clock_step_ms,
                tolerance_ms=tolerance_ms,
                final_status=final_status,
            )
            result["watermark"] = store.get_meta_int("applied_watermark")
            return result
        watermark = store.get_meta_int("applied_watermark")
        candidates = self._raw_wal(store, watermark)
        results: list[dict[str, Any]] = []
        for index, raw in enumerate(candidates):
            txn_id = int(raw["txn_id"])
            reusable = self._existing_reusable_delay(store, txn_id)
            replay_ms = (
                None
                if reusable is not None
                else now_ms + index * clock_step_ms
            )
            result = self._apply_one(
                store,
                raw,
                now_ms=now_ms,
                replay_ms=replay_ms,
                tolerance_ms=tolerance_ms,
                force=False,
                allow_delay_reuse=True,
                operation_id=operation_id,
            )
            results.append(result)
        committed = [
            result["txn_id"]
            for result in results
            if result["status"] == "committed"
        ]
        reused = [
            result["txn_id"]
            for result in results
            if result.get("delay", {}).get("reused")
        ]
        with store.transaction():
            store.check_clock(now_ms, tolerance_ms)
            store.finish_operation(
                operation_id,
                final_status,
                now_ms,
                {
                    "applied_txns": committed,
                    "reused_delays": reused,
                    "already_committed": [
                        result["txn_id"]
                        for result in results
                        if result["status"] == "already_committed"
                    ],
                },
            )
        return {
            "rebuilt": False,
            "applied_txns": committed,
            "reused_delays": reused,
            "already_committed": [
                result["txn_id"]
                for result in results
                if result["status"] == "already_committed"
            ],
            "watermark": store.get_meta_int("applied_watermark"),
        }

    def _recover_pending_locked(
        self,
        store: Store,
        *,
        now_ms: int,
        clock_step_ms: int,
        tolerance_ms: int,
    ) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        pending = store.pending_operations()
        for operation in pending:
            op_id = operation["op_id"]
            kind = operation["kind"]
            if kind in {"edit", "checkpoint"}:
                with store.transaction():
                    store.finish_operation(
                        op_id,
                        "aborted",
                        now_ms,
                        {
                            "reason": (
                                "domain transaction did not commit before "
                                "process termination"
                            )
                        },
                    )
                    store.record_recovery(
                        now_ms,
                        "aborted_pending_operation",
                        {"op_id": op_id, "kind": kind},
                    )
                recovered.append(
                    {
                        "op_id": op_id,
                        "kind": kind,
                        "decision": "aborted_no_partial_state",
                    }
                )
            elif kind == "apply":
                result = self._run_apply(
                    store,
                    op_id,
                    now_ms=now_ms,
                    clock_step_ms=clock_step_ms,
                    tolerance_ms=tolerance_ms,
                    final_status="recovered",
                )
                with store.transaction():
                    store.record_recovery(
                        now_ms,
                        "completed_pending_apply",
                        {"op_id": op_id, "result": result},
                    )
                recovered.append(
                    {
                        "op_id": op_id,
                        "kind": kind,
                        "decision": "continued_missing_transactions",
                        "result": result,
                    }
                )
            else:
                with store.transaction():
                    store.finish_operation(
                        op_id, "aborted", now_ms, {"reason": "unknown operation"}
                    )
                recovered.append(
                    {"op_id": op_id, "kind": kind, "decision": "aborted"}
                )
        return recovered

    def apply(
        self,
        *,
        now_ms: int,
        clock_step_ms: int = 0,
        tolerance_ms: int = 0,
    ) -> dict[str, Any]:
        with self._store() as store:
            with store.lock:
                store.connect(create=False)
                store.ensure_initialized()
                self._read_effective_wal(store)
                recovery = self._recover_pending_locked(
                    store,
                    now_ms=now_ms,
                    clock_step_ms=clock_step_ms,
                    tolerance_ms=tolerance_ms,
                )
                op_id = store.begin_operation(
                    "apply",
                    {"now_ms": now_ms, "clock_step_ms": clock_step_ms},
                    now_ms,
                    tolerance_ms,
                )
                result = self._run_apply(
                    store,
                    op_id,
                    now_ms=now_ms,
                    clock_step_ms=clock_step_ms,
                    tolerance_ms=tolerance_ms,
                    final_status="completed",
                )
                return {
                    "ok": True,
                    "command": "apply",
                    "operation_id": op_id,
                    "recovery": recovery,
                    **result,
                }

    def resume(
        self,
        *,
        now_ms: int,
        clock_step_ms: int = 0,
        tolerance_ms: int = 0,
    ) -> dict[str, Any]:
        with self._store() as store:
            with store.lock:
                store.connect(create=False)
                store.ensure_initialized()
                self._read_effective_wal(store)
                pending_before = [
                    operation["op_id"]
                    for operation in store.pending_operations()
                ]
                recovery = self._recover_pending_locked(
                    store,
                    now_ms=now_ms,
                    clock_step_ms=clock_step_ms,
                    tolerance_ms=tolerance_ms,
                )
                op_id = store.begin_operation(
                    "apply",
                    {
                        "now_ms": now_ms,
                        "clock_step_ms": clock_step_ms,
                        "resume": True,
                    },
                    now_ms,
                    tolerance_ms,
                )
                result = self._run_apply(
                    store,
                    op_id,
                    now_ms=now_ms,
                    clock_step_ms=clock_step_ms,
                    tolerance_ms=tolerance_ms,
                    final_status="completed",
                )
                return {
                    "ok": True,
                    "command": "resume",
                    "operation_id": op_id,
                    "pending_operations_before": pending_before,
                    "recovery": recovery,
                    **result,
                }

    def _validate_edit(
        self,
        store: Store,
        txn_id: int,
        changes: dict[str, Any],
        delete: bool,
    ) -> tuple[WalRow, WalRow | None, list[WalRow], list[str]]:
        raw = store.wal_record(txn_id)
        if raw is None:
            raise AppError(
                ErrorCode.EDIT_TARGET_NOT_FOUND,
                "WAL transaction to edit does not exist",
                {"txn_id": txn_id},
            )
        if bool(raw["deleted"]):
            raise AppError(
                ErrorCode.EDIT_TARGET_NOT_FOUND,
                "WAL row is already deleted",
                {"txn_id": txn_id},
            )
        old = row_from_db(raw)
        allowed = {"table", "key", "op", "before", "after", "checkpoint_gen", "commit_ms"}
        unsupported = sorted(set(changes) - allowed)
        if unsupported:
            raise AppError(
                ErrorCode.UNSUPPORTED_EDIT,
                "edit contains unsupported fields",
                {"fields": unsupported, "allowed": sorted(allowed)},
            )
        before_values = old.as_record()
        changed_fields: list[str] = []
        new_values = dict(before_values)
        for field, value in changes.items():
            column = {
                "table": "table",
                "key": "key",
                "op": "op",
                "before": "before",
                "after": "after",
                "checkpoint_gen": "checkpoint_gen",
                "commit_ms": "commit_ms",
            }[field]
            if before_values[column] != value:
                changed_fields.append(field)
            new_values[column] = value
        new_values["content_hash"] = compute_content_hash(new_values)
        new = WalRow(
            segment=old.segment,
            segment_no=old.segment_no,
            line_no=old.line_no,
            txn_id=old.txn_id,
            table=new_values["table"],
            key=new_values["key"],
            op=new_values["op"],
            before=new_values["before"],
            after=new_values["after"],
            checkpoint_gen=new_values["checkpoint_gen"],
            content_hash=new_values["content_hash"],
            commit_ms=new_values["commit_ms"],
        )
        all_rows: list[WalRow] = []
        for record in self._raw_wal(store):
            if int(record["txn_id"]) == txn_id:
                if not delete:
                    all_rows.append(new)
            else:
                all_rows.append(row_from_db(record))
        validate_wal_ordering(all_rows)
        validate_wal_rows(all_rows)
        validate_wal_dependencies(all_rows, store.checkpoint_generations())
        return old, (None if delete else new), all_rows, changed_fields

    def edit(
        self,
        txn_id: int,
        *,
        changes: dict[str, Any] | None = None,
        delete: bool = False,
        now_ms: int,
        tolerance_ms: int = 0,
    ) -> dict[str, Any]:
        changes = dict(changes or {})
        if delete and changes:
            raise AppError(
                ErrorCode.UNSUPPORTED_EDIT,
                "delete and field changes are mutually exclusive",
            )
        with self._store() as store:
            with store.lock:
                store.connect(create=False)
                store.ensure_initialized()
                self._recover_pending_locked(
                    store,
                    now_ms=now_ms,
                    clock_step_ms=0,
                    tolerance_ms=tolerance_ms,
                )
                old, new, _, changed_fields = self._validate_edit(
                    store, txn_id, changes, delete
                )
                op_id = store.begin_operation(
                    "edit",
                    {
                        "txn_id": txn_id,
                        "changes": changes,
                        "delete": delete,
                    },
                    now_ms,
                    tolerance_ms,
                )
                with store.transaction():
                    store.check_clock(now_ms, tolerance_ms)
                    affected_pairs = {(old.table, old.key)}
                    if new is not None:
                        affected_pairs.add((new.table, new.key))
                    row_invalidations: list[dict[str, Any]] = []
                    impacted_txns = {txn_id}
                    for table, key in sorted(affected_pairs):
                        history = store.db.execute(
                            """
                            SELECT * FROM _row_history
                            WHERE table_name = ? AND row_key = ?
                              AND txn_id >= ?
                            ORDER BY version
                            """,
                            (table, key, txn_id),
                        ).fetchall()
                        for row in history:
                            impacted_txns.add(int(row["txn_id"]))
                            row_invalidations.append(
                                {
                                    "table": table,
                                    "key": key,
                                    "version": int(row["version"]),
                                    "txn_id": int(row["txn_id"]),
                                    "action": "recompute",
                                    "reason": (
                                        "row version is at or downstream of the "
                                        "edited WAL transaction"
                                    ),
                                }
                            )
                    if not any(
                        item["txn_id"] == txn_id for item in row_invalidations
                    ):
                        row_invalidations.append(
                            {
                                "table": old.table,
                                "key": old.key,
                                "version": None,
                                "txn_id": txn_id,
                                "action": "recompute",
                                "reason": (
                                    "transaction result is not materialized yet"
                                ),
                            }
                        )
                    commit_changed = (
                        delete or new is None or new.commit_ms != old.commit_ms
                    )
                    delay_invalidations: list[dict[str, Any]] = []
                    if commit_changed:
                        delay_invalidations.append(
                            {
                                "txn_id": txn_id,
                                "action": "recompute",
                                "reason": (
                                    "transaction was deleted or its commit time "
                                    "changed"
                                ),
                            }
                        )
                        store.db.execute(
                            "UPDATE _delays SET valid = 0 WHERE txn_id = ?",
                            (txn_id,),
                        )
                    else:
                        delay_invalidations.append(
                            {
                                "txn_id": txn_id,
                                "action": "reuse",
                                "reason": (
                                    "commit and replay timestamps are unchanged"
                                ),
                            }
                        )
                    for impacted in sorted(impacted_txns - {txn_id}):
                        delay_invalidations.append(
                            {
                                "txn_id": impacted,
                                "action": "reuse",
                                "reason": (
                                    "only its row dependency changed; commit and "
                                    "replay timestamps are unchanged"
                                ),
                            }
                        )
                    checkpoint_invalidations: list[dict[str, Any]] = []
                    versions = store.checkpoint_versions()
                    for version in versions:
                        if (
                            version.applied_watermark >= txn_id
                            and not version.invalidated
                        ):
                            checkpoint_invalidations.append(
                                {
                                    "generation": version.generation,
                                    "source_format": version.source_format,
                                    "source_path": version.source_path,
                                    "applied_watermark": version.applied_watermark,
                                    "action": "invalidate",
                                    "reason": (
                                        "checkpoint watermark covers the edited "
                                        "transaction"
                                    ),
                                }
                            )
                            store.db.execute(
                                "UPDATE _checkpoint_versions SET invalidated = 1 "
                                "WHERE version_id = ?",
                                (version.version_id,),
                            )
                    baseline_row = store.db.execute(
                        """
                        SELECT * FROM _checkpoint_versions
                        WHERE selected = 1 AND invalidated = 0
                          AND applied_watermark < ?
                        ORDER BY generation DESC LIMIT 1
                        """,
                        (txn_id,),
                    ).fetchone()
                    baseline_changed = False
                    pointer = store.current_pointer()
                    pointer_was_invalidated = any(
                        pointer is not None
                        and item["generation"] == pointer["generation"]
                        and item["action"] == "invalidate"
                        for item in checkpoint_invalidations
                    )
                    if pointer_was_invalidated:
                        baseline_changed = True
                        if baseline_row is not None:
                            baseline_version = store._version_from_row(
                                dict(baseline_row)
                            )
                            store.replace_pointer_in_transaction(
                                baseline_version, now_ms
                            )
                            store.set_meta(
                                "applied_watermark",
                                baseline_version.applied_watermark,
                            )
                        else:
                            store.clear_pointer()
                            store.set_meta("applied_watermark", 0)
                    if delete:
                        store.db.execute(
                            "UPDATE _wal_rows SET deleted = 1, updated_clock_ms = ? "
                            "WHERE txn_id = ?",
                            (now_ms, txn_id),
                        )
                    elif new is not None:
                        store.db.execute(
                            """
                            UPDATE _wal_rows SET
                                table_name = ?, row_key = ?, op = ?,
                                before_json = ?, after_json = ?,
                                checkpoint_gen = ?, content_hash = ?,
                                commit_ms = ?, updated_clock_ms = ?
                            WHERE txn_id = ?
                            """,
                            (
                                new.table,
                                new.key,
                                new.op,
                                _encode_value(new.before),
                                _encode_value(new.after),
                                new.checkpoint_gen,
                                new.content_hash,
                                new.commit_ms,
                                now_ms,
                                txn_id,
                            ),
                        )
                    for impacted in sorted(impacted_txns):
                        store.db.execute(
                            "UPDATE _txn SET status = 'stale', "
                            "updated_clock_ms = ? WHERE txn_id = ?",
                            (now_ms, impacted),
                        )
                    store.set_meta("needs_rebuild", 1)
                    store.set_meta("rebuild_from_txn", txn_id)
                    trigger = {
                        "kind": "wal_row",
                        "txn_id": txn_id,
                        "segment": old.segment,
                        "line_no": old.line_no,
                        "action": "delete" if delete else "modify",
                        "fields_changed": changed_fields,
                    }
                    reuse_denials = [
                        {
                            "ref": (
                                f"row_version:{item['table']}:{item['key']}:"
                                f"{item['version']}"
                            ),
                            "denied_because": item["reason"],
                        }
                        for item in row_invalidations
                    ] + [
                        {
                            "ref": (
                                f"checkpoint:{item['source_format']}:"
                                f"{item['generation']}"
                            ),
                            "denied_because": item["reason"],
                        }
                        for item in checkpoint_invalidations
                    ]
                    report = {
                        "ok": True,
                        "command": "edit",
                        "txn_id": txn_id,
                        "operation_id": op_id,
                        "trigger": trigger,
                        "closure": {
                            "row_versions": row_invalidations,
                            "checkpoint_refs": checkpoint_invalidations,
                            "delay_records": delay_invalidations,
                        },
                        "safe_reuse": [
                            item
                            for item in delay_invalidations
                            if item["action"] == "reuse"
                        ],
                        "reuse_denials": reuse_denials,
                        "rebuild": {
                            "required": True,
                            "from_txn": txn_id,
                            "baseline_generation": (
                                baseline_version.generation
                                if baseline_changed
                                and baseline_row is not None
                                else (
                                    0
                                    if baseline_changed
                                    else store.get_meta_int(
                                        "current_checkpoint_gen"
                                    )
                                )
                            ),
                            "baseline_changed": baseline_changed,
                        },
                        "dry_run": False,
                    }
                    cursor = store.db.execute(
                        """
                        INSERT INTO _edits
                        (txn_id, action, fields_changed_json,
                         before_row_json, after_row_json, clock_ms, report_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            txn_id,
                            "delete" if delete else "modify",
                            canonical_json(changed_fields),
                            canonical_json(old.as_record()),
                            (
                                canonical_json(new.as_record())
                                if new is not None
                                else None
                            ),
                            now_ms,
                            canonical_json(report),
                        ),
                    )
                    edit_id = int(cursor.lastrowid)
                    for item in row_invalidations:
                        store.db.execute(
                            """
                            INSERT INTO _invalidations
                            (edit_id, kind, ref, action, reason, trigger_txn,
                             clock_ms, details_json)
                            VALUES (?, 'row_version', ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                edit_id,
                                (
                                    f"{item['table']}:{item['key']}:"
                                    f"{item['version']}"
                                ),
                                item["action"],
                                item["reason"],
                                txn_id,
                                now_ms,
                                canonical_json(item),
                            ),
                        )
                    for item in checkpoint_invalidations:
                        store.db.execute(
                            """
                            INSERT INTO _invalidations
                            (edit_id, kind, ref, action, reason, trigger_txn,
                             clock_ms, details_json)
                            VALUES (?, 'checkpoint_ref', ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                edit_id,
                                f"{item['source_format']}:{item['generation']}",
                                item["action"],
                                item["reason"],
                                txn_id,
                                now_ms,
                                canonical_json(item),
                            ),
                        )
                    for item in delay_invalidations:
                        store.db.execute(
                            """
                            INSERT INTO _invalidations
                            (edit_id, kind, ref, action, reason, trigger_txn,
                             clock_ms, details_json)
                            VALUES (?, 'delay_record', ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                edit_id,
                                f"txn:{item['txn_id']}",
                                item["action"],
                                item["reason"],
                                txn_id,
                                now_ms,
                                canonical_json(item),
                            ),
                        )
                    report["edit_id"] = edit_id
                    store.finish_operation(op_id, "completed", now_ms, report)
                    _fault("edit_before_commit")
                return report

    def _current_snapshot_rows(self, store: Store) -> list[SnapshotRow]:
        return [
            SnapshotRow(
                table=row["table_name"],
                key=row["row_key"],
                version=int(row["version"]),
                txn_id=int(row["txn_id"]),
                deleted=bool(row["deleted"]),
                value=_decode_json(row["value_json"]),
            )
            for row in store.db.execute(
                "SELECT * FROM _rows ORDER BY table_name, row_key"
            )
        ]

    def checkpoint(
        self,
        *,
        now_ms: int,
        out_dir: Path | None = None,
        package: Path | None = None,
        full_replace: bool = False,
        tolerance_ms: int = 0,
    ) -> dict[str, Any]:
        if package is not None:
            return self._receive_checkpoint(
                package,
                now_ms=now_ms,
                tolerance_ms=tolerance_ms,
            )
        if out_dir is None:
            raise AppError(
                ErrorCode.CHECKPOINT_INVALID,
                "checkpoint create requires --out",
            )
        return self._create_checkpoint(
            out_dir,
            now_ms=now_ms,
            full_replace=full_replace,
            tolerance_ms=tolerance_ms,
        )

    def _create_checkpoint(
        self,
        out_dir: Path,
        *,
        now_ms: int,
        full_replace: bool,
        tolerance_ms: int,
    ) -> dict[str, Any]:
        with self._store() as store:
            with store.lock:
                store.connect(create=False)
                store.ensure_initialized()
                if store.get_meta("needs_rebuild") == "1":
                    raise AppError(
                        ErrorCode.STATE_STALE,
                        "apply or resume invalidated edits before checkpointing",
                    )
                op_id = store.begin_operation(
                    "checkpoint",
                    {"out_dir": str(out_dir), "full_replace": full_replace},
                    now_ms,
                    tolerance_ms,
                )
                try:
                    pointer = store.current_pointer()
                    versions = store.checkpoint_versions()
                    max_generation = max(
                        (version.generation for version in versions), default=0
                    )
                    new_generation = max_generation + 1
                    manifest_version = max(
                        (version.manifest_version for version in versions),
                        default=0,
                    ) + 1
                    max_replay = max(
                        (version.replay_generation for version in versions),
                        default=0,
                    )
                    new_replay = max_replay + 1
                    new_version = CheckpointVersion(
                        generation=new_generation,
                        source_format="v2",
                        source_path=str(out_dir / "metadata.sqlite"),
                        source_hash="",
                        saved_at_ms=now_ms,
                        replay_generation=new_replay,
                        applied_watermark=store.get_meta_int("applied_watermark"),
                        snapshot_rows=self._current_snapshot_rows(store),
                        tables={},
                        manifest_version=manifest_version,
                        base_generation=(
                            0
                            if full_replace
                            else (int(pointer["generation"]) if pointer else 0)
                        ),
                        base_manifest_hash=(
                            None
                            if full_replace
                            else (pointer["manifest_hash"] if pointer else None)
                        ),
                        manifest={
                            "replay_generation_floor": (
                                int(pointer["replay_generation"]) if pointer else 0
                            )
                        },
                        selected=True,
                    )
                    versions_to_write = [new_version]
                    if full_replace:
                        versions_to_write = [
                            version
                            for version in versions
                            if version.selected and not version.invalidated
                        ] + [new_version]
                    package_prefix = str(Path(out_dir).resolve()) + os.sep
                    owned = [
                        int(row["generation"])
                        for row in store.db.execute(
                            "SELECT generation FROM _checkpoint_versions "
                            "WHERE source_path LIKE ?",
                            (package_prefix + "%",),
                        )
                    ]
                    if owned:
                        raise AppError(
                            ErrorCode.CHECKPOINT_CONFLICT,
                            "output path already owns a committed checkpoint",
                            {
                                "path": str(out_dir),
                                "generations": sorted(owned),
                            },
                        )
                    package_report = write_v2_package(
                        Path(out_dir), versions_to_write, overwrite_same=True
                    )
                    with store.transaction():
                        store.check_clock(now_ms, tolerance_ms)
                        existing = store.db.execute(
                            "SELECT 1 FROM _checkpoint_versions "
                            "WHERE generation = ?",
                            (new_generation,),
                        ).fetchone()
                        if existing is not None:
                            raise AppError(
                                ErrorCode.CHECKPOINT_CONFLICT,
                                "checkpoint generation was already committed",
                                {"generation": new_generation},
                            )
                        new_version.selected = True
                        store.insert_checkpoint_version(new_version)
                        previous_saved = (
                            int(pointer["saved_at_ms"]) if pointer else None
                        )
                        interval = (
                            now_ms - previous_saved
                            if previous_saved is not None
                            else None
                        )
                        store.db.execute(
                            """
                            INSERT INTO _checkpoint_intervals
                            (new_generation, previous_generation,
                             previous_saved_at_ms, new_saved_at_ms,
                             interval_ms, precision_ms, clock_ms)
                            VALUES (?, ?, ?, ?, ?, 1, ?)
                            """,
                            (
                                new_generation,
                                int(pointer["generation"]) if pointer else 0,
                                previous_saved,
                                now_ms,
                                interval,
                                now_ms,
                            ),
                        )
                        store.replace_pointer_in_transaction(
                            new_version, now_ms
                        )
                        store.set_meta(
                            "last_checkpoint_interval_ms",
                            "" if interval is None else interval,
                        )
                        report = {
                            "ok": True,
                            "command": "checkpoint",
                            "decision": "created",
                            "generation": new_generation,
                            "manifest_version": manifest_version,
                            "replay_generation": new_replay,
                            "applied_watermark": new_version.applied_watermark,
                            "saved_at_ms": now_ms,
                            "interval_ms": interval,
                            "interval_precision_ms": 1,
                            "package": str(out_dir),
                            "package_hash": package_report["package_hash"],
                            "full_replace_package": full_replace,
                        }
                        store.finish_operation(op_id, "completed", now_ms, report)
                        _fault("checkpoint_before_commit")
                    return report
                except BaseException:
                    if store.connection is not None:
                        try:
                            with store.transaction():
                                store.finish_operation(
                                    op_id,
                                    "aborted",
                                    now_ms,
                                    {"error": "checkpoint create failed"},
                                )
                        except Exception:
                            pass
                    raise

    def _receive_checkpoint(
        self,
        package: Path,
        *,
        now_ms: int,
        tolerance_ms: int,
    ) -> dict[str, Any]:
        from .checkpoints import parse_v2_package

        incoming = parse_v2_package(Path(package))
        if not incoming:
            raise AppError(
                ErrorCode.CHECKPOINT_INVALID,
                "incoming checkpoint package contains no versions",
            )
        incoming.sort(key=lambda version: version.generation)
        max_incoming = incoming[-1]
        with self._store() as store:
            with store.lock:
                store.connect(create=False)
                store.ensure_initialized()
                pointer = store.current_pointer()
                current_generation = int(pointer["generation"]) if pointer else 0
                current_manifest_version = (
                    int(pointer["manifest_version"]) if pointer else 0
                )
                current_manifest_hash = (
                    pointer["manifest_hash"] if pointer else None
                )
                current_watermark = store.get_meta_int("applied_watermark")
                current_replay = (
                    int(pointer["replay_generation"]) if pointer else 0
                )
                if (
                    max_incoming.generation < current_generation
                    or max_incoming.manifest_version < current_manifest_version
                ):
                    raise AppError(
                        ErrorCode.CHECKPOINT_ROLLBACK,
                        "incoming checkpoint version is older than the current pointer",
                        {
                            "current_generation": current_generation,
                            "incoming_generation": max_incoming.generation,
                            "current_manifest_version": current_manifest_version,
                            "incoming_manifest_version": max_incoming.manifest_version,
                        },
                    )
                if (
                    max_incoming.generation == current_generation
                    and max_incoming.manifest_hash == current_manifest_hash
                ):
                    decision = "noop"
                elif max_incoming.generation == current_generation:
                    raise AppError(
                        ErrorCode.CHECKPOINT_CONFLICT,
                        "same checkpoint generation has a different manifest hash",
                        {
                            "generation": max_incoming.generation,
                            "current_manifest_hash": current_manifest_hash,
                            "incoming_manifest_hash": max_incoming.manifest_hash,
                        },
                    )
                elif (
                    max_incoming.base_generation == current_generation
                    and max_incoming.base_manifest_hash == current_manifest_hash
                    and max_incoming.replay_generation >= current_replay
                    and max_incoming.applied_watermark >= current_watermark
                ):
                    decision = "incremental_append"
                elif (
                    max_incoming.base_generation == 0
                    or any(
                        version.generation == current_generation
                        and version.manifest_hash == current_manifest_hash
                        for version in incoming
                    )
                ):
                    decision = "full_replace"
                else:
                    raise AppError(
                        ErrorCode.CHECKPOINT_CONFLICT,
                        "incoming checkpoint does not extend or replace the current chain",
                        {
                            "current_generation": current_generation,
                            "incoming_generation": max_incoming.generation,
                            "incoming_base_generation": max_incoming.base_generation,
                        },
                    )
                if max_incoming.saved_at_ms < now_ms - 10**15:
                    pass
                if max_incoming.applied_watermark > current_watermark:
                    raise AppError(
                        ErrorCode.STATE_STALE,
                        "incoming checkpoint is ahead of locally applied WAL",
                        {
                            "checkpoint_watermark": max_incoming.applied_watermark,
                            "current_watermark": current_watermark,
                        },
                    )
                if max_incoming.applied_watermark == current_watermark:
                    current_snapshot = self._current_snapshot_rows(store)
                    if _snapshot_map(current_snapshot) != _snapshot_map(
                        max_incoming.snapshot_rows
                    ):
                        raise AppError(
                            ErrorCode.CHECKPOINT_CONFLICT,
                            "incoming checkpoint snapshot does not match local state",
                            {"generation": max_incoming.generation},
                        )
                op_id = store.begin_operation(
                    "checkpoint",
                    {"package": str(package), "decision": decision},
                    now_ms,
                    tolerance_ms,
                )
                with store.transaction():
                    store.check_clock(now_ms, tolerance_ms)
                    if decision == "noop":
                        report = {
                            "ok": True,
                            "command": "checkpoint",
                            "decision": "noop",
                            "generation": current_generation,
                            "manifest_version": current_manifest_version,
                            "package": str(package),
                        }
                    else:
                        selected_by_generation: dict[int, CheckpointVersion] = {}
                        for version in incoming:
                            previous = selected_by_generation.get(
                                version.generation
                            )
                            if previous is None or (
                                version.saved_at_ms,
                                version.replay_generation,
                            ) > (
                                previous.saved_at_ms,
                                previous.replay_generation,
                            ):
                                selected_by_generation[version.generation] = version
                        for version in incoming:
                            version.selected = (
                                version is selected_by_generation[version.generation]
                            )
                        if decision == "full_replace":
                            package_generations = {
                                version.generation for version in incoming
                            }
                            effective = self._read_effective_wal(store)
                            validate_wal_dependencies(
                                effective, package_generations
                            )
                            store.db.execute("DELETE FROM _checkpoint_pointer")
                            store.db.execute("DELETE FROM _checkpoint_snapshot")
                            store.db.execute("DELETE FROM _checkpoint_shards")
                            store.db.execute("DELETE FROM _checkpoint_versions")
                            versions_to_store = incoming
                        else:
                            existing = {
                                version.generation
                                for version in store.checkpoint_versions()
                            }
                            versions_to_store = [
                                version
                                for version in incoming
                                if version.generation > current_generation
                            ]
                            if not versions_to_store:
                                raise AppError(
                                    ErrorCode.CHECKPOINT_CONFLICT,
                                    "incremental package has no new generation",
                                )
                            if any(
                                version.generation in existing
                                for version in versions_to_store
                            ):
                                raise AppError(
                                    ErrorCode.CHECKPOINT_CONFLICT,
                                    "incremental package reuses a committed generation",
                                )
                        stored: list[CheckpointVersion] = []
                        for version in versions_to_store:
                            store.insert_checkpoint_version(version)
                            stored.append(version)
                        pointer_version = max(
                            stored, key=lambda version: version.generation
                        )
                        store.replace_pointer_in_transaction(
                            pointer_version, now_ms
                        )
                        previous_pointer = pointer
                        if previous_pointer is not None:
                            interval = (
                                pointer_version.saved_at_ms
                                - int(previous_pointer["saved_at_ms"])
                            )
                        else:
                            interval = None
                        store.db.execute(
                            """
                            INSERT OR REPLACE INTO _checkpoint_intervals
                            (new_generation, previous_generation,
                             previous_saved_at_ms, new_saved_at_ms,
                             interval_ms, precision_ms, clock_ms)
                            VALUES (?, ?, ?, ?, ?, 1, ?)
                            """,
                            (
                                pointer_version.generation,
                                current_generation,
                                (
                                    int(pointer["saved_at_ms"])
                                    if pointer is not None
                                    else None
                                ),
                                pointer_version.saved_at_ms,
                                interval,
                                now_ms,
                            ),
                        )
                        report = {
                            "ok": True,
                            "command": "checkpoint",
                            "decision": decision,
                            "generation": pointer_version.generation,
                            "manifest_version": pointer_version.manifest_version,
                            "replay_generation": pointer_version.replay_generation,
                            "applied_watermark": pointer_version.applied_watermark,
                            "package": str(package),
                            "compared": {
                                "current_generation": current_generation,
                                "current_manifest_version": current_manifest_version,
                                "current_manifest_hash": current_manifest_hash,
                                "incoming_generation": max_incoming.generation,
                                "incoming_manifest_version": (
                                    max_incoming.manifest_version
                                ),
                                "incoming_replay_generation": (
                                    max_incoming.replay_generation
                                ),
                                "incoming_replay_generation_floor": (
                                    max_incoming.manifest.get(
                                        "replay_generation_floor",
                                        max_incoming.replay_generation,
                                    )
                                ),
                                "shard_hashes": [
                                    shard["shard_hash"]
                                    for shard in max_incoming.shards
                                ],
                            },
                        }
                    store.finish_operation(op_id, "completed", now_ms, report)
                    return report

    def status(self) -> dict[str, Any]:
        with self._store() as store:
            with store.lock:
                store.connect(create=False)
                store.ensure_initialized()
                pointer = store.current_pointer()
                delays = store.delay_rows()
                valid_delays = [row for row in delays if int(row["valid"]) == 1]
                latencies = [int(row["latency_ms"]) for row in valid_delays]
                txn_rows = store.txn_rows()
                applied_statuses = {"committed", "checkpointed", "deleted"}
                applied = [
                    row for row in txn_rows if row["status"] in applied_statuses
                ]
                pending = [
                    row for row in txn_rows if row["status"] == "pending"
                ]
                edit_row = store.db.execute(
                    "SELECT COUNT(*) AS count FROM _edits"
                ).fetchone()
                return {
                    "ok": True,
                    "command": "status",
                    "watermark": store.get_meta_int("applied_watermark"),
                    "checkpoint_generation": (
                        int(pointer["generation"]) if pointer else 0
                    ),
                    "checkpoint": (
                        {
                            "generation": int(pointer["generation"]),
                            "manifest_version": int(pointer["manifest_version"]),
                            "replay_generation": int(
                                pointer["replay_generation"]
                            ),
                            "applied_watermark": int(
                                pointer["applied_watermark"]
                            ),
                            "saved_at_ms": int(pointer["saved_at_ms"]),
                            "manifest_hash": pointer["manifest_hash"],
                        }
                        if pointer
                        else None
                    ),
                    "applied_transactions": len(applied),
                    "pending_transactions": [
                        int(row["txn_id"]) for row in pending
                    ],
                    "applied_transaction_ids": [
                        int(row["txn_id"]) for row in applied
                    ],
                    "delay_summary": {
                        "count": len(latencies),
                        "min_latency_ms": min(latencies) if latencies else None,
                        "max_latency_ms": max(latencies) if latencies else None,
                        "total_latency_ms": sum(latencies),
                        "precision_ms": 1,
                        "rounding": "millisecond_exact",
                        "records": [
                            {
                                "txn_id": int(row["txn_id"]),
                                "commit_ms": int(row["commit_ms"]),
                                "replay_ms": int(row["replay_ms"]),
                                "latency_ms": int(row["latency_ms"]),
                            }
                            for row in valid_delays
                        ],
                    },
                    "needs_rebuild": store.get_meta("needs_rebuild") == "1",
                    "rebuild_from_txn": store.get_meta_int("rebuild_from_txn"),
                    "edit_count": int(edit_row["count"]),
                    "pending_operations": [
                        operation["op_id"]
                        for operation in store.pending_operations()
                    ],
                    "last_clock_ms": store.get_meta_int("last_clock_ms"),
                    "last_checkpoint_interval_ms": (
                        int(store.get_meta("last_checkpoint_interval_ms"))
                        if store.get_meta("last_checkpoint_interval_ms")
                        else None
                    ),
                }

    def checkpoint_list(self) -> dict[str, Any]:
        with self._store() as store:
            with store.lock:
                store.connect(create=False)
                store.ensure_initialized()
                pointer = store.current_pointer()
                return {
                    "ok": True,
                    "command": "checkpoint",
                    "decision": "list",
                    "pointer_generation": (
                        int(pointer["generation"]) if pointer else 0
                    ),
                    "versions": [
                        {
                            "generation": version.generation,
                            "source_format": version.source_format,
                            "source_path": version.source_path,
                            "source_hash": version.source_hash,
                            "saved_at_ms": version.saved_at_ms,
                            "replay_generation": version.replay_generation,
                            "applied_watermark": version.applied_watermark,
                            "manifest_version": version.manifest_version,
                            "manifest_hash": version.manifest_hash,
                            "selected": version.selected,
                            "invalidated": version.invalidated,
                            "decision_reason": version.decision_reason,
                            "v1_conversion": version.conversion,
                        }
                        for version in store.checkpoint_versions()
                    ],
                }

    def verify(self) -> dict[str, Any]:
        with self._store() as store:
            with store.lock:
                store.connect(create=False)
                store.ensure_initialized()
                effective = self._read_effective_wal(store)
                full = _replay_records_to_map(
                    [
                        row.as_record()
                        for row in effective
                    ]
                )
                pointer = store.current_pointer()
                baseline = store.current_version()
                if baseline is not None:
                    incremental = _snapshot_map(baseline.snapshot_rows)
                    watermark = baseline.applied_watermark
                else:
                    incremental = {}
                    watermark = 0
                incremental = _replay_records_to_map(
                    [
                        row.as_record()
                        for row in effective
                        if row.txn_id > watermark
                    ],
                    incremental,
                )
                actual = store.current_rows_map()
                differences = []
                for key in sorted(set(actual) | set(full) | set(incremental)):
                    actual_value = actual.get(key)
                    incremental_value = incremental.get(key)
                    full_value = full.get(key)
                    if (
                        actual_value != full_value
                        or incremental_value != full_value
                    ):
                        differences.append(
                            {
                                "table": key[0],
                                "key": key[1],
                                "actual": actual_value,
                                "incremental": incremental_value,
                                "full_replay": full_value,
                                "actual_txn_id": (
                                    actual_value["txn_id"]
                                    if actual_value
                                    else None
                                ),
                                "incremental_txn_id": (
                                    incremental_value["txn_id"]
                                    if incremental_value
                                    else None
                                ),
                                "full_txn_id": (
                                    full_value["txn_id"] if full_value else None
                                ),
                            }
                        )
                        break
                stale = store.get_meta("needs_rebuild") == "1"
                report = {
                    "ok": not differences and not stale,
                    "command": "verify",
                    "matched": not differences and not stale,
                    "needs_rebuild": stale,
                    "watermark": store.get_meta_int("applied_watermark"),
                    "checkpoint_generation": (
                        int(pointer["generation"]) if pointer else 0
                    ),
                    "baseline_watermark": watermark,
                    "row_count": len(full),
                    "actual_row_count": len(actual),
                    "incremental_row_count": len(incremental),
                    "full_replay_row_count": len(full),
                    "first_difference": differences[0] if differences else None,
                    "effective_wal_txns": [row.txn_id for row in effective],
                }
                if differences:
                    report["error_code"] = ErrorCode.REPLAY_INCONSISTENCY.value
                elif stale:
                    report["error_code"] = ErrorCode.STATE_STALE.value
                return report


def _snapshot_map(
    rows: list[SnapshotRow],
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (row.table, row.key): row.as_dict()
        for row in rows
    }


def _validate_materialized_baseline(version: CheckpointVersion) -> None:
    from .checkpoints import table_summary

    actual = table_summary(version.snapshot_rows)
    if version.source_format == "v1":
        declared = version.manifest.get("raw", {}).get("tables", {})
    else:
        declared = version.tables
    if not isinstance(declared, dict):
        return
    for table, summary in declared.items():
        if not isinstance(summary, dict):
            continue
        actual_summary = actual.get(table, {"row_count": 0, "content_digest": None})
        if (
            "row_count" in summary
            and summary["row_count"] is not None
            and summary["row_count"] != actual_summary["row_count"]
        ):
            raise AppError(
                ErrorCode.CHECKPOINT_INVALID,
                "materialized checkpoint row count does not match its summary",
                {
                    "generation": version.generation,
                    "table": table,
                    "declared": summary["row_count"],
                    "actual": actual_summary["row_count"],
                },
            )
        declared_digest = summary.get("digest", summary.get("content_digest"))
        if (
            declared_digest is not None
            and declared_digest != actual_summary["content_digest"]
        ):
            raise AppError(
                ErrorCode.CHECKPOINT_INVALID,
                "materialized checkpoint digest does not match its summary",
                {
                    "generation": version.generation,
                    "table": table,
                    "declared": declared_digest,
                    "actual": actual_summary["content_digest"],
                },
            )


def _replay_records_to_map(
    records: list[dict[str, Any]],
    initial: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    state = {
        key: {
            "table": value["table"],
            "key": value["key"],
            "version": int(value["version"]),
            "txn_id": int(value["txn_id"]),
            "deleted": bool(value["deleted"]),
            "value": value["value"],
        }
        for key, value in (initial or {}).items()
    }
    for record in records:
        if record.get("deleted"):
            continue
        table = record["table"]
        key = record["key"]
        op = record["op"]
        pair = (table, key)
        current = state.get(pair)
        if op in ("update", "delete") and (
            current is None or current["deleted"]
        ):
            raise AppError(
                ErrorCode.REPLAY_INCONSISTENCY,
                "full replay encountered a missing target row",
                {"txn_id": record["txn_id"], "table": table, "key": key},
            )
        version = 1 if current is None else current["version"] + 1
        state[pair] = {
            "table": table,
            "key": key,
            "version": version,
            "txn_id": int(record["txn_id"]),
            "deleted": op == "delete",
            "value": record["before"] if op == "delete" else record["after"],
        }
    return state
