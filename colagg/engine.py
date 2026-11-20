from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .canonical import (
    atomic_write_bytes,
    canonical_dumps,
    sha256_bytes,
    sha256_file,
    sha256_json,
    strict_json_loads,
)
from .errors import (
    E_BAD_BATCH,
    E_DIGEST_MISMATCH,
    E_FIXED_OVERFLOW,
    E_MALFORMED_COLUMN,
    E_NOT_FOUND,
    E_RECOVERY_FAILED,
    E_SPILL_READBACK_FAILED,
    E_SPILL_TAMPERED,
    E_STATE_TOO_LARGE,
    E_WINDOW_MISMATCH,
    EngineError,
)
from .filelock import FileLock
from .models import (
    Batch,
    Column,
    GroupState,
    Injection,
    Plan,
    Query,
    StatState,
    aggregate_result_payload,
    group_hash,
    window_result_payload,
)
from .numeric import convert_fixed, ensure_fixed_bounds, raw_float_repr
from .spill import SpillManager, SpillRecord
from .storage import Store, compute_state_digest
from .validation import parse_batch


@dataclass(slots=True)
class PendingWindowResult:
    group_hash: str
    group_key_json: str
    version: int
    item_id: str
    window_start: int
    window_end: int
    payload: dict[str, Any]
    forced: bool = False


@dataclass(slots=True)
class BatchOutcome:
    batch_seq: int
    status: str
    rows_processed: int = 0
    groups_updated: int = 0
    windows_emitted: int = 0
    spills: int = 0
    readbacks: int = 0
    failures: int = 0
    error_code: str | None = None
    error_details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_seq": self.batch_seq,
            "status": self.status,
            "rows_processed": self.rows_processed,
            "groups_updated": self.groups_updated,
            "windows_emitted": self.windows_emitted,
            "spills": self.spills,
            "readbacks": self.readbacks,
            "failures": self.failures,
            "error_code": self.error_code,
            "error_details": self.error_details,
        }


class Engine:
    def __init__(self, store: Store, plan: Plan) -> None:
        self.store = store
        self.plan = plan
        self.query = plan.query
        self.config = plan.config
        self.mode = plan.config.numeric_mode
        self.spill = SpillManager(store.root, self.config.spill_dir)
        self.cache: dict[str, GroupState] = {}
        self.resident_bytes = 0
        self.touched: set[str] = set()
        self.spilled_refs: dict[str, SpillRecord] = {}
        self.failed_groups: set[str] = set()
        self.pending_windows: list[PendingWindowResult] = []
        self.aggregate_results: dict[str, list[tuple[str, dict[str, Any], int]]] = {}
        self.audited: set[tuple[int, int, str]] = set()
        self.touch_counter = 0
        self.batch_seq = 0
        self.batch_clock = 0
        self.spill_count = 0
        self.readback_count = 0
        self.failure_count = 0

    def execute(
        self,
        *,
        target_seq: int | None = None,
        max_batches: int | None = None,
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        outcomes: list[BatchOutcome] = []
        skipped: list[dict[str, Any]] = []
        while max_batches is None or len(outcomes) < max_batches:
            with self.store.transaction():
                row = self.store.next_pending_batch(self.mode)
                if row is None:
                    if target_seq is not None:
                        existing = self.store.batch_status(self.mode, target_seq)
                        if existing is not None:
                            skipped.append(
                                {
                                    "batch_seq": target_seq,
                                    "status": existing["status"],
                                    "idempotent": existing["status"] in {"committed", "failed"},
                                }
                            )
                        else:
                            raise EngineError(
                                E_NOT_FOUND,
                                "target batch is not loaded",
                                target_batch_seq=target_seq,
                            )
                    break
                if target_seq is not None:
                    existing_target = self.store.batch_status(self.mode, target_seq)
                    if existing_target is None:
                        raise EngineError(
                            E_NOT_FOUND,
                            "target batch is not loaded",
                            target_batch_seq=target_seq,
                        )
                    if existing_target["status"] != "pending":
                        skipped.append(
                            {
                                "batch_seq": target_seq,
                                "status": existing_target["status"],
                                "idempotent": existing_target["status"]
                                in {"committed", "failed"},
                            }
                        )
                        break
                    if int(row["batch_seq"]) > target_seq:
                        break
                outcome = self._process_locked_batch(row, continue_on_error)
                outcomes.append(outcome)
                if target_seq is not None and int(row["batch_seq"]) == target_seq:
                    break
                if not continue_on_error and outcome.status == "failed":
                    break
        return {
            "mode": self.mode,
            "processed": [item.as_dict() for item in outcomes],
            "skipped": skipped,
            "logical_clock": self.store.clock(),
            "state_digest": self.store.state_digest(),
            "watermark": int(self.store.get_meta("watermark", "0") or 0),
        }

    def _process_locked_batch(
        self,
        row: sqlite3.Row,
        continue_on_error: bool,
    ) -> BatchOutcome:
        batch_seq = int(row["batch_seq"])
        self._reset_batch_state(batch_seq)
        step = self.config.logical_clock_step
        start_clock, self.batch_clock = self.store.advance_clock(step)
        outcome = BatchOutcome(batch_seq=batch_seq, status="pending")

        try:
            raw = strict_json_loads(row["payload_json"])
            injected_malformed = self._inject_read_anomalies(raw, batch_seq)
            batch = parse_batch(
                raw,
                self.config,
                expected_seq=batch_seq,
            )
            if batch.digest != row["batch_digest"]:
                raise EngineError(
                    E_DIGEST_MISMATCH,
                    "stored batch digest does not match batch payload",
                    batch_seq=batch_seq,
                    expected_digest=row["batch_digest"],
                    actual_digest=batch.digest,
                )
        except EngineError as exc:
            code = exc.code
            if injected_malformed and code == E_BAD_BATCH:
                code = E_MALFORMED_COLUMN
            self._mark_batch_failed(
                batch_seq,
                code,
                exc.message,
                phase="read",
                details=exc.details,
                clock=self.batch_clock,
                column=exc.details.get("column"),
            )
            outcome.status = "failed"
            outcome.error_code = code
            outcome.error_details = exc.as_dict()
            outcome.failures = 1
            self._finish_batch_metadata(batch_seq, outcome, row_digest=row["batch_digest"])
            return outcome
        except Exception as exc:
            self._mark_batch_failed(
                batch_seq,
                E_BAD_BATCH,
                "batch payload could not be decoded",
                phase="read",
                details={"error": repr(exc)},
                clock=self.batch_clock,
                column=None,
            )
            outcome.status = "failed"
            outcome.error_code = E_BAD_BATCH
            outcome.error_details = {"message": "batch payload could not be decoded"}
            outcome.failures = 1
            self._finish_batch_metadata(batch_seq, outcome, row_digest=row["batch_digest"])
            return outcome

        try:
            self._process_rows(batch, outcome)
            self._apply_end_of_batch_injections(batch, outcome)
        except EngineError as exc:
            self._mark_batch_failed(
                batch_seq,
                exc.code,
                exc.message,
                phase="aggregate",
                details=exc.details,
                clock=self.batch_clock,
                column=exc.details.get("column"),
            )
            outcome.status = "failed"
            outcome.error_code = exc.code
            outcome.error_details = exc.as_dict()
            outcome.failures += 1
            self._finish_batch_metadata(batch_seq, outcome, row_digest=batch.digest)
            return outcome

        self._commit_batch_state(batch, outcome)
        outcome.status = "committed"
        self._finish_batch_metadata(batch_seq, outcome, row_digest=batch.digest)
        return outcome

    def _reset_batch_state(self, batch_seq: int) -> None:
        self.cache.clear()
        self.resident_bytes = 0
        self.touched.clear()
        self.spilled_refs.clear()
        self.failed_groups.clear()
        self.pending_windows.clear()
        self.aggregate_results.clear()
        self.audited.clear()
        self.spill_count = 0
        self.readback_count = 0
        self.failure_count = 0
        self.batch_seq = batch_seq
        max_touch = self.store.conn.execute(
            "SELECT COALESCE(MAX(last_touch), 0) AS n FROM group_states WHERE mode = ?",
            (self.mode,),
        ).fetchone()
        self.touch_counter = int(max_touch["n"] if max_touch is not None else 0)

    def _inject_read_anomalies(self, raw: dict[str, Any], batch_seq: int) -> bool:
        malformed = False
        for injection in self.plan.manifest.matching("read", batch_seq):
            if injection.kind == "malformed_column":
                columns = raw.get("columns")
                if isinstance(columns, list):
                    for column in columns:
                        if isinstance(column, dict) and column.get("name") == injection.column:
                            values = column.get("values")
                            if isinstance(values, list):
                                if values:
                                    column["values"] = values[:-1]
                                else:
                                    column["values"] = [False]
                            malformed = True
                            break
            elif injection.kind == "digest_mismatch":
                raw["batch_digest"] = "0" * 64
        return malformed

    def _process_rows(self, batch: Batch, outcome: BatchOutcome) -> None:
        columns = batch.by_name
        group_columns = [columns[name] for name in self.query.group_by]
        value_columns = [columns[name] for name in self.query.value_columns]

        for row_index in range(batch.row_count):
            key = tuple(column.values[row_index] for column in group_columns)
            key_hash = group_hash(key)
            if key_hash in self.failed_groups:
                continue
            try:
                state = self._get_state(key, key_hash)
                if state is None:
                    continue
                if self._has_fixed_overflow_injection(key_hash):
                    raise EngineError(
                        E_FIXED_OVERFLOW,
                        "injected fixed-point overflow",
                        batch_seq=batch.batch_seq,
                        group=list(key),
                        column=self.query.window.column,
                    )

                numeric_values: dict[str, float | int] = {}
                fixed_values: dict[str, int] = {}
                for column in value_columns:
                    raw_value = column.values[row_index]
                    if column.type == "int":
                        numeric = int(raw_value)
                        raw_float = float(raw_value)
                    else:
                        numeric = float(raw_value)
                        raw_float = numeric
                    if self.mode == "fixed":
                        try:
                            conversion = convert_fixed(
                                numeric,
                                self.config.fixed_scale,
                                self.config.fixed_min,
                                self.config.fixed_max,
                            )
                        except EngineError as exc:
                            exc.details.update(
                                {
                                    "batch_seq": batch.batch_seq,
                                    "row_index": row_index,
                                    "column": column.name,
                                    "group": list(key),
                                    "fixed_int_bits": self.config.fixed_int_bits,
                                }
                            )
                            raise
                        self._record_conversion(
                            batch.batch_seq,
                            row_index,
                            column.name,
                            conversion.raw_repr,
                            conversion.scale,
                            conversion.scaled,
                            conversion.direction,
                            conversion.nonfinite,
                        )
                        numeric_values[column.name] = raw_float
                        if conversion.scaled is not None:
                            fixed_values[column.name] = conversion.scaled
                    else:
                        nonfinite = None
                        if math.isnan(raw_float):
                            nonfinite = "nan"
                        elif math.isinf(raw_float):
                            nonfinite = "positive_infinity" if raw_float > 0 else "negative_infinity"
                        self._record_conversion(
                            batch.batch_seq,
                            row_index,
                            column.name,
                            raw_float_repr(raw_float),
                            0,
                            None,
                            "float64",
                            nonfinite,
                        )
                        numeric_values[column.name] = raw_float

                self._update_statistics(
                    state,
                    numeric_values,
                    fixed_values,
                    batch.batch_seq,
                    row_index,
                    key,
                )
                self._append_window_value(state, numeric_values[self.query.window.column])
                self._emit_ready_windows(state, batch.batch_seq, key)
                self._refresh_aggregate_results(state)
                self._enforce_memory(state, key)
                outcome.rows_processed += 1
            except EngineError as exc:
                if exc.code in {
                    E_FIXED_OVERFLOW,
                    E_STATE_TOO_LARGE,
                    E_WINDOW_MISMATCH,
                    E_SPILL_TAMPERED,
                    E_SPILL_READBACK_FAILED,
                    E_DIGEST_MISMATCH,
                }:
                    state = self.cache.get(key_hash)
                    self._mark_group_failure(
                        key=key,
                        key_hash=key_hash,
                        state=state,
                        code=exc.code,
                        phase=self._phase_for_error(exc.code),
                        details=exc.details,
                    )
                    continue
                raise

    def _has_fixed_overflow_injection(self, key_hash: str) -> bool:
        if self.mode != "fixed":
            return False
        for injection in self.plan.manifest.matching(
            "aggregate",
            self.batch_seq,
            kind="fixed_overflow",
        ):
            if injection.group is not None and group_hash(injection.group) == key_hash:
                return True
        return False

    def _get_state(
        self,
        key: tuple[Any, ...],
        key_hash: str,
    ) -> GroupState | None:
        cached = self.cache.get(key_hash)
        if cached is not None:
            self._touch(cached)
            return cached
        if key_hash in self.failed_groups:
            return None
        active_spill = self.spilled_refs.get(key_hash)
        if active_spill is not None:
            try:
                state = self._read_spilled_record(active_spill, key)
            except EngineError as exc:
                self._mark_group_failure(
                    key=key,
                    key_hash=key_hash,
                    state=None,
                    code=exc.code,
                    phase="read",
                    details=exc.details,
                )
                return None
            self.spilled_refs.pop(key_hash, None)
            self._touch(state)
            self._cache_add(state, key)
            state.dirty = True
            return state
        failed = self.store.conn.execute(
            "SELECT 1 FROM group_failures WHERE mode = ? AND group_hash = ?",
            (self.mode, key_hash),
        ).fetchone()
        if failed is not None:
            self.failed_groups.add(key_hash)
            return None

        row = self.store.conn.execute(
            "SELECT * FROM group_states WHERE mode = ? AND group_hash = ?",
            (self.mode, key_hash),
        ).fetchone()
        state: GroupState
        if row is None:
            state = GroupState(
                key=key,
                group_hash=key_hash,
                version=0,
                row_count=0,
                stats={
                    name: StatState()
                    for name in self.query.value_columns
                },
                window_values=[],
                window_base=0,
                last_touch=0,
                status="active",
                dirty=False,
                loaded_version=0,
            )
        else:
            if row["status"] != "active":
                self.failed_groups.add(key_hash)
                return None
            if row["state_json"] is not None:
                payload = strict_json_loads(row["state_json"])
                state = GroupState.from_payload(payload, self.mode)
                state.loaded_version = int(row["version"])
                state.version = int(row["version"])
            else:
                try:
                    state = self._read_spill_state(row, key)
                except EngineError as exc:
                    self._mark_group_failure(
                        key=key,
                        key_hash=key_hash,
                        state=None,
                        code=exc.code,
                        phase="read",
                        details=exc.details,
                    )
                    return None
                state.loaded_version = int(row["version"])
                state.version = int(row["version"])

        for name in self.query.value_columns:
            state.stats.setdefault(name, StatState())
        self._touch(state)
        self._cache_add(state, key)
        state.dirty = True
        state.version = state.loaded_version + 1
        self.touched.add(key_hash)
        return state

    def _read_spilled_record(
        self,
        record: SpillRecord,
        key: tuple[Any, ...],
    ) -> GroupState:
        key_hash = group_hash(key)
        inject_corrupt = False
        for injection in self.plan.manifest.matching(
            "read",
            self.batch_seq,
            kind="spill_readback_corrupt",
        ):
            if injection.group is not None and group_hash(injection.group) == key_hash:
                inject_corrupt = True
                break
        cost = self.config.logical_clock_step * (
            1 + int(math.ceil(record.size_bytes / 4096))
        )
        start, end = self.store.advance_clock(cost)
        try:
            read = self.spill.read(
                mode=self.mode,
                spill_id=record.spill_id,
                rel_path=record.rel_path,
                expected_digest=record.sha256,
                inject_corrupt=inject_corrupt,
            )
        except EngineError as exc:
            self.store.conn.execute(
                "UPDATE spill_files SET status = 'readback_failed' WHERE spill_id = ?",
                (record.spill_id,),
            )
            self._record_spill_event(
                direction="readback",
                group_hash_value=key_hash,
                size_bytes=record.size_bytes,
                start=start,
                end=end,
                reused=False,
            )
            raise
        self._record_spill_event(
            direction="readback",
            group_hash_value=key_hash,
            size_bytes=read.size_bytes,
            start=start,
            end=end,
            reused=False,
        )
        self.readback_count += 1
        return read.state

    def _read_spill_state(
        self,
        row: sqlite3.Row,
        key: tuple[Any, ...],
    ) -> GroupState:
        spill_id = row["spill_id"]
        spill_row = self.store.conn.execute(
            "SELECT * FROM spill_files WHERE spill_id = ?",
            (spill_id,),
        ).fetchone()
        if spill_row is None:
            raise EngineError(
                E_SPILL_READBACK_FAILED,
                "group state references a missing spill manifest row",
                spill_id=spill_id,
                batch_seq=self.batch_seq,
                group=list(key),
            )
        inject_corrupt = False
        for injection in self.plan.manifest.matching(
            "read",
            self.batch_seq,
            kind="spill_readback_corrupt",
        ):
            if injection.group is not None and group_hash(injection.group) == row["group_hash"]:
                inject_corrupt = True
                break
        cost = self.config.logical_clock_step * (
            1 + int(math.ceil(int(spill_row["size_bytes"]) / 4096))
        )
        start, end = self.store.advance_clock(cost)
        try:
            read = self.spill.read(
                mode=self.mode,
                spill_id=spill_id,
                rel_path=spill_row["rel_path"],
                expected_digest=spill_row["sha256"],
                inject_corrupt=inject_corrupt,
            )
        except EngineError as exc:
            self.store.conn.execute(
                """
                UPDATE spill_files SET status = 'readback_failed'
                WHERE spill_id = ?
                """,
                (spill_id,),
            )
            self._record_spill_event(
                direction="readback",
                group_hash_value=row["group_hash"],
                size_bytes=int(spill_row["size_bytes"]),
                start=start,
                end=end,
                reused=False,
            )
            self._mark_group_failure(
                key=key,
                key_hash=row["group_hash"],
                state=None,
                code=E_SPILL_READBACK_FAILED,
                phase="read",
                details=exc.details,
            )
            raise
        self._record_spill_event(
            direction="readback",
            group_hash_value=row["group_hash"],
            size_bytes=read.size_bytes,
            start=start,
            end=end,
            reused=False,
        )
        self.readback_count += 1
        return read.state

    def _record_conversion(
        self,
        batch_seq: int,
        row_index: int,
        column: str,
        raw_repr: str,
        scale: int,
        scaled: int | None,
        direction: str,
        nonfinite: str | None,
    ) -> None:
        audit_key = (batch_seq, row_index, column)
        if audit_key in self.audited:
            return
        self.audited.add(audit_key)
        self.store.conn.execute(
            """
            INSERT OR IGNORE INTO conversion_audit
                (mode, batch_seq, row_index, column_name, raw_repr, scale,
                 scaled, direction, nonfinite)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.mode,
                batch_seq,
                row_index,
                column,
                raw_repr,
                scale,
                scaled,
                direction,
                nonfinite,
            ),
        )

    def _update_statistics(
        self,
        state: GroupState,
        numeric_values: dict[str, float | int],
        fixed_values: dict[str, int],
        batch_seq: int,
        row_index: int,
        key: tuple[Any, ...],
    ) -> None:
        if len(numeric_values) != len(self.query.value_columns):
            raise EngineError(
                E_FIXED_OVERFLOW,
                "one or more fixed-point cells overflowed",
                batch_seq=batch_seq,
                row_index=row_index,
                group=list(key),
            )
        state.row_count += 1
        for column_name in self.query.value_columns:
            stat = state.stats.setdefault(column_name, StatState())
            stat.total_count += 1
            raw = numeric_values[column_name]
            if isinstance(raw, float) and math.isnan(raw):
                stat.nan_count += 1
                continue
            if isinstance(raw, float) and math.isinf(raw):
                if raw > 0:
                    stat.pos_inf_count += 1
                else:
                    stat.neg_inf_count += 1
                continue
            stat.finite_count += 1
            if self.mode == "fixed":
                if column_name not in fixed_values:
                    continue
                scaled = fixed_values[column_name]
                stat.total = int(stat.total) + scaled
                stat.square_total = int(stat.square_total) + scaled * scaled
                try:
                    ensure_fixed_bounds(
                        int(stat.total),
                        self.config.fixed_min,
                        self.config.fixed_max,
                        field="sum",
                        details={
                            "batch_seq": batch_seq,
                            "row_index": row_index,
                            "column": column_name,
                            "group": list(key),
                        },
                    )
                    ensure_fixed_bounds(
                        int(stat.square_total),
                        self.config.fixed_min,
                        self.config.fixed_max,
                        field="square_sum",
                        details={
                            "batch_seq": batch_seq,
                            "row_index": row_index,
                            "column": column_name,
                            "group": list(key),
                        },
                    )
                except EngineError as exc:
                    exc.details.update(
                        {
                            "column": column_name,
                            "batch_seq": batch_seq,
                            "row_index": row_index,
                            "group": list(key),
                            "fixed_int_bits": self.config.fixed_int_bits,
                        }
                    )
                    raise
            else:
                value = float(raw)
                stat.total = float(stat.total) + value
                stat.square_total = float(stat.square_total) + value * value

    def _append_window_value(self, state: GroupState, value: float) -> None:
        state.window_values.append(float(value))

    def _emit_ready_windows(
        self,
        state: GroupState,
        batch_seq: int,
        key: tuple[Any, ...],
    ) -> None:
        length = self.query.window.length
        stride = self.query.window.stride
        while len(state.window_values) >= length:
            values = state.window_values[:length]
            start = state.window_base
            try:
                payload = window_result_payload(
                    self.mode,
                    values,
                    start,
                    self.config.fixed_scale,
                    self.config.fixed_min,
                    self.config.fixed_max,
                )
            except EngineError as exc:
                exc.details.update(
                    {
                        "batch_seq": batch_seq,
                        "column": self.query.window.column,
                        "group": list(key),
                        "fixed_int_bits": self.config.fixed_int_bits,
                    }
                )
                raise
            payload = {
                "window_start": payload["window_start"],
                "window_end": payload["window_end"],
                "count": payload["count"],
                "finite_count": payload["finite_count"],
                "nan_count": payload["nan_count"],
                "pos_inf_count": payload["pos_inf_count"],
                "neg_inf_count": payload["neg_inf_count"],
                **{
                    op: payload[op]
                    for op in ("count", "sum", "mean", "variance")
                    if op in self.query.window.outputs
                },
            }
            corrupt = self._window_corrupt_target(key)
            forced = False
            if corrupt:
                expected = dict(payload)
                actual = dict(payload)
                sum_payload = dict(actual["sum"])
                old_value = sum_payload.get("value")
                new_value = (0.0 if old_value is None else float(old_value)) + 1.0
                sum_payload["value"] = new_value
                sum_payload["hex"] = float(new_value).hex()
                actual["sum"] = sum_payload
                self.pending_windows.append(
                    PendingWindowResult(
                        group_hash=state.group_hash,
                        group_key_json=canonical_dumps(list(key)),
                        version=state.version,
                        item_id=f"window:{start}",
                        window_start=start,
                        window_end=start + length,
                        payload=actual,
                        forced=True,
                    )
                )
                self._record_window_divergence(
                    key,
                    state.group_hash,
                    start,
                    expected,
                    actual,
                )
                raise EngineError(
                    E_WINDOW_MISMATCH,
                    "injected window result diverges from full recomputation",
                    group=list(key),
                    group_hash=state.group_hash,
                    window_start=start,
                    expected=expected,
                    actual=actual,
                    batch_seq=batch_seq,
                )
            else:
                self.pending_windows.append(
                    PendingWindowResult(
                        group_hash=state.group_hash,
                        group_key_json=canonical_dumps(list(key)),
                        version=state.version,
                        item_id=f"window:{start}",
                        window_start=start,
                        window_end=start + length,
                        payload=payload,
                    )
                )
            del state.window_values[:stride]
            state.window_base += stride

    def _window_corrupt_target(self, key: tuple[Any, ...]) -> bool:
        target_hash = group_hash(key)
        for injection in self.plan.manifest.matching(
            "window",
            self.batch_seq,
            kind="window_corrupt",
        ):
            if injection.group is not None and group_hash(injection.group) == target_hash:
                return True
        return False

    def _record_window_divergence(
        self,
        key: tuple[Any, ...],
        key_hash: str,
        window_start: int,
        expected: dict[str, Any],
        actual: dict[str, Any],
    ) -> None:
        return None

    def _refresh_aggregate_results(self, state: GroupState) -> None:
        rows: list[tuple[str, dict[str, Any], int]] = []
        for aggregate in self.query.aggregates:
            stat = state.stats.get(aggregate.column, StatState())
            payload = aggregate_result_payload(
                self.mode,
                aggregate.op,
                stat,
                state.row_count,
                self.config.fixed_scale,
            )
            rows.append((aggregate.name, payload, state.version))
        self.aggregate_results[state.group_hash] = rows

    def _enforce_memory(
        self,
        state: GroupState,
        key: tuple[Any, ...],
    ) -> None:
        size = state.estimated_bytes()
        if size > self.config.memory_budget_bytes:
            raise EngineError(
                E_STATE_TOO_LARGE,
                "a single group state cannot fit in the memory budget",
                group=list(key),
                group_hash=state.group_hash,
                group_state_bytes=size,
                memory_budget_bytes=self.config.memory_budget_bytes,
            )
        self.resident_bytes = sum(item.estimated_bytes() for item in self.cache.values())
        while self.resident_bytes > self.config.memory_budget_bytes:
            candidates = [
                item
                for item in self.cache.values()
                if item.group_hash != state.group_hash
            ]
            if not candidates:
                raise EngineError(
                    E_STATE_TOO_LARGE,
                    "a single group state cannot fit in the memory budget",
                    group=list(key),
                    group_hash=state.group_hash,
                    group_state_bytes=size,
                    memory_budget_bytes=self.config.memory_budget_bytes,
                )
            victim = min(candidates, key=lambda item: (item.last_touch, item.group_hash))
            self._spill_group(victim, tamper=self._spill_tamper_target(victim))
            self.resident_bytes = sum(item.estimated_bytes() for item in self.cache.values())
        self.store.set_meta("peak_resident_bytes", str(max(
            int(self.store.get_meta("peak_resident_bytes", "0") or 0),
            self.resident_bytes,
        )))

    def _cache_add(self, state: GroupState, key: tuple[Any, ...]) -> None:
        size = state.estimated_bytes()
        if size > self.config.memory_budget_bytes:
            raise EngineError(
                E_STATE_TOO_LARGE,
                "a single group state cannot fit in the memory budget",
                group=list(key),
                group_hash=state.group_hash,
                group_state_bytes=size,
                memory_budget_bytes=self.config.memory_budget_bytes,
            )
        self.cache[state.group_hash] = state
        self.resident_bytes += size
        while self.resident_bytes > self.config.memory_budget_bytes:
            candidates = [
                item
                for item in self.cache.values()
                if item.group_hash != state.group_hash
            ]
            if not candidates:
                raise EngineError(
                    E_STATE_TOO_LARGE,
                    "a single group state cannot fit in the memory budget",
                    group=list(key),
                    group_hash=state.group_hash,
                    group_state_bytes=size,
                    memory_budget_bytes=self.config.memory_budget_bytes,
                )
            victim = min(candidates, key=lambda item: (item.last_touch, item.group_hash))
            self._spill_group(victim, tamper=self._spill_tamper_target(victim))
            self.resident_bytes = sum(item.estimated_bytes() for item in self.cache.values())

    def _touch(self, state: GroupState) -> None:
        self.touch_counter += 1
        state.last_touch = self.touch_counter
        state.dirty = True
        self.touched.add(state.group_hash)

    def _spill_tamper_target(self, state: GroupState) -> bool:
        for injection in self.plan.manifest.matching(
            "write",
            self.batch_seq,
            kind="spill_tamper",
        ):
            if injection.group is None or group_hash(injection.group) == state.group_hash:
                return True
        return False

    def _spill_group(self, state: GroupState, *, tamper: bool) -> SpillRecord | None:
        state_json = canonical_dumps(state.as_payload(self.mode))
        try:
            record = self.spill.materialize(
                mode=self.mode,
                state=state,
                state_json=state_json,
                tamper=tamper,
            )
        except EngineError as exc:
            self._record_tampered_spill(state, exc)
            self._mark_group_failure(
                key=state.key,
                key_hash=state.group_hash,
                state=state,
                code=exc.code,
                phase="write",
                details=exc.details,
            )
            return None

        size = record.size_bytes
        cost = self.config.logical_clock_step * (1 + int(math.ceil(size / 4096)))
        start, end = self.store.advance_clock(cost)
        self.store.conn.execute(
            """
            INSERT INTO spill_files
                (spill_id, mode, group_hash, group_version, rel_path, sha256,
                 size_bytes, created_clock, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'materialized')
            ON CONFLICT(spill_id) DO UPDATE SET
                sha256 = excluded.sha256,
                size_bytes = excluded.size_bytes,
                status = 'materialized'
            """,
            (
                record.spill_id,
                self.mode,
                state.group_hash,
                state.version,
                record.rel_path,
                record.sha256,
                size,
                end,
            ),
        )
        self._record_spill_event(
            direction="spill",
            group_hash_value=state.group_hash,
            size_bytes=size,
            start=start,
            end=end,
            reused=record.reused,
        )
        self.spill_count += 1
        self.spilled_refs[state.group_hash] = record
        self.cache.pop(state.group_hash, None)
        self.resident_bytes = sum(item.estimated_bytes() for item in self.cache.values())
        return record

    def _record_tampered_spill(self, state: GroupState, exc: EngineError) -> None:
        spill_id = exc.details.get("spill_id")
        if not isinstance(spill_id, str):
            return
        self.store.conn.execute(
            """
            INSERT INTO spill_files
                (spill_id, mode, group_hash, group_version, rel_path, sha256,
                 size_bytes, created_clock, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'tampered')
            ON CONFLICT(spill_id) DO UPDATE SET status = 'tampered'
            """,
            (
                spill_id,
                self.mode,
                state.group_hash,
                state.version,
                str(exc.details.get("path", "")),
                str(exc.details.get("actual_digest", "")),
                0,
                self.batch_clock,
            ),
        )

    def _record_spill_event(
        self,
        *,
        direction: str,
        group_hash_value: str,
        size_bytes: int,
        start: int,
        end: int,
        reused: bool,
    ) -> None:
        self.store.conn.execute(
            """
            INSERT INTO spill_events
                (mode, batch_seq, group_hash, direction, bytes, start_clock,
                 end_clock, duration_ticks, reused)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.mode,
                self.batch_seq,
                group_hash_value,
                direction,
                size_bytes,
                start,
                end,
                end - start,
                1 if reused else 0,
            ),
        )

    def _mark_group_failure(
        self,
        *,
        key: tuple[Any, ...],
        key_hash: str,
        state: GroupState | None,
        code: str,
        phase: str,
        details: dict[str, Any],
    ) -> None:
        if key_hash in self.failed_groups:
            return
        self.failed_groups.add(key_hash)
        self.failure_count += 1
        self.cache.pop(key_hash, None)
        self.resident_bytes = sum(item.estimated_bytes() for item in self.cache.values())
        self.spilled_refs.pop(key_hash, None)
        self.aggregate_results.pop(key_hash, None)
        self.pending_windows = [
            item
            for item in self.pending_windows
            if item.group_hash != key_hash or item.forced
        ]
        payload = dict(details)
        payload.setdefault("batch_seq", self.batch_seq)
        payload.setdefault("group", list(key))
        payload.setdefault("column", details.get("column"))
        self.store.mark_group_failed(
            mode=self.mode,
            batch_seq=self.batch_seq,
            phase=phase,
            code=code,
            group_hash_value=key_hash,
            group_key_json=canonical_dumps(list(key)),
            details=payload,
            logical_time=self.batch_clock,
            column=(
                details.get("column")
                if details.get("column") is not None
                else (self.query.window.column if code == E_WINDOW_MISMATCH else None)
            ),
        )
        if state is not None:
            state.status = "failed"
            state.failed_code = code

    def _phase_for_error(self, code: str) -> str:
        if code in {E_SPILL_TAMPERED}:
            return "write"
        if code in {E_SPILL_READBACK_FAILED}:
            return "read"
        if code in {E_WINDOW_MISMATCH}:
            return "window"
        return "aggregate"

    def _apply_end_of_batch_injections(
        self,
        batch: Batch,
        outcome: BatchOutcome,
    ) -> None:
        for injection in self.plan.manifest.matching("write", batch.batch_seq):
            if injection.kind == "spill_tamper":
                target = self._injection_target_state(injection)
                if target is not None:
                    try:
                        self._spill_group(target, tamper=True)
                    except EngineError:
                        pass
            elif injection.kind == "crash_after_spill":
                if self._crash_already_fired(injection.injection_id):
                    continue
                target = self._injection_target_state(injection)
                if target is None and self.cache:
                    target = next(iter(self.cache.values()))
                if target is not None:
                    try:
                        self._spill_group(
                            target,
                            tamper=self._spill_tamper_target(target),
                        )
                    except EngineError:
                        pass
                self._mark_crash_fired(injection.injection_id)
                os._exit(86)
            elif injection.kind == "crash_before_commit":
                if self._crash_already_fired(injection.injection_id):
                    continue
                self._mark_crash_fired(injection.injection_id)
                os._exit(86)

    def _injection_target_state(self, injection: Injection) -> GroupState | None:
        if injection.group is None:
            return None
        target_hash = group_hash(injection.group)
        return self.cache.get(target_hash)

    def _crash_marker_path(self, injection_id: str) -> Path:
        return self.store.db_path.parent / "injection-markers" / f"{injection_id}.fired"

    def _crash_already_fired(self, injection_id: str) -> bool:
        return self._crash_marker_path(injection_id).exists()

    def _mark_crash_fired(self, injection_id: str) -> None:
        atomic_write_bytes(
            self._crash_marker_path(injection_id),
            b"fired\n",
        )

    def _commit_batch_state(self, batch: Batch, outcome: BatchOutcome) -> None:
        for state in sorted(self.cache.values(), key=lambda item: item.group_hash):
            if state.group_hash in self.failed_groups:
                continue
            state_json = canonical_dumps(state.as_payload(self.mode))
            state_digest = sha256_bytes(state_json.encode("utf-8"))
            self.store.conn.execute(
                """
                INSERT INTO group_states
                    (mode, group_hash, group_key_json, version, state_json,
                     state_digest, spill_id, status, last_touch)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 'active', ?)
                ON CONFLICT(mode, group_hash) DO UPDATE SET
                    group_key_json = excluded.group_key_json,
                    version = excluded.version,
                    state_json = excluded.state_json,
                    state_digest = excluded.state_digest,
                    spill_id = NULL,
                    status = 'active',
                    last_touch = excluded.last_touch
                """,
                (
                    self.mode,
                    state.group_hash,
                    canonical_dumps(list(state.key)),
                    state.version,
                    state_json,
                    state_digest,
                    state.last_touch,
                ),
            )
            self.store.conn.execute(
                """
                INSERT OR IGNORE INTO group_commits
                    (mode, batch_seq, group_hash, version)
                VALUES (?, ?, ?, ?)
                """,
                (self.mode, batch.batch_seq, state.group_hash, state.version),
            )
            outcome.groups_updated += 1

        for key_hash, record in sorted(self.spilled_refs.items()):
            if key_hash in self.failed_groups or key_hash in self.cache:
                continue
            state_payload_row = self.store.conn.execute(
                """
                SELECT version, group_key_json FROM group_states
                WHERE mode = ? AND group_hash = ?
                """,
                (self.mode, key_hash),
            ).fetchone()
            state_json = self._spill_state_json(record)
            if state_json is None:
                continue
            state_digest = sha256_bytes(state_json.encode("utf-8"))
            state_payload = strict_json_loads(state_json)
            version = int(state_payload["version"])
            key_json = canonical_dumps(state_payload["key"])
            self.store.conn.execute(
                """
                INSERT INTO group_states
                    (mode, group_hash, group_key_json, version, state_json,
                     state_digest, spill_id, status, last_touch)
                VALUES (?, ?, ?, ?, NULL, ?, ?, 'active', ?)
                ON CONFLICT(mode, group_hash) DO UPDATE SET
                    group_key_json = excluded.group_key_json,
                    version = excluded.version,
                    state_json = NULL,
                    state_digest = excluded.state_digest,
                    spill_id = excluded.spill_id,
                    status = 'active',
                    last_touch = excluded.last_touch
                """,
                (
                    self.mode,
                    key_hash,
                    key_json,
                    version,
                    state_digest,
                    record.spill_id,
                    int(state_payload["last_touch"]),
                ),
            )
            self.store.conn.execute(
                """
                INSERT OR IGNORE INTO group_commits
                    (mode, batch_seq, group_hash, version)
                VALUES (?, ?, ?, ?)
                """,
                (self.mode, batch.batch_seq, key_hash, version),
            )
            outcome.groups_updated += 1

        for key_hash, rows in sorted(self.aggregate_results.items()):
            if key_hash in self.failed_groups:
                continue
            state_row = self.store.conn.execute(
                """
                SELECT group_key_json FROM group_states
                WHERE mode = ? AND group_hash = ?
                """,
                (self.mode, key_hash),
            ).fetchone()
            if state_row is None:
                continue
            key_json = state_row["group_key_json"]
            for item_id, payload, version in rows:
                self.store.conn.execute(
                    """
                    INSERT INTO results
                        (mode, kind, group_hash, group_key_json, item_id, version,
                         window_start, window_end, value_json)
                    VALUES (?, 'aggregate', ?, ?, ?, ?, NULL, NULL, ?)
                    ON CONFLICT(mode, kind, group_hash, item_id) DO UPDATE SET
                        group_key_json = excluded.group_key_json,
                        version = excluded.version,
                        value_json = excluded.value_json
                    """,
                    (
                        self.mode,
                        key_hash,
                        key_json,
                        item_id,
                        version,
                        canonical_dumps(payload),
                    ),
                )

        for pending in sorted(
            self.pending_windows,
            key=lambda item: (item.group_hash, item.window_start, item.item_id),
        ):
            self.store.conn.execute(
                """
                INSERT INTO results
                    (mode, kind, group_hash, group_key_json, item_id, version,
                     window_start, window_end, value_json)
                VALUES (?, 'window', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mode, kind, group_hash, item_id) DO UPDATE SET
                    version = excluded.version,
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    value_json = excluded.value_json
                """,
                (
                    self.mode,
                    pending.group_hash,
                    pending.group_key_json,
                    pending.item_id,
                    pending.version,
                    pending.window_start,
                    pending.window_end,
                    canonical_dumps(pending.payload),
                ),
            )
            outcome.windows_emitted += 1

        new_digest = compute_state_digest(self.store.conn)
        self.store.set_meta("state_digest", new_digest)
        outcome.spills = self.spill_count
        outcome.readbacks = self.readback_count
        outcome.failures += self.failure_count

    def _spill_state_json(self, record: SpillRecord) -> str | None:
        try:
            if sha256_file(record.absolute_path) != record.sha256:
                return None
            payload = strict_json_loads(record.absolute_path.read_text(encoding="utf-8"))
            return canonical_dumps(payload["state_json"])
        except (OSError, KeyError, ValueError):
            return None

    def _mark_batch_failed(
        self,
        batch_seq: int,
        code: str,
        message: str,
        *,
        phase: str,
        details: dict[str, Any],
        clock: int,
        column: str | None = None,
    ) -> None:
        failure_details = dict(details)
        failure_details.update({"message": message, "batch_seq": batch_seq})
        self.store.record_failure(
            mode=self.mode,
            batch_seq=batch_seq,
            phase=phase,
            code=code,
            details=failure_details,
            logical_time=clock,
            column=column,
        )

    def _finish_batch_metadata(
        self,
        batch_seq: int,
        outcome: BatchOutcome,
        *,
        row_digest: str,
    ) -> None:
        current_watermark = int(self.store.get_meta("watermark", "0") or 0)
        self.store.set_meta("watermark", str(max(current_watermark, batch_seq)))
        status = outcome.status
        error_json = (
            canonical_dumps(outcome.error_details)
            if outcome.error_details is not None
            else None
        )
        self.store.conn.execute(
            """
            UPDATE batches
            SET status = ?, result_digest = ?, error_code = ?, error_json = ?,
                committed_clock = ?
            WHERE mode = ? AND batch_seq = ?
            """,
            (
                status,
                self.store.state_digest(),
                outcome.error_code,
                error_json,
                self.batch_clock,
                self.mode,
                batch_seq,
            ),
        )
