from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from .canonical import (
    ZERO_HASH,
    page_for_key,
    row_digest,
    sha256_json,
    summarize_tables,
)
from .checkpoint_io import (
    checkpoint_from_v2,
    read_v2_package,
    write_v2_package,
)
from .errors import (
    apply_failed,
    bad_checkpoint,
    chain_break,
    clock_rollback,
    half_applied,
    replay_generation_error,
    state_error,
    summary_mismatch,
    version_rollback,
    WalRecError,
)
from .models import Policy, Sample, TableRow, WalRecord
from .sample import parse_sample
from .store import Store, compare_row_snapshots

FAILPOINT_ENV = "WALREC_FAILPOINT"
FAIL_TXID_ENV = "WALREC_FAIL_TXID"


def _maybe_failpoint(stage: str, txid: int) -> None:
    if os.environ.get(FAILPOINT_ENV) != stage:
        return
    raw_txid = os.environ.get(FAIL_TXID_ENV)
    if raw_txid is not None and int(raw_txid) != txid:
        return
    os._exit(91)


def _logical_failure(txid: int, sample: Sample) -> None:
    if txid in sample.policy.inject_failure_tx_ids:
        raise apply_failed(
            "injected post-start transaction failure",
            txid=txid,
        )


def _apply_pure_row(
    state: dict[tuple[str, str], dict[str, Any]],
    record: WalRecord,
) -> None:
    key = (record.table, record.row_key)
    current = state.get(key)
    if record.operation == "INSERT":
        if current is not None and not current["tombstone"]:
            raise chain_break(
                "seed replay INSERT conflicts with a live row",
                txid=record.logical_tx_id,
                table=record.table,
                row_key=record.row_key,
            )
        version = 1 if current is None else int(current["version"]) + 1
        payload = record.after
        tombstone = False
    elif record.operation == "UPDATE":
        if current is None or current["tombstone"] or current["payload"] != record.before:
            raise chain_break(
                "seed replay UPDATE before image does not match",
                txid=record.logical_tx_id,
                table=record.table,
                row_key=record.row_key,
            )
        version = int(current["version"]) + 1
        payload = record.after
        tombstone = False
    else:
        if current is None or current["tombstone"] or current["payload"] != record.before:
            raise chain_break(
                "seed replay DELETE before image does not match",
                txid=record.logical_tx_id,
                table=record.table,
                row_key=record.row_key,
            )
        version = int(current["version"]) + 1
        payload = None
        tombstone = True
    state[key] = {
        "key": record.row_key,
        "payload": payload,
        "row_hash": row_digest(
            record.table,
            record.row_key,
            payload,
            version,
            record.logical_tx_id,
            tombstone,
        ),
        "source_tx": record.logical_tx_id,
        "table": record.table,
        "tombstone": tombstone,
        "version": version,
    }


def replay_rows_to_watermark(
    records: Sequence[WalRecord],
    watermark: int,
) -> dict[str, list[TableRow]]:
    state: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record.logical_tx_id > watermark:
            break
        _apply_pure_row(state, record)
    grouped: dict[str, list[TableRow]] = {}
    for (table, key), row in sorted(state.items()):
        grouped.setdefault(table, []).append(
            TableRow(
                table=table,
                key=key,
                payload=row["payload"],
                version=int(row["version"]),
                source_tx=int(row["source_tx"]),
                tombstone=bool(row["tombstone"]),
                row_hash=str(row["row_hash"]),
            )
        )
    return grouped


def expected_rows_for_sample(
    sample: Sample,
    watermark: int,
) -> dict[str, list[dict[str, Any]]] | None:
    state: dict[tuple[str, str], dict[str, Any]] = {}
    start_at = 0
    if sample.wal[0].prev_tx_id != 0:
        anchors = [
            checkpoint
            for checkpoint in sample.checkpoints.by_gen.values()
            if checkpoint.format_version == "v2"
            and checkpoint.applied_watermark == sample.wal[0].prev_tx_id
        ]
        if not anchors:
            return None
        anchor = max(anchors, key=lambda checkpoint: checkpoint.checkpoint_gen)
        start_at = anchor.applied_watermark
        for table, table_rows in anchor.rows_by_table.items():
            for row in table_rows:
                state[(table, row.key)] = {
                    "key": row.key,
                    "payload": row.payload,
                    "row_hash": row.row_hash,
                    "source_tx": row.source_tx,
                    "table": table,
                    "tombstone": row.tombstone,
                    "version": row.version,
                }
    for record in sample.wal:
        if record.logical_tx_id <= start_at:
            continue
        if record.logical_tx_id > watermark:
            break
        _apply_pure_row(state, record)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for (_, _), row in sorted(state.items()):
        grouped.setdefault(str(row["table"]), []).append(
            {
                "key": row["key"],
                "payload": row["payload"],
                "row_hash": row["row_hash"],
                "source_tx": int(row["source_tx"]),
                "tombstone": bool(row["tombstone"]),
                "version": int(row["version"]),
            }
        )
    return grouped


class Engine:
    def __init__(self, db_path: str | Path) -> None:
        self.store = Store(db_path)
        self._process_lock = threading.Lock()

    @property
    def db_path(self) -> Path:
        return self.store.path

    @staticmethod
    def _pending_ids(sample: Sample, applied: Sequence[int]) -> list[int]:
        applied_set = set(applied)
        return [
            record.logical_tx_id
            for record in sample.wal
            if record.logical_tx_id not in applied_set
        ]

    def _load_sample(
        self,
        sample_dir: str | Path,
        policy_overrides: Mapping[str, Any] | None = None,
    ) -> Sample:
        return parse_sample(sample_dir, policy_overrides=policy_overrides)

    def load(
        self,
        sample_dir: str | Path,
        *,
        checkpoint_gen: int | None = None,
        reset: bool = False,
        clock_start_ms: int | None = None,
    ) -> dict[str, Any]:
        sample = self._load_sample(sample_dir)
        if checkpoint_gen is None:
            checkpoint = sample.checkpoints.current
        else:
            try:
                checkpoint = sample.checkpoints.by_gen[checkpoint_gen]
            except KeyError as exc:
                raise bad_checkpoint(
                    "requested checkpoint generation does not exist",
                    checkpoint_gen=checkpoint_gen,
                ) from exc
        selected_clock = (
            checkpoint.logical_clock_ms if clock_start_ms is None else clock_start_ms
        )
        if selected_clock < checkpoint.logical_clock_ms - sample.policy.clock_tolerance_ms:
            raise clock_rollback(
                "load clock precedes the checkpoint logical clock",
                checkpoint_clock_ms=checkpoint.logical_clock_ms,
                requested_clock_ms=selected_clock,
                tolerance_ms=sample.policy.clock_tolerance_ms,
            )

        if checkpoint.format_version == "v2":
            rows_by_table = checkpoint.rows_by_table
        else:
            rows_by_table = replay_rows_to_watermark(
                sample.wal,
                checkpoint.applied_watermark,
            )
            for table, summary in checkpoint.table_summaries.items():
                live = sum(
                    1
                    for row in rows_by_table.get(table, [])
                    if not row.tombstone
                )
                expected = int(summary.get("live_row_count", 0))
                if live != expected:
                    raise summary_mismatch(
                        "v1 table row count does not match replayed WAL",
                        table=table,
                        expected=expected,
                        actual=live,
                    )
            for table, rows in rows_by_table.items():
                live = sum(1 for row in rows if not row.tombstone)
                expected = int(
                    checkpoint.table_summaries.get(table, {}).get("live_row_count", 0)
                )
                if live != expected:
                    raise summary_mismatch(
                        "v1 contains a table absent from replayed WAL counts",
                        table=table,
                        expected=expected,
                        actual=live,
                    )

        if self.store.exists() and not reset:
            self.store.require_initialized()
            with self.store.connection(writable=False) as connection:
                pointer = self.store.get_pointer(connection)
            if pointer is not None:
                raise state_error(
                    "state database already loaded; pass reset=True to replace it"
                )
        self.store.initialize(reset=reset)
        self.store.seed_state(
            checkpoint,
            sample.wal,
            rows_by_table,
            installed_at_ms=selected_clock,
        )
        if selected_clock != checkpoint.logical_clock_ms:
            with self.store.transaction() as connection:
                self.store.set_meta(
                    connection,
                    "logical_clock_ms",
                    str(selected_clock),
                )
        pending = self._pending_ids(sample, self.store.applied_txids())
        return {
            "checkpoint_gen": checkpoint.checkpoint_gen,
            "loaded": True,
            "ok": True,
            "pending_tx_ids": pending,
            "reset": reset,
            "status": self.store.status(pending_tx_ids=pending),
        }

    def _check_clock(
        self,
        sample: Sample,
        requested: int | None,
        *,
        persisted: int,
    ) -> int:
        if requested is None:
            return persisted
        if requested < persisted - sample.policy.clock_tolerance_ms:
            raise clock_rollback(
                "logical clock moved backwards beyond tolerance",
                persisted_clock_ms=persisted,
                requested_clock_ms=requested,
                tolerance_ms=sample.policy.clock_tolerance_ms,
            )
        return max(requested, persisted)

    def _recover_transaction(
        self,
        sample: Sample,
        *,
        requested_clock_ms: int | None,
    ) -> dict[str, Any]:
        wal_by_id = sample.tx_by_id
        with self._process_lock, self.store.lock, self.store.transaction() as connection:
            persisted_clock = self.store.get_meta_int(connection, "logical_clock_ms")
            clock = self._check_clock(
                sample,
                requested_clock_ms,
                persisted=persisted_clock,
            )
            old_watermark = self.store.get_meta_int(connection, "applied_watermark")
            old_in_flight = [
                int(row["txid"])
                for row in connection.execute(
                    "SELECT txid FROM in_flight ORDER BY txid"
                ).fetchall()
            ]
            orphan_rows = connection.execute(
                """
                SELECT DISTINCT source_tx
                FROM data_rows
                WHERE source_tx NOT IN (SELECT txid FROM applied_txs)
                ORDER BY source_tx
                """
            ).fetchall()
            repaired_rows: list[int] = []
            for row in orphan_rows:
                txid = int(row["source_tx"])
                record = wal_by_id.get(txid)
                if record is None:
                    raise half_applied(
                        "orphan data row has no WAL transaction",
                        txid=txid,
                    )
                prior_tables = expected_rows_for_sample(sample, txid - 1)
                if prior_tables is None:
                    raise half_applied(
                        "cannot reconstruct the pre-transaction state",
                        txid=txid,
                    )
                prior = next(
                    (
                        candidate
                        for candidate in prior_tables.get(record.table, [])
                        if candidate["key"] == record.row_key
                    ),
                    None,
                )
                if prior is None:
                    connection.execute(
                        "DELETE FROM data_rows WHERE logical_table = ? AND row_key = ?",
                        (record.table, record.row_key),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE data_rows
                        SET payload_json = ?, version = ?, source_tx = ?,
                            tombstone = ?, row_hash = ?
                        WHERE logical_table = ? AND row_key = ?
                        """,
                        (
                            json.dumps(
                                prior["payload"],
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            int(prior["version"]),
                            int(prior["source_tx"]),
                            int(bool(prior["tombstone"])),
                            str(prior["row_hash"]),
                            record.table,
                            record.row_key,
                        ),
                    )
                repaired_rows.append(txid)
            connection.execute("DELETE FROM in_flight")
            old_watermark, new_watermark = self.store.repair_watermark(connection)
            summaries = self.store.summaries_snapshot(connection)
            self.store.set_meta_json(connection, "state_table_summaries", summaries)
            self.store.set_meta(connection, "state_rows_digest", sha256_json(summaries))
            self.store.set_meta(connection, "logical_clock_ms", str(clock))
            return {
                "clock_ms": clock,
                "in_flight_cleared": old_in_flight,
                "repaired_half_applied_tx_ids": sorted(set(repaired_rows)),
                "watermark_before": old_watermark,
                "watermark_after": new_watermark,
            }

    def recover(
        self,
        sample_dir: str | Path,
        *,
        clock_ms: int | None = None,
    ) -> dict[str, Any]:
        sample = self._load_sample(sample_dir)
        self.store.require_initialized()
        recovery = self._recover_transaction(sample, requested_clock_ms=clock_ms)
        applied = self.store.applied_txids()
        pending = self._pending_ids(sample, applied)
        return {
            "ok": True,
            "recovery": recovery,
            "status": self.store.status(pending_tx_ids=pending),
        }

    def resume(
        self,
        sample_dir: str | Path,
        *,
        clock_ms: int | None = None,
        max_rounds: int | None = None,
        no_auto_checkpoint: bool = False,
        policy_overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        sample = self._load_sample(sample_dir, policy_overrides)
        self.store.require_initialized()
        recovery = self._recover_transaction(sample, requested_clock_ms=clock_ms)
        applied_result = self._apply_sample(
            sample,
            clock_ms=recovery["clock_ms"],
            max_rounds=max_rounds,
            no_auto_checkpoint=no_auto_checkpoint,
            already_recovered=True,
        )
        applied_result["recovery"] = recovery
        applied_result["resumed"] = True
        return applied_result

    def _apply_one(
        self,
        sample: Sample,
        record: WalRecord,
        *,
        round_id: str,
        sequence: int,
        started_at_ms: int,
    ) -> dict[str, Any]:
        completed_at_ms = started_at_ms + record.duration_ms
        lease_id = f"{record.logical_tx_id}-{record.tx_hash[:16]}"
        with self._process_lock, self.store.lock:
            try:
                lease = self.store.register_in_flight(
                    record,
                    started_at_ms=started_at_ms,
                    lease_id=lease_id,
                )
                if not lease:
                    return {
                        "completed_at_ms": completed_at_ms,
                        "reason": "transaction_already_committed",
                        "seq": sequence,
                        "started_at_ms": started_at_ms,
                        "status": "duplicate",
                        "txid": record.logical_tx_id,
                    }
                _maybe_failpoint("after_inflight_before_data", record.logical_tx_id)
                _logical_failure(record.logical_tx_id, sample)
                with self.store.transaction() as connection:
                    result = self.store.apply_record(
                        record,
                        applied_at_ms=completed_at_ms,
                        connection=connection,
                    )
                    _maybe_failpoint("after_data_before_commit", record.logical_tx_id)
                _maybe_failpoint("after_commit", record.logical_tx_id)
                return {
                    "completed_at_ms": completed_at_ms,
                    "reason": "",
                    "seq": sequence,
                    "started_at_ms": started_at_ms,
                    "status": result["status"],
                    "txid": record.logical_tx_id,
                }
            except WalRecError as exc:
                self.store.cleanup_in_flight()
                return {
                    "completed_at_ms": completed_at_ms,
                    "error": exc.to_dict(),
                    "reason": exc.code,
                    "seq": sequence,
                    "started_at_ms": started_at_ms,
                    "status": "failed_after_start",
                    "txid": record.logical_tx_id,
                }
            except sqlite3.Error as exc:
                self.store.cleanup_in_flight()
                error = apply_failed(
                    "SQLite rejected the transaction",
                    txid=record.logical_tx_id,
                    reason=str(exc),
                )
                return {
                    "completed_at_ms": completed_at_ms,
                    "error": error.to_dict(),
                    "reason": error.code,
                    "seq": sequence,
                    "started_at_ms": started_at_ms,
                    "status": "failed_after_start",
                    "txid": record.logical_tx_id,
                }

    def _select_round(
        self,
        pending: Sequence[WalRecord],
        policy: Policy,
    ) -> tuple[list[WalRecord], bool]:
        selected: list[WalRecord] = []
        spent = 0
        keys: set[tuple[str, str]] = set()
        budget_cut = False
        for record in pending:
            if len(selected) >= policy.max_concurrent_txs:
                break
            row_key = (record.table, record.row_key)
            if row_key in keys:
                budget_cut = False
                break
            if spent + record.duration_ms > policy.round_budget_ms:
                budget_cut = True
                break
            selected.append(record)
            keys.add(row_key)
            spent += record.duration_ms
        return selected, budget_cut

    def _record_outcome(
        self,
        round_id: str,
        outcome: Mapping[str, Any],
    ) -> dict[str, Any]:
        stored = dict(outcome)
        stored["round_id"] = round_id
        self.store.record_outcome(
            round_id=round_id,
            sequence=int(outcome["seq"]),
            txid=int(outcome["txid"]),
            status=str(outcome["status"]),
            reason=str(outcome["reason"]),
            started_at_ms=outcome.get("started_at_ms"),
            completed_at_ms=outcome.get("completed_at_ms"),
        )
        return stored

    def _apply_sample(
        self,
        sample: Sample,
        *,
        clock_ms: int | None,
        max_rounds: int | None,
        no_auto_checkpoint: bool,
        already_recovered: bool = False,
    ) -> dict[str, Any]:
        self.store.require_initialized()
        with self.store.connection(writable=False) as connection:
            persisted_clock = self.store.get_meta_int(connection, "logical_clock_ms")
            pointer = self.store.get_pointer(connection)
        current_clock = self._check_clock(
            sample,
            clock_ms,
            persisted=persisted_clock,
        )
        if pointer is not None:
            if pointer["applied_watermark"] == 0:
                expected_chain = ZERO_HASH
            else:
                anchor = sample.tx_by_id.get(pointer["applied_watermark"])
                if anchor is None:
                    first = sample.wal[0]
                    if (
                        first.prev_tx_id == pointer["applied_watermark"]
                        and first.prev_tx_hash == pointer["chain_hash"]
                    ):
                        expected_chain = pointer["chain_hash"]
                    else:
                        raise chain_break(
                            "checkpoint pointer watermark is absent from WAL",
                            applied_watermark=pointer["applied_watermark"],
                        )
                else:
                    expected_chain = anchor.tx_hash
            if pointer["chain_hash"] != expected_chain:
                raise chain_break(
                    "checkpoint pointer chain anchor differs from WAL",
                    applied_watermark=pointer["applied_watermark"],
                    expected=expected_chain,
                    actual=pointer["chain_hash"],
                )
        if current_clock != persisted_clock:
            with self.store.transaction() as connection:
                self.store.set_meta(connection, "logical_clock_ms", str(current_clock))

        all_outcomes: list[dict[str, Any]] = []
        checkpoints_created: list[dict[str, Any]] = []
        rounds_run = 0
        applied_now: list[int] = []
        failed = False
        stopped_for_budget = False

        while True:
            applied_ids = self.store.applied_txids()
            pending = [
                record
                for record in sample.wal
                if record.logical_tx_id not in set(applied_ids)
            ]
            if not pending:
                break
            if max_rounds is not None and rounds_run >= max_rounds:
                round_counter, round_id = self.store.next_round_id()
                start = current_clock
                for seq, record in enumerate(pending, start=1):
                    outcome = {
                        "completed_at_ms": None,
                        "reason": "max_rounds_reached",
                        "seq": seq,
                        "started_at_ms": None,
                        "status": "actively_abandoned",
                        "txid": record.logical_tx_id,
                    }
                    all_outcomes.append(self._record_outcome(round_id, outcome))
                break

            selected, budget_cut = self._select_round(pending, sample.policy)
            round_counter, round_id = self.store.next_round_id()
            start = current_clock
            if not selected:
                for seq, record in enumerate(pending, start=1):
                    outcome = {
                        "completed_at_ms": None,
                        "reason": "round_budget_exhausted",
                        "seq": seq,
                        "started_at_ms": None,
                        "status": "not_started_budget",
                        "txid": record.logical_tx_id,
                    }
                    all_outcomes.append(self._record_outcome(round_id, outcome))
                stopped_for_budget = True
                break

            rounds_run += 1
            if budget_cut:
                cutoff_ids = {
                    record.logical_tx_id
                    for record in pending
                    if record not in selected
                }
            else:
                cutoff_ids = set()

            outcomes_by_seq: dict[int, dict[str, Any]] = {}
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(selected),
                thread_name_prefix=f"walrec-{round_counter}",
            ) as executor:
                futures = {
                    executor.submit(
                        self._apply_one,
                        sample,
                        record,
                        round_id=round_id,
                        sequence=seq,
                        started_at_ms=start,
                    ): (seq, record)
                    for seq, record in enumerate(selected, start=1)
                }
                for future in concurrent.futures.as_completed(futures):
                    seq, record = futures[future]
                    try:
                        outcome = future.result()
                    except BaseException as exc:  # defensive worker boundary
                        outcome = {
                            "completed_at_ms": None,
                            "error": {
                                "code": "E_APPLY_FAILED",
                                "message": str(exc),
                                "ok": False,
                                "details": {},
                            },
                            "reason": "E_APPLY_FAILED",
                            "seq": seq,
                            "started_at_ms": start,
                            "status": "failed_after_start",
                            "txid": record.logical_tx_id,
                        }
                    outcomes_by_seq[seq] = outcome
            ordered = [outcomes_by_seq[seq] for seq in sorted(outcomes_by_seq)]
            for outcome in ordered:
                all_outcomes.append(self._record_outcome(round_id, outcome))
                if outcome["status"] == "applied":
                    applied_now.append(int(outcome["txid"]))
                if outcome["status"] == "failed_after_start":
                    failed = True

            max_duration = max(record.duration_ms for record in selected)
            next_clock = start + max(sample.policy.clock_step_ms, max_duration)
            with self.store.transaction() as connection:
                self.store.set_meta(connection, "logical_clock_ms", str(next_clock))
            current_clock = next_clock

            if cutoff_ids:
                seq = len(selected) + 1
                for record in pending:
                    if record.logical_tx_id not in cutoff_ids:
                        continue
                    outcome = {
                        "completed_at_ms": None,
                        "reason": "round_budget_exhausted",
                        "seq": seq,
                        "started_at_ms": None,
                        "status": "not_started_budget",
                        "txid": record.logical_tx_id,
                    }
                    all_outcomes.append(self._record_outcome(round_id, outcome))
                    seq += 1

            if failed:
                applied_ids = set(self.store.applied_txids())
                failed_ids = {
                    int(outcome["txid"])
                    for outcome in ordered
                    if outcome["status"] == "failed_after_start"
                }
                round_counter, round_id = self.store.next_round_id()
                seq = 1
                for record in sample.wal:
                    if (
                        record.logical_tx_id in applied_ids
                        or record.logical_tx_id in failed_ids
                    ):
                        continue
                    outcome = {
                        "completed_at_ms": None,
                        "reason": "stopped_after_started_failure",
                        "seq": seq,
                        "started_at_ms": None,
                        "status": "actively_abandoned",
                        "txid": record.logical_tx_id,
                    }
                    all_outcomes.append(self._record_outcome(round_id, outcome))
                    seq += 1
                break

            if not no_auto_checkpoint:
                with self.store.connection(writable=False) as connection:
                    last_checkpoint_clock = self.store.get_meta_int(
                        connection,
                        "last_checkpoint_clock_ms",
                    )
                    stored_clock = self.store.get_meta_int(connection, "logical_clock_ms")
                if (
                    next_clock - last_checkpoint_clock
                    >= sample.policy.checkpoint_interval_ms
                ):
                    created = self.checkpoint(
                        sample.root,
                        clock_ms=next_clock,
                    )
                    if created.get("created"):
                        checkpoints_created.append(created)
                        current_clock = max(
                            int(created["checkpoint"]["commit_ts_ms"]),
                            stored_clock,
                        )

        applied_ids = self.store.applied_txids()
        pending = self._pending_ids(sample, applied_ids)
        if (
            not no_auto_checkpoint
            and not pending
            and not failed
            and not stopped_for_budget
        ):
            with self.store.connection(writable=False) as connection:
                final_pointer = self.store.get_pointer(connection)
                final_summaries = self.store.summaries_snapshot(connection)
                final_watermark = self.store.get_meta_int(
                    connection,
                    "applied_watermark",
                )
            if final_pointer is not None and (
                final_pointer["applied_watermark"] < final_watermark
                or final_pointer["table_summaries"] != final_summaries
            ):
                created = self.checkpoint(sample.root)
                if created.get("created"):
                    checkpoints_created.append(created)
                    current_clock = max(
                        current_clock,
                        int(created["checkpoint"]["commit_ts_ms"]),
                    )
        status = self.store.status(pending_tx_ids=pending)
        return {
            "applied": sorted(set(applied_now)),
            "checkpoints_created": checkpoints_created,
            "idempotent": not applied_now,
            "ok": not failed and not stopped_for_budget,
            "outcomes": all_outcomes,
            "rounds_run": rounds_run,
            "status": status,
        }

    def apply(
        self,
        sample_dir: str | Path,
        *,
        clock_ms: int | None = None,
        max_rounds: int | None = None,
        no_auto_checkpoint: bool = False,
        policy_overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        sample = self._load_sample(sample_dir, policy_overrides)
        self.store.require_initialized()
        return self._apply_sample(
            sample,
            clock_ms=clock_ms,
            max_rounds=max_rounds,
            no_auto_checkpoint=no_auto_checkpoint,
        )

    def _next_generation_and_versions(
        self,
        current: Mapping[str, Any],
    ) -> tuple[int, int, int]:
        return (
            int(current["checkpoint_gen"]) + 1,
            int(current["manifest_version"]) + 1,
            int(current["replay_generation"]) + 1,
        )

    def checkpoint(
        self,
        sample_dir: str | Path | None = None,
        *,
        clock_ms: int | None = None,
        package_path: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        sample: Sample | None = None
        if sample_dir is not None:
            sample = self._load_sample(sample_dir)
        self.store.require_initialized()
        if package_path is not None:
            return self.install_checkpoint(package_path, clock_ms=clock_ms)
        default_tolerance = 4 if sample is None else sample.policy.clock_tolerance_ms
        with self.store.connection(writable=False) as connection:
            pointer = self.store.get_pointer(connection)
            persisted_clock = self.store.get_meta_int(connection, "logical_clock_ms")
        if pointer is None:
            raise state_error("checkpoint creation requires a loaded state")
        selected_clock = persisted_clock if clock_ms is None else clock_ms
        if selected_clock < persisted_clock - default_tolerance:
            raise clock_rollback(
                "checkpoint clock moved backwards beyond tolerance",
                persisted_clock_ms=persisted_clock,
                requested_clock_ms=selected_clock,
                tolerance_ms=default_tolerance,
            )
        selected_clock = max(selected_clock, persisted_clock)

        with self._process_lock, self.store.lock:
            with self.store.connection(writable=False) as connection:
                pointer = self.store.get_pointer(connection)
                assert pointer is not None
                rows = self.store.rows_snapshot(connection)
                summaries = summarize_tables(rows)
                watermark = self.store.get_meta_int(connection, "applied_watermark")
                applied = self.store.applied_tx_map(connection)
            if (
                pointer["format_version"] == "v2"
                and watermark == pointer["applied_watermark"]
                and summaries == pointer["table_summaries"]
                and selected_clock <= pointer["logical_clock_ms"]
            ):
                return {
                    "checkpoint": {
                        "checkpoint_gen": pointer["checkpoint_gen"],
                        "logical_clock_ms": pointer["logical_clock_ms"],
                        "manifest_version": pointer["manifest_version"],
                        "replay_generation": pointer["replay_generation"],
                    },
                    "created": False,
                    "decision": "noop",
                    "ok": True,
                    "status": self.store.status(),
                }

            generation, manifest_version, replay_generation = (
                self._next_generation_and_versions(pointer)
            )
            if output_dir is None:
                checkpoint_root = self.db_path.parent / "checkpoints"
            else:
                checkpoint_root = Path(output_dir).resolve()
            checkpoint_root.mkdir(parents=True, exist_ok=True)
            destination = checkpoint_root / f"gen-{generation:06d}"
            while destination.exists():
                candidate_manifest, _, candidate_hash = read_v2_package(destination)
                if (
                    int(candidate_manifest["checkpoint_gen"]) == generation
                    and int(candidate_manifest["applied_watermark"]) == watermark
                    and candidate_manifest["table_summaries"] == summaries
                ):
                    break
                generation += 1
                manifest_version += 1
                replay_generation += 1
                destination = checkpoint_root / f"gen-{generation:06d}"

            if watermark == 0:
                chain_hash = ZERO_HASH
            else:
                applied_row = applied.get(watermark)
                if applied_row is None:
                    raise state_error(
                        "applied watermark has no transaction record",
                        applied_watermark=watermark,
                    )
                chain_hash = applied_row["tx_hash"]
            changed_after = pointer["applied_watermark"]
            dirty: dict[str, dict[str, list[str]]] = {}
            for table, table_rows in rows.items():
                pages: dict[str, list[str]] = {}
                for row in sorted(table_rows, key=lambda item: str(item["key"])):
                    if int(row["source_tx"]) <= changed_after:
                        continue
                    page_id = page_for_key(table, str(row["key"]))
                    pages.setdefault(page_id, []).append(str(row["key"]))
                dirty[table] = {
                    page: sorted(keys) for page, keys in sorted(pages.items())
                }
            manifest_base = {
                "applied_watermark": watermark,
                "chain_hash": chain_hash,
                "checkpoint_gen": generation,
                "commit_ts_ms": selected_clock,
                "dirty_pages": dirty,
                "logical_clock_ms": selected_clock,
                "manifest_version": manifest_version,
                "min_replay_generation": int(pointer["replay_generation"]),
                "parent_checkpoint_gen": int(pointer["checkpoint_gen"]),
                "replay_generation": replay_generation,
                "update_kind": "full",
            }
            if destination.exists():
                manifest, _, manifest_hash = read_v2_package(destination)
                selected_clock = max(
                    selected_clock,
                    int(manifest["commit_ts_ms"]),
                )
                checkpoint, _ = checkpoint_from_v2(destination)
            else:
                manifest, manifest_hash = write_v2_package(
                    destination,
                    manifest_base,
                    rows,
                )
                checkpoint, _ = checkpoint_from_v2(destination)
            _maybe_failpoint("checkpoint_after_package", generation)
            with self.store.transaction() as connection:
                latest = self.store.get_pointer(connection)
                if latest is None:
                    raise state_error("checkpoint pointer disappeared during creation")
                if latest["checkpoint_gen"] != pointer["checkpoint_gen"]:
                    raise version_rollback(
                        "checkpoint pointer changed concurrently",
                        expected_generation=pointer["checkpoint_gen"],
                        actual_generation=latest["checkpoint_gen"],
                    )
                self.store.replace_checkpoint_pointer(
                    connection,
                    checkpoint,
                    decision="full_replace",
                    installed_at_ms=selected_clock,
                )
                self.store.set_meta(
                    connection,
                    "logical_clock_ms",
                    str(max(selected_clock, persisted_clock)),
                )
                self.store.set_meta(
                    connection,
                    "last_checkpoint_clock_ms",
                    str(selected_clock),
                )
            return {
                "checkpoint": {
                    "checkpoint_gen": generation,
                    "commit_ts_ms": selected_clock,
                    "manifest_hash": manifest_hash,
                    "manifest_version": manifest_version,
                    "replay_generation": replay_generation,
                    "source": destination.name,
                },
                "created": True,
                "decision": "full_replace",
                "ok": True,
                "status": self.store.status(),
            }

    def install_checkpoint(
        self,
        package_path: str | Path,
        *,
        clock_ms: int | None = None,
    ) -> dict[str, Any]:
        package = Path(package_path).resolve()
        checkpoint, _ = checkpoint_from_v2(package)
        self.store.require_initialized()
        with self._process_lock, self.store.lock:
            with self.store.transaction() as connection:
                pointer = self.store.get_pointer(connection)
                if pointer is None:
                    raise state_error("checkpoint installation requires a loaded state")
                current_clock = self.store.get_meta_int(connection, "logical_clock_ms")
                persisted_tolerance = 4
                selected_clock = (
                    current_clock if clock_ms is None else max(clock_ms, current_clock)
                )
                if clock_ms is not None and clock_ms < current_clock - persisted_tolerance:
                    raise clock_rollback(
                        "checkpoint installation clock moved backwards",
                        persisted_clock_ms=current_clock,
                        requested_clock_ms=clock_ms,
                        tolerance_ms=persisted_tolerance,
                    )
                if (
                    checkpoint.checkpoint_gen == pointer["checkpoint_gen"]
                    and checkpoint.manifest_hash == pointer["manifest_hash"]
                ):
                    return {
                        "checkpoint": {
                            "checkpoint_gen": checkpoint.checkpoint_gen,
                            "manifest_version": checkpoint.manifest_version,
                            "replay_generation": checkpoint.replay_generation,
                        },
                        "created": False,
                        "decision": "noop",
                        "ok": True,
                        "status": self.store.status(),
                    }
                if (
                    checkpoint.checkpoint_gen < pointer["checkpoint_gen"]
                    or checkpoint.manifest_version < pointer["manifest_version"]
                    or checkpoint.replay_generation < pointer["replay_generation"]
                ):
                    raise version_rollback(
                        "incoming checkpoint would roll back the current pointer",
                        current_checkpoint_gen=pointer["checkpoint_gen"],
                        incoming_checkpoint_gen=checkpoint.checkpoint_gen,
                        current_manifest_version=pointer["manifest_version"],
                        incoming_manifest_version=checkpoint.manifest_version,
                        current_replay_generation=pointer["replay_generation"],
                        incoming_replay_generation=checkpoint.replay_generation,
                    )
                if checkpoint.min_replay_generation > pointer["replay_generation"]:
                    raise replay_generation_error(
                        "checkpoint requires a newer replay generation base",
                        current_replay_generation=pointer["replay_generation"],
                        min_replay_generation=checkpoint.min_replay_generation,
                    )
                if checkpoint.checkpoint_gen == pointer["checkpoint_gen"]:
                    raise version_rollback(
                        "same checkpoint generation carries a different manifest",
                        checkpoint_gen=checkpoint.checkpoint_gen,
                    )
                summaries = self.store.summaries_snapshot(connection)
                if summaries != checkpoint.table_summaries:
                    raise summary_mismatch(
                        "incoming checkpoint table summaries differ from persisted rows",
                        expected=summaries,
                        actual=checkpoint.table_summaries,
                    )
                if checkpoint.applied_watermark > self.store.get_meta_int(
                    connection,
                    "applied_watermark",
                ):
                    raise state_error(
                        "incoming checkpoint is ahead of the applied watermark",
                        incoming_watermark=checkpoint.applied_watermark,
                        applied_watermark=self.store.get_meta_int(
                            connection,
                            "applied_watermark",
                        ),
                    )
                if (
                    checkpoint.update_kind == "delta"
                    and checkpoint.parent_checkpoint_gen != pointer["checkpoint_gen"]
                ):
                    raise version_rollback(
                        "delta checkpoint parent is not the current pointer",
                        parent_checkpoint_gen=checkpoint.parent_checkpoint_gen,
                        current_checkpoint_gen=pointer["checkpoint_gen"],
                    )
                decision = (
                    "incremental_append"
                    if checkpoint.update_kind == "delta"
                    else "full_replace"
                )
                self.store.replace_checkpoint_pointer(
                    connection,
                    checkpoint,
                    decision=decision,
                    installed_at_ms=selected_clock,
                )
                self.store.set_meta(
                    connection,
                    "logical_clock_ms",
                    str(max(current_clock, checkpoint.logical_clock_ms)),
                )
                self.store.set_meta(
                    connection,
                    "last_checkpoint_clock_ms",
                    str(checkpoint.logical_clock_ms),
                )
            status = self.store.status()
            return {
                "checkpoint": {
                    "checkpoint_gen": checkpoint.checkpoint_gen,
                    "manifest_version": checkpoint.manifest_version,
                    "replay_generation": checkpoint.replay_generation,
                },
                "created": True,
                "decision": decision,
                "ok": True,
                "status": status,
            }

    def status(self, sample_dir: str | Path | None = None) -> dict[str, Any]:
        self.store.require_initialized()
        pending: list[int] | None = None
        if sample_dir is not None:
            sample = self._load_sample(sample_dir)
            pending = self._pending_ids(sample, self.store.applied_txids())
        return {
            "ok": True,
            "status": self.store.status(pending_tx_ids=pending),
        }

    def verify(self, sample_dir: str | Path | None = None) -> dict[str, Any]:
        sample = self._load_sample(sample_dir) if sample_dir is not None else None
        self.store.require_initialized()
        mismatches: list[dict[str, Any]] = []
        with self.store.connection(writable=False) as connection:
            rows = self.store.rows_snapshot(connection)
            applied = self.store.applied_tx_map(connection)
            watermark = self.store.get_meta_int(connection, "applied_watermark")
            meta_summaries_raw = self.store.get_meta(connection, "state_table_summaries")
            meta_summaries = (
                json.loads(meta_summaries_raw) if meta_summaries_raw is not None else {}
            )
            meta_digest = self.store.get_meta(connection, "state_rows_digest")
            checkpoint_base = self.store.get_meta_int(
                connection,
                "checkpoint_base_watermark",
            )
            pointer = self.store.get_pointer(connection)
            contiguous = self.store.contiguous_watermark(connection)

        for table, table_rows in rows.items():
            for row in table_rows:
                expected = row_digest(
                    table,
                    str(row["key"]),
                    row["payload"],
                    int(row["version"]),
                    int(row["source_tx"]),
                    bool(row["tombstone"]),
                )
                if expected != row["row_hash"]:
                    mismatches.append(
                        {
                            "reason": "row_hash_mismatch",
                            "table": table,
                            "txid": int(row["source_tx"]),
                        }
                    )
                if int(row["source_tx"]) not in applied:
                    mismatches.append(
                        {
                            "reason": "row_source_tx_not_committed",
                            "table": table,
                            "txid": int(row["source_tx"]),
                        }
                    )
        actual_summaries = summarize_tables(rows)
        if actual_summaries != meta_summaries:
            for table in sorted(set(actual_summaries) | set(meta_summaries)):
                if actual_summaries.get(table) == meta_summaries.get(table):
                    continue
                txids = [int(row["source_tx"]) for row in rows.get(table, [])]
                mismatches.append(
                    {
                        "reason": "persisted_table_summary_mismatch",
                        "table": table,
                        "txid": max(txids, default=0),
                    }
                )
        if meta_digest != sha256_json(actual_summaries):
            txids = [
                int(row["source_tx"])
                for table_rows in rows.values()
                for row in table_rows
            ]
            mismatches.append(
                {
                    "reason": "persisted_state_digest_mismatch",
                    "table": "*",
                    "txid": max(txids, default=0),
                }
            )
        if watermark != contiguous:
            missing_start = contiguous + 1
            for txid in applied:
                if txid >= missing_start:
                    mismatches.append(
                        {
                            "reason": "watermark_not_contiguous",
                            "table": "*",
                            "txid": txid,
                        }
                    )
                    break
        if pointer is not None and pointer["applied_watermark"] == watermark:
            if pointer["table_summaries"] != actual_summaries:
                for table in sorted(
                    set(pointer["table_summaries"]) | set(actual_summaries)
                ):
                    if pointer["table_summaries"].get(table) == actual_summaries.get(table):
                        continue
                    txids = [int(row["source_tx"]) for row in rows.get(table, [])]
                    mismatches.append(
                        {
                            "reason": "checkpoint_table_summary_mismatch",
                            "table": table,
                            "txid": max(txids, default=0),
                        }
                    )
        if sample is not None:
            wal_by_id = sample.tx_by_id
            expected_rows = expected_rows_for_sample(sample, watermark)
            if expected_rows is not None:
                for table in sorted(set(expected_rows) | set(rows)):
                    expected_by_key = {
                        str(row["key"]): row for row in expected_rows.get(table, [])
                    }
                    actual_by_key = {
                        str(row["key"]): row for row in rows.get(table, [])
                    }
                    for key in sorted(set(expected_by_key) | set(actual_by_key)):
                        expected_row = expected_by_key.get(key)
                        actual_row = actual_by_key.get(key)
                        if expected_row == actual_row:
                            continue
                        if expected_row is None:
                            reason = "row_unexpected"
                            txid = int(actual_row["source_tx"])
                        elif actual_row is None:
                            reason = "row_missing"
                            txid = int(expected_row["source_tx"])
                        else:
                            reason = "row_content_mismatch"
                            txid = int(expected_row["source_tx"])
                        mismatches.append(
                            {
                                "reason": reason,
                                "table": table,
                                "txid": txid,
                            }
                        )
            for txid, info in sorted(applied.items()):
                expected = wal_by_id.get(txid)
                if expected is None:
                    if txid > checkpoint_base:
                        mismatches.append(
                            {
                                "reason": "applied_tx_absent_from_wal",
                                "table": "*",
                                "txid": txid,
                            }
                        )
                elif expected.tx_hash != info["tx_hash"]:
                    mismatches.append(
                        {
                            "reason": "applied_tx_hash_mismatch",
                            "table": "*",
                            "txid": txid,
                        }
                    )
            for txid in range(1, watermark + 1):
                if txid not in applied and txid in wal_by_id:
                    mismatches.append(
                        {
                            "reason": "watermark_tx_not_applied",
                            "table": "*",
                            "txid": txid,
                        }
                    )
        mismatches.sort(
            key=lambda item: (
                int(item["txid"]),
                str(item["reason"]),
                str(item["table"]),
            )
        )
        return {
            "applied_tx_count": len(applied),
            "checked_rows": sum(len(table_rows) for table_rows in rows.values()),
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "ok": not mismatches,
            "watermark": watermark,
        }

    def snapshot(self) -> dict[str, Any]:
        self.store.require_initialized()
        rows, pointer = self.store.snapshot_pair()
        return {"pointer": pointer, "rows": rows}

    def compare(self, other_db: str | Path) -> dict[str, Any]:
        self.store.require_initialized()
        other = Store(other_db)
        other.require_initialized()
        left, left_pointer = self.store.snapshot_pair()
        right, right_pointer = other.snapshot_pair()
        differences = compare_row_snapshots(left, right)
        return {
            "different_row_count": len(differences),
            "differences": differences,
            "ok": not differences,
            "pointer_equal": left_pointer == right_pointer,
            "pointers": {"left": left_pointer, "right": right_pointer},
        }
