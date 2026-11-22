"""Thread-safe orchestrator for writes, incremental multiplication, and recovery."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .batches import WriteBatch, parse_write_batch
from .case import SampleCase
from .checkpoint import CheckpointStore
from .codes import (
    E_INCONSISTENT,
    E_RECOVERY_FAILED,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_WRITE_BUSY,
    E_WRITE_CONFLICT,
    E_WRITE_STALE,
    OK,
)
from .compute import (
    BlockComputer,
    block_ranges,
    build_block,
    dense_reference,
    deviation_report,
    initial_blocks,
    input_digest,
    make_computer,
    materialize_result,
    recompute_blocks,
    result_summary,
    row_states_from_base,
)
from .config import ComponentConfig
from .errors import BusinessError, raise_business
from .models import IncrementalEvent, Ledger, RowBlock, RowState, RuntimeState
from .state_codec import (
    decode_ledger_snapshot,
    decode_result_snapshot,
    encode_ledger_snapshot,
    encode_result_snapshot,
    make_checkpoint_bundle,
    verify_checkpoint_bundle,
)


@dataclass(frozen=True)
class ServiceView:
    case_id: str
    ledger: Ledger
    blocks: tuple[RowBlock, ...]
    row_states: tuple[RowState, ...]
    events: tuple[IncrementalEvent, ...]
    next_write_index: int
    ledger_snapshot: dict[str, Any]
    result_snapshot: dict[str, Any]
    recovery_info: dict[str, Any]


class SparseIncrementalService:
    """One atomic state machine per sparse case.

    Writers use a non-blocking exclusive lock. Readers briefly synchronize to
    take an immutable state reference and can then materialize outside the
    lock without observing a partially applied batch.
    """

    def __init__(
        self,
        case: SampleCase,
        config: ComponentConfig | None = None,
        *,
        checkpoint_dir: str | None = None,
        resume: bool = False,
    ) -> None:
        self.case = case
        self.config = config or ComponentConfig()
        self._computer: BlockComputer = make_computer(case)
        self._mutex = threading.RLock()
        self._admission_lock = threading.Lock()
        self._resident_batches = 0
        self._state: RuntimeState | None = None
        self._events: list[IncrementalEvent] = []
        self._next_write_index = 0
        self._checkpoint_store = (
            CheckpointStore(checkpoint_dir) if checkpoint_dir is not None else None
        )
        self._recovery_info: dict[str, Any] = {
            "resumed": False,
            "discarded_pending": False,
        }
        if resume and self._checkpoint_store is None:
            raise_business(
                E_RECOVERY_FAILED,
                case_id=case.case_id,
                segment="checkpoint",
                message="resume requires checkpoint_dir",
            )
        with self._mutex:
            if resume:
                self._resume_locked()
            else:
                self._initialize_locked()
                if self._checkpoint_store is not None:
                    self._save_checkpoint_locked()

    def _initialize_locked(self) -> None:
        row_states = row_states_from_base(self.case)
        blocks = initial_blocks(self._computer, row_states, self.config)
        ledger = Ledger(
            version=0,
            accepted=(),
            row_owners=tuple(state.owner_dict() for state in row_states),
        )
        ledger_snapshot = encode_ledger_snapshot(ledger)
        result_snapshot = encode_result_snapshot(0, blocks)
        self._state = RuntimeState(
            ledger=ledger,
            blocks=blocks,
            row_states=row_states,
            ledger_snapshot=ledger_snapshot,
            result_snapshot=result_snapshot,
        )
        self._events = []
        self._next_write_index = 0

    def _resume_locked(self) -> None:
        assert self._checkpoint_store is not None
        try:
            loaded = self._checkpoint_store.require_bundle()
        except BusinessError as exc:
            if exc.case_id is None:
                exc.case_id = self.case.case_id
            raise
        bundle = loaded.bundle
        assert bundle is not None
        verified = verify_checkpoint_bundle(
            bundle,
            case_id=self.case.case_id,
            operation=self.case.operation,
            base_checksum=self.case.base_checksum,
            config_checksum=self.config.checksum,
        )
        if int(verified["next_write_index"]) > len(self.case.write_batches):
            raise_business(
                E_RECOVERY_FAILED,
                case_id=self.case.case_id,
                segment="checkpoint.next_write_index",
                message="checkpoint points beyond the manifest write sequence",
                next_write_index=verified["next_write_index"],
                write_batch_count=len(self.case.write_batches),
            )
        self._install_bundle_locked(verified, enforce_rollback=False)
        assert self._state is not None
        self._recovery_info = {
            "resumed": True,
            "discarded_pending": loaded.discarded_pending,
            "pending_payload": loaded.pending_payload,
            "checkpoint_path": self._checkpoint_store.path_text,
            "loaded_version": self._state.ledger.version,
            "loaded_next_write_index": self._next_write_index,
            "loaded_ledger_snapshot_checksum": self._state.ledger_snapshot["checksum"],
            "loaded_result_snapshot_checksum": self._state.result_snapshot["checksum"],
            "committed_blocks_reused": len(self._state.blocks),
            "committed_intervals_reused": [
                block.interval_dict() for block in self._state.blocks
            ],
        }

    def _install_bundle_locked(self, bundle: dict[str, Any], *, enforce_rollback: bool) -> None:
        ledger, ledger_verified = decode_ledger_snapshot(
            bundle["ledger"],
            case_id=self.case.case_id,
        )
        blocks, result_verified = decode_result_snapshot(
            bundle["results"],
            case_id=self.case.case_id,
        )
        if ledger_verified.version != result_verified.version:
            self._snapshot_corrupt(
                "bundle",
                "ledger and result snapshot versions differ",
                snapshot_version=ledger_verified.version,
                ledger_version=ledger_verified.version,
                result_version=result_verified.version,
            )
        row_states = self._reconstruct_rows(ledger)
        self._validate_blocks(blocks, row_states, ledger.version)
        if enforce_rollback and self._state is not None:
            current = self._state
            current_version = current.ledger.version
            incoming_version = ledger.version
            if incoming_version < current_version:
                self._rollback(
                    current_version,
                    incoming_version,
                    current.ledger_snapshot.get("checksum"),
                    ledger_verified.checksum,
                )
            if incoming_version == current_version:
                same = (
                    current.ledger_snapshot.get("checksum") == ledger_verified.checksum
                    and current.result_snapshot.get("checksum") == result_verified.checksum
                )
                if not same:
                    self._rollback(
                        current_version,
                        incoming_version,
                        current.ledger_snapshot.get("checksum"),
                        ledger_verified.checksum,
                    )
                return
        events = tuple(
            IncrementalEvent.from_dict(item) for item in bundle.get("incremental_events", [])
        )
        next_write_index = int(bundle["next_write_index"])
        self._state = RuntimeState(
            ledger=ledger,
            blocks=blocks,
            row_states=row_states,
            ledger_snapshot=copy.deepcopy(bundle["ledger"]),
            result_snapshot=copy.deepcopy(bundle["results"]),
        )
        self._events = list(events)
        self._next_write_index = next_write_index

    def _reconstruct_rows(self, ledger: Ledger) -> tuple[RowState, ...]:
        rows = list(row_states_from_base(self.case))
        last_version = 0
        seen_batch_ids: set[str] = set()
        for expected_sequence, entry in enumerate(ledger.accepted, start=1):
            try:
                if int(entry["sequence"]) != expected_sequence:
                    self._snapshot_corrupt(
                        "write_ledger",
                        "accepted ledger sequence is not contiguous",
                        snapshot_version=ledger.version,
                    )
                if entry.get("status") != "accepted":
                    self._snapshot_corrupt(
                        "write_ledger",
                        "accepted ledger contains non-accepted entry",
                        snapshot_version=ledger.version,
                    )
                raw_batch = entry["batch"]
                batch = parse_write_batch(
                    raw_batch,
                    case_id=self.case.case_id,
                    rows=self.case.m,
                    cols=self.case.k,
                )
            except BusinessError as exc:
                self._snapshot_corrupt(
                    "write_ledger",
                    "accepted ledger batch cannot be reconstructed",
                    snapshot_version=ledger.version,
                    cause=exc.to_dict(),
                )
            if batch.write_version <= last_version:
                self._snapshot_corrupt(
                    "write_ledger",
                    "accepted write versions are not strictly increasing",
                    snapshot_version=ledger.version,
                )
            if batch.batch_id in seen_batch_ids:
                self._snapshot_corrupt(
                    "write_ledger",
                    "accepted ledger contains a duplicate batch_id",
                    snapshot_version=ledger.version,
                )
            seen_batch_ids.add(batch.batch_id)
            rows, _ = self._apply_batch_to_rows(tuple(rows), batch)
            last_version = batch.write_version
        if ledger.version != last_version:
            self._snapshot_corrupt(
                "write_ledger",
                "ledger version does not equal the last accepted write version",
                snapshot_version=ledger.version,
                ledger_version=ledger.version,
                last_write_version=last_version,
            )
        owners = tuple(state.owner_dict() for state in rows)
        if list(owners) != list(ledger.row_owners):
            self._snapshot_corrupt(
                "write_ledger",
                "ledger row owners do not match replayed write batches",
                snapshot_version=ledger.version,
            )
        return tuple(rows)

    def _validate_blocks(
        self,
        blocks: tuple[RowBlock, ...],
        row_states: tuple[RowState, ...],
        version: int,
    ) -> None:
        expected = block_ranges(self.case.m, self.config.row_block_size)
        if len(blocks) != len(expected):
            self._snapshot_corrupt(
                "row_results",
                "row block count does not match row_block_size",
                snapshot_version=version,
                expected_blocks=len(expected),
                actual_blocks=len(blocks),
            )
        for block, (row_start, row_end, block_id) in zip(blocks, expected):
            if (
                block.block_id != block_id
                or block.row_start != row_start
                or block.row_end != row_end
            ):
                self._snapshot_corrupt(
                    "row_results",
                    "row block partition does not match configuration",
                    snapshot_version=version,
                    block_id=block.block_id,
                )
            if block.result.shape != (row_end - row_start, self.case.n):
                self._snapshot_corrupt(
                    "row_results",
                    "row block result shape is invalid",
                    snapshot_version=version,
                    block_id=block.block_id,
                )
            expected_last = max(
                (state.version for state in row_states[row_start:row_end]),
                default=0,
            )
            if block.last_write_version != expected_last:
                self._snapshot_corrupt(
                    "row_results",
                    "row block write version is inconsistent with the ledger",
                    snapshot_version=version,
                    block_id=block.block_id,
                )
            expected_input = input_digest(self.case, row_states, row_start, row_end)
            if block.input_digest != expected_input:
                self._snapshot_corrupt(
                    "row_results",
                    "row block input digest is inconsistent with the ledger",
                    snapshot_version=version,
                    block_id=block.block_id,
                )

    def submit_batch(
        self,
        raw: Any,
        *,
        batch_index: int | None = None,
    ) -> dict[str, Any]:
        with self._admission_lock:
            if self._resident_batches >= self.config.max_resident_batches:
                raise_business(
                    E_RESOURCE_LIMIT,
                    case_id=self.case.case_id,
                    segment="resident_batches",
                    message="simultaneously resident write batches exceed max_resident_batches",
                    resident_batches=self._resident_batches,
                    max_resident_batches=self.config.max_resident_batches,
                )
            self._resident_batches += 1
        acquired = False
        try:
            batch = parse_write_batch(
                raw,
                case_id=self.case.case_id,
                rows=self.case.m,
                cols=self.case.k,
            )
            acquired = self._mutex.acquire(blocking=False)
            if not acquired:
                raise_business(
                    E_WRITE_BUSY,
                    case_id=self.case.case_id,
                    segment="write_lock",
                    message="another write batch is currently applying",
                    batch_id=batch.batch_id,
                    write_version=batch.write_version,
                )
            assert self._state is not None
            if batch.write_version <= self._state.ledger.version:
                raise_business(
                    E_WRITE_STALE,
                    case_id=self.case.case_id,
                    segment="write_version",
                    message="write_version must be strictly greater than the accepted ledger version",
                    batch_id=batch.batch_id,
                    write_version=batch.write_version,
                    latest_version=self._state.ledger.version,
                )
            if batch.batch_id in self._state.ledger.accepted_ids():
                raise_business(
                    E_WRITE_CONFLICT,
                    case_id=self.case.case_id,
                    segment="batch_id",
                    message="batch_id has already been accepted",
                    batch_id=batch.batch_id,
                    write_version=batch.write_version,
                )
            return self._commit_locked(batch, batch_index)
        finally:
            if acquired:
                self._mutex.release()
            with self._admission_lock:
                self._resident_batches -= 1

    def _commit_locked(self, batch: WriteBatch, batch_index: int | None) -> dict[str, Any]:
        assert self._state is not None
        old_state = self._state
        new_rows, overwritten_versions = self._apply_batch_to_rows(old_state.row_states, batch)
        new_blocks, event = recompute_blocks(
            computer=self._computer,
            row_states=new_rows,
            previous_blocks=old_state.blocks,
            row_start=batch.row_start,
            row_end=batch.row_end,
            write_version=batch.write_version,
            batch_id=batch.batch_id,
            config=self.config,
            case_id=self.case.case_id,
        )
        affected_block_ids = [item["block_id"] for item in event.recompute_intervals]
        accepted_entry = {
            "sequence": len(old_state.ledger.accepted) + 1,
            "status": "accepted",
            "batch": batch.canonical_dict(),
            "affected_block_ids": affected_block_ids,
            "overwrote_versions": overwritten_versions,
        }
        new_ledger = Ledger(
            version=batch.write_version,
            accepted=old_state.ledger.accepted + (accepted_entry,),
            row_owners=tuple(state.owner_dict() for state in new_rows),
        )
        new_ledger_snapshot = encode_ledger_snapshot(new_ledger)
        new_result_snapshot = encode_result_snapshot(batch.write_version, new_blocks)
        new_state = RuntimeState(
            ledger=new_ledger,
            blocks=new_blocks,
            row_states=new_rows,
            ledger_snapshot=new_ledger_snapshot,
            result_snapshot=new_result_snapshot,
        )
        new_events = self._events + [event]
        if batch_index is None:
            new_next_index = self._next_write_index + 1
        else:
            if isinstance(batch_index, bool) or not isinstance(batch_index, int) or batch_index < 0:
                raise_business(
                    E_WRITE_CONFLICT,
                    case_id=self.case.case_id,
                    segment="batch_index",
                    message="batch_index must be a non-negative integer",
                    batch_id=batch.batch_id,
                )
            new_next_index = max(self._next_write_index, batch_index + 1)

        if self._checkpoint_store is not None:
            bundle = self._make_bundle(
                new_state,
                new_events,
                new_next_index,
            )
            self._checkpoint_store.save(bundle)

        self._state = new_state
        self._events = new_events
        self._next_write_index = new_next_index
        return {
            "business_code": OK,
            "case_id": self.case.case_id,
            "batch_id": batch.batch_id,
            "write_version": batch.write_version,
            "ledger_version": new_ledger.version,
            "ledger_checksum": new_ledger_snapshot["checksum"],
            "result_checksum": new_result_snapshot["checksum"],
            "degraded": bool(event.degradation_categories),
            "degradation_categories": list(event.degradation_categories),
            "event": event.to_dict(),
        }

    def _apply_batch_to_rows(
        self,
        row_states: tuple[RowState, ...],
        batch: WriteBatch,
    ) -> tuple[tuple[RowState, ...], list[int]]:
        rows = list(row_states)
        overwritten = {
            rows[row].version for row in range(batch.row_start, batch.row_end)
        }
        if batch.mode == "replace":
            for row_write in batch.rows:
                rows[row_write.row] = RowState.replaced(
                    row=row_write.row,
                    version=batch.write_version,
                    col_indices=row_write.col_indices,
                    values=row_write.values,
                )
        else:
            for row in range(batch.row_start, batch.row_end):
                rows[row] = RowState.invalidated(row, batch.write_version)
        return tuple(rows), sorted(overwritten)

    def snapshot_view(self) -> ServiceView:
        with self._mutex:
            assert self._state is not None
            state = self._state
            return ServiceView(
                case_id=self.case.case_id,
                ledger=copy.deepcopy(state.ledger),
                blocks=state.blocks,
                row_states=state.row_states,
                events=tuple(self._events),
                next_write_index=self._next_write_index,
                ledger_snapshot=copy.deepcopy(state.ledger_snapshot),
                result_snapshot=copy.deepcopy(state.result_snapshot),
                recovery_info=copy.deepcopy(self._recovery_info),
            )

    @property
    def ledger(self) -> Ledger:
        return self.snapshot_view().ledger

    @property
    def events(self) -> tuple[IncrementalEvent, ...]:
        return self.snapshot_view().events

    @property
    def next_write_index(self) -> int:
        return self.snapshot_view().next_write_index

    @property
    def recovery_info(self) -> dict[str, Any]:
        return self.snapshot_view().recovery_info

    def materialize(self) -> np.ndarray:
        view = self.snapshot_view()
        return materialize_result(view.blocks, self.case.m, self.case.n)

    def export_bundle(self) -> dict[str, Any]:
        with self._mutex:
            assert self._state is not None
            return copy.deepcopy(
                self._make_bundle(self._state, self._events, self._next_write_index)
            )

    def checkpoint_checksum(self) -> str:
        return str(self.export_bundle()["bundle_checksum"])

    def import_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        verified = verify_checkpoint_bundle(
            bundle,
            case_id=self.case.case_id,
            operation=self.case.operation,
            base_checksum=self.case.base_checksum,
            config_checksum=self.config.checksum,
        )
        with self._mutex:
            self._install_bundle_locked(verified, enforce_rollback=True)
            return {
                "business_code": OK,
                "case_id": self.case.case_id,
                "ledger_version": self._state.ledger.version if self._state else None,
                "bundle_checksum": verified["bundle_checksum"],
            }

    def _make_bundle(
        self,
        state: RuntimeState,
        events: Iterable[IncrementalEvent],
        next_write_index: int,
    ) -> dict[str, Any]:
        return self._make_bundle_parts(
            state.ledger_snapshot,
            state.result_snapshot,
            events,
            next_write_index,
        )

    def _make_bundle_parts(
        self,
        ledger_snapshot: dict[str, Any],
        result_snapshot: dict[str, Any],
        events: Iterable[IncrementalEvent],
        next_write_index: int,
    ) -> dict[str, Any]:
        return make_checkpoint_bundle(
            case_id=self.case.case_id,
            operation=self.case.operation,
            base_checksum=self.case.base_checksum,
            config_checksum=self.config.checksum,
            next_write_index=next_write_index,
            ledger_snapshot=ledger_snapshot,
            result_snapshot=result_snapshot,
            incremental_events=[event.to_dict() for event in events],
        )

    def _save_checkpoint_locked(self) -> None:
        assert self._checkpoint_store is not None
        assert self._state is not None
        self._checkpoint_store.save(
            self._make_bundle(self._state, self._events, self._next_write_index)
        )

    def self_check(self) -> dict[str, Any]:
        view = self.snapshot_view()
        blocks = view.blocks
        row_states = view.row_states
        self._check_final_partition(blocks)
        for event in view.events:
            intervals = [
                (int(item["row_start"]), int(item["row_end"]))
                for item in event.recompute_intervals + event.reuse_intervals
            ]
            self._check_cover(intervals, context=f"event_{event.write_version}")

        recomputed_matches = 0
        for block in blocks:
            rebuilt = build_block(
                self._computer,
                row_states,
                block.row_start,
                block.row_end,
                block.block_id,
            )
            if (
                rebuilt.input_digest != block.input_digest
                or rebuilt.digest != block.digest
                or not np.array_equal(rebuilt.result, block.result)
            ):
                self._inconsistent(
                    category="block_recompute_mismatch",
                    interval=block.interval,
                    block_id=block.block_id,
                )
            recomputed_matches += 1

        base_rows = row_states_from_base(self.case)
        untouched_blocks = 0
        for block in blocks:
            if block.last_write_version != 0:
                continue
            baseline = build_block(
                self._computer,
                base_rows,
                block.row_start,
                block.row_end,
                block.block_id,
            )
            if baseline.digest != block.digest or not np.array_equal(baseline.result, block.result):
                self._inconsistent(
                    category="untouched_digest_changed",
                    interval=block.interval,
                    block_id=block.block_id,
                )
            untouched_blocks += 1

        try:
            replayed_rows = self._reconstruct_rows(view.ledger)
        except BusinessError:
            self._inconsistent(
                category="ledger_mismatch",
                interval=(0, self.case.m),
            )
        if replayed_rows != row_states:
            self._inconsistent(
                category="ledger_mismatch",
                interval=(0, self.case.m),
            )

        result = materialize_result(blocks, self.case.m, self.case.n)
        reference = dense_reference(self.case, row_states)
        deviation = deviation_report(result, reference, self.config)
        if not deviation["within_tolerance"]:
            self._inconsistent(
                category="reference_mismatch",
                interval=(0, self.case.m),
                deviation=deviation,
            )
        touched_intervals = [
            block.interval_dict() for block in blocks if block.last_write_version > 0
        ]
        untouched_intervals = [
            block.interval_dict() for block in blocks if block.last_write_version == 0
        ]
        return {
            "business_code": OK,
            "case_id": self.case.case_id,
            "intervals": {
                "disjoint": True,
                "coverage_complete": True,
                "block_count": len(blocks),
                "touched_by_writes": touched_intervals,
                "never_touched_by_writes": untouched_intervals,
            },
            "independent_recompute": {
                "blocks_checked": recomputed_matches,
                "all_results_match": True,
            },
            "untouched_checksums": {
                "blocks_checked": untouched_blocks,
                "byte_identical": True,
            },
            "ledger": {
                "accepted_entries": len(view.ledger.accepted),
                "row_owners_checked": len(view.ledger.row_owners),
                "consistent": True,
            },
            "deviation": deviation,
        }

    def _check_final_partition(self, blocks: tuple[RowBlock, ...]) -> None:
        expected_start = 0
        for block in blocks:
            if block.row_start != expected_start:
                self._inconsistent(
                    category="interval_coverage",
                    interval=(expected_start, block.row_start),
                )
            if block.row_end <= block.row_start:
                self._inconsistent(category="interval_overlap", interval=block.interval)
            expected_start = block.row_end
        if expected_start != self.case.m:
            self._inconsistent(
                category="interval_coverage",
                interval=(expected_start, self.case.m),
            )

    def _check_cover(self, intervals: list[tuple[int, int]], *, context: str) -> None:
        ordered = sorted(intervals)
        expected = 0
        for start, end in ordered:
            if start < expected:
                self._inconsistent(
                    category="interval_overlap",
                    interval=(start, min(end, expected)),
                    context=context,
                )
            if start > expected:
                self._inconsistent(
                    category="interval_coverage",
                    interval=(expected, start),
                    context=context,
                )
            expected = end
        if expected != self.case.m:
            self._inconsistent(
                category="interval_coverage",
                interval=(expected, self.case.m),
                context=context,
            )

    def _inconsistent(
        self,
        *,
        category: str,
        interval: tuple[int, int],
        **details: Any,
    ) -> None:
        raise_business(
            E_INCONSISTENT,
            case_id=self.case.case_id,
            segment="incremental_consistency",
            message="incremental consistency self-check failed",
            interval={"row_start": interval[0], "row_end": interval[1]},
            inconsistency_category=category,
            **details,
        )

    def _snapshot_corrupt(
        self,
        category: str,
        message: str,
        *,
        snapshot_version: int | None = None,
        **details: Any,
    ) -> None:
        raise_business(
            E_SNAPSHOT_CORRUPT,
            case_id=self.case.case_id,
            segment=f"snapshot.{category}",
            message=message,
            snapshot_category=category,
            snapshot_version=(
                snapshot_version
                if snapshot_version is not None
                else (self._state.ledger.version if self._state else None)
            ),
            **details,
        )

    def _rollback(
        self,
        current_version: int,
        incoming_version: int,
        current_checksum: str | None,
        incoming_checksum: str | None,
    ) -> None:
        raise_business(
            E_SNAPSHOT_ROLLBACK,
            case_id=self.case.case_id,
            segment="snapshot.version",
            message="incoming snapshot cannot replace a same-or-newer committed snapshot",
            snapshot_category="bundle",
            current_version=current_version,
            incoming_version=incoming_version,
            current_checksum=current_checksum,
            incoming_checksum=incoming_checksum,
        )

    def report(self) -> dict[str, Any]:
        view = self.snapshot_view()
        result = materialize_result(view.blocks, self.case.m, self.case.n)
        reference = dense_reference(self.case, view.row_states)
        deviation = deviation_report(result, reference, self.config)
        events = [event.to_dict() for event in view.events]
        categories = sorted(
            {
                category
                for event in view.events
                for category in event.degradation_categories
            }
        )
        final_recompute = blocks_where(view.blocks, lambda block: block.last_write_version > 0)
        final_reuse = blocks_where(view.blocks, lambda block: block.last_write_version == 0)
        bundle_checksum = self._make_bundle_parts(
            view.ledger_snapshot,
            view.result_snapshot,
            view.events,
            view.next_write_index,
        )["bundle_checksum"]
        return {
            "business_code": OK,
            "case_id": self.case.case_id,
            "operation": self.case.operation,
            "case": self.case.public_dict(),
            "config": self.config.public_dict(),
            "degraded": bool(categories),
            "degradation_categories": categories,
            "result_summary": result_summary(result),
            "deviation": deviation,
            "write_ledger": {
                "version": view.ledger.version,
                "checksum": view.ledger.checksum,
                "snapshot_checksum": view.ledger_snapshot["checksum"],
                "entries": list(view.ledger.accepted),
            },
            "incremental": {
                "events": events,
                "final_recomputed_intervals": final_recompute,
                "final_reused_intervals": final_reuse,
                "trigger_write_versions": [event.write_version for event in view.events],
            },
            "snapshot": {
                "write_ledger": {
                    "version": view.ledger_snapshot["version"],
                    "checksum": view.ledger_snapshot["checksum"],
                },
                "row_results": {
                    "version": view.result_snapshot["version"],
                    "checksum": view.result_snapshot["checksum"],
                },
                "checkpoint_bundle_checksum": bundle_checksum,
            },
            "recovery": view.recovery_info,
            "next_write_index": view.next_write_index,
        }


def blocks_where(
    blocks: tuple[RowBlock, ...],
    predicate: Any,
) -> list[dict[str, Any]]:
    return [block.interval_dict() for block in blocks if predicate(block)]
