from __future__ import annotations

import json
import os
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Any

from .config import (
    canonical_json,
    load_configuration,
    parse_events,
    stable_hash,
)
from .dataio import DataFiles
from .errors import EngineError, fail
from .fixed import units_to_decimal
from .models import Configuration, EventSpec
from .store import Store, Transaction


class _BufferFull(Exception):
    pass


def _event_identity(event: EventSpec) -> dict[str, Any]:
    return {
        "tenant": event.tenant,
        "metric": event.metric,
        "tags": [list(item) for item in event.tags],
        "logical_time_ms": event.logical_time_ms,
        "sample_seq": event.sample_seq,
        "sample_bytes": event.sample_bytes,
    }


def _event_identity_hash(event: EventSpec) -> str:
    return stable_hash(_event_identity(event))


def _read_json_file(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        fail("E_IO", f"file not found: {path}", path=str(path))
    except json.JSONDecodeError as exc:
        fail("E_BAD_JSON", f"invalid JSON in {path}: {exc.msg}", path=str(path))
    except OSError as exc:
        fail("E_IO", f"cannot read {path}: {exc}", path=str(path))


class Engine:
    def __init__(self, store: Store, config: Configuration) -> None:
        self.store = store
        self.config = config
        self.data = DataFiles(store.data_dir)
        self._stream_map = config.stream_map
        self._quota_scale = config.quotas.fixed_scale
        self._token_scale = config.tokens.fixed_scale
        self._token_cost_units = (
            config.partitions.token_cost_per_sample * config.tokens.fixed_scale
        )

    @classmethod
    def load(
        cls,
        workspace: str | Path,
        *,
        streams_path: str | Path,
        partitions_path: str | Path,
        quotas_path: str | Path,
        tokens_path: str | Path,
    ) -> tuple["Engine", dict[str, Any]]:
        config = load_configuration(
            streams_path, partitions_path, quotas_path, tokens_path
        )
        store = Store(workspace)
        report = store.initialize(config)
        engine = cls(store, config)
        store.refresh_derived_files()
        store.append_log({"operation": "load", **report})
        return engine, report

    @classmethod
    def open(cls, workspace: str | Path) -> "Engine":
        store = Store(workspace)
        if not store.db_path.exists():
            fail(
                "E_CONFIG_NOT_LOADED",
                "workspace has not been initialized; run load first",
                workspace=str(workspace),
            )
        config = store.load_configuration()
        return cls(store, config)

    def _stream_for(self, event: EventSpec) -> dict[str, Any]:
        stream = self._stream_map.get((event.tenant, event.metric))
        if stream is None:
            fail(
                "E_BAD_STREAM",
                "event references an unknown stream",
                tenant=event.tenant,
                metric=event.metric,
            )
        return {
            "partition": stream.partition,
            "sample_bytes": stream.sample_bytes,
            "sample_period_ms": stream.sample_period_ms,
        }

    def _existing_sample(
        self, tx: Transaction, event: EventSpec
    ) -> dict[str, Any] | None:
        existing = tx.sample_by_identity(_event_identity_hash(event))
        if existing is not None:
            return existing
        existing = tx.sample_by_id(event.event_id)
        if existing is not None:
            if existing["identity_hash"] != _event_identity_hash(event):
                fail(
                    "E_EVENT_ID_CONFLICT",
                    "event_id was already used for a different sample",
                    event_id=event.event_id,
                )
        return existing

    def _insert_one(self, event: EventSpec, *, force_drop: bool = False) -> dict[str, Any]:
        partition = self._stream_for(event)["partition"]
        with self.store.transaction("ingest") as tx:
            existing = self._existing_sample(tx, event)
            if existing is not None:
                tx.record_ingest_decision(
                    existing,
                    decision="IDEMPOTENT",
                    reason=f"existing_state={existing['state']}",
                    detail={"existing_state": existing["state"]},
                )
                return {
                    "event_id": event.event_id,
                    "sample_id": existing["sample_id"],
                    "decision": "IDEMPOTENT",
                    "state": existing["state"],
                    "partition": int(existing["partition"]),
                    "partition_arrival_seq": int(existing["partition_arrival_seq"]),
                }

            tx.set_clock(event.logical_time_ms)
            depth = tx.buffer_depth(partition)
            if depth >= self.config.partitions.buffer_per_partition and not force_drop:
                raise _BufferFull

            if depth >= self.config.partitions.buffer_per_partition:
                sample, _ = tx.insert_sample(
                    event,
                    partition=partition,
                    state="DROPPED",
                    decision="DROPPED_BUFFER_FULL",
                    reason="BUFFER_FULL_AFTER_FLUSH",
                    first_constraint={
                        "constraint": "partition_buffer",
                        "partition": partition,
                        "buffer_limit": self.config.partitions.buffer_per_partition,
                    },
                )
                tx.record_ingest_decision(
                    sample,
                    decision="DROPPED",
                    reason="BUFFER_FULL_AFTER_FLUSH",
                    detail={
                        "buffer_depth": depth,
                        "buffer_limit": self.config.partitions.buffer_per_partition,
                    },
                )
                return {
                    "event_id": event.event_id,
                    "sample_id": sample["sample_id"],
                    "decision": "DROPPED",
                    "reason": "BUFFER_FULL_AFTER_FLUSH",
                    "state": "DROPPED",
                    "partition": partition,
                    "partition_arrival_seq": int(sample["partition_arrival_seq"]),
                }

            sample, _ = tx.insert_sample(
                event,
                partition=partition,
                state="BUFFERED",
                decision="BUFFERED",
                reason=None,
                first_constraint=None,
            )
            tx.record_ingest_decision(
                sample,
                decision="BUFFERED",
                reason=None,
                detail={"buffer_depth": depth + 1},
            )
            return {
                "event_id": event.event_id,
                "sample_id": sample["sample_id"],
                "decision": "BUFFERED",
                "state": "BUFFERED",
                "partition": partition,
                "partition_arrival_seq": int(sample["partition_arrival_seq"]),
            }

    def ingest_document(
        self,
        document: Any,
        *,
        auto_flush: bool = True,
    ) -> dict[str, Any]:
        events, batch_duplicates = parse_events(document, self.config)
        events.sort(key=lambda item: (item.logical_time_ms, item.sample_seq, item.event_id))
        decisions: list[dict[str, Any]] = []
        for duplicate in batch_duplicates:
            decisions.append(
                {
                    "event_id": duplicate["event_id"],
                    "decision": "IDEMPOTENT",
                    "state": "DUPLICATE_IN_BATCH",
                    "batch_index": duplicate["index"],
                }
            )
        auto_rounds: list[dict[str, Any]] = []
        for event in events:
            try:
                decision = self._insert_one(event)
            except _BufferFull:
                if not auto_flush:
                    decision = self._insert_one(event, force_drop=True)
                else:
                    flushed = self.flush(
                        clock_ms=event.logical_time_ms,
                        trigger="buffer_full",
                        rounds=1,
                    )
                    auto_rounds.extend(flushed["rounds"])
                    decision = self._insert_one(event, force_drop=True)
            decisions.append(decision)
        self.store.refresh_derived_files()
        report = {
            "ok": True,
            "operation": "ingest",
            "count": len(decisions),
            "decisions": decisions,
            "auto_flush_rounds": auto_rounds,
            "version": self.store.current_version(),
            "logical_clock_ms": self.store.logical_clock(),
        }
        self.store.append_log(
            {
                "operation": "ingest",
                "count": report["count"],
                "version": report["version"],
                "logical_clock_ms": report["logical_clock_ms"],
            }
        )
        return report

    def ingest_file(
        self,
        path: str | Path,
        *,
        auto_flush: bool = True,
    ) -> dict[str, Any]:
        return self.ingest_document(
            _read_json_file(path), auto_flush=auto_flush
        )

    def _tenant_window_id(self, tenant: str, logical_clock_ms: int) -> int:
        declarations = self.config.quotas.tenants
        if tenant not in declarations:
            fail("E_BAD_QUOTA", "tenant has no quota declaration", tenant=tenant)
        return logical_clock_ms // self.config.quotas.window_ms

    def _detect_precision_sensitivity(
        self,
        tx: Transaction,
        candidates: list[dict[str, Any]],
        *,
        clock_ms: int,
        round_id: str,
        token_balance_units: int,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        tolerance_units = max(
            0,
            int(
                tx.conn.execute(
                    "SELECT MAX(comparison_tolerance_units) AS t FROM tenant_quotas"
                ).fetchone()["t"]
                or 0
            ),
        )
        if tolerance_units == 0:
            return []

        tenant_entries: dict[str, dict[str, Any]] = {}
        partition_entries: dict[int, dict[str, Any]] = {}
        for candidate in candidates:
            tenant = candidate["tenant"]
            required = int(candidate["sample_bytes"]) * self._quota_scale
            if tenant not in tenant_entries:
                availability = tx.quota_availability(tenant, clock_ms)
                passes = (
                    int(availability["available_units"]) >= required
                    and token_balance_units >= self._token_cost_units
                )
                tenant_entries[tenant] = {
                    "tenant": tenant,
                    "window_id": int(availability["window_id"]),
                    "availability_units": int(availability["available_units"]),
                    "required_units": required,
                    "passes": passes,
                }
            partition = int(candidate["partition"])
            if partition not in partition_entries:
                availability = tx.quota_availability(tenant, clock_ms)
                residual = int(availability["available_units"]) - required
                passes = residual >= 0 and token_balance_units >= self._token_cost_units
                partition_entries[partition] = {
                    "tenant": tenant,
                    "window_id": int(availability["window_id"]),
                    "partition": partition,
                    "residual_units": residual,
                    "required_units": required,
                    "passes": passes,
                }

        marks: list[dict[str, Any]] = []
        for left, right in combinations(sorted(tenant_entries.values(), key=lambda x: x["tenant"]), 2):
            delta = abs(left["availability_units"] - right["availability_units"])
            if delta < tolerance_units and left["passes"] != right["passes"]:
                sources = {
                    "entity_a": tx.precision_sources(
                        tenant=left["tenant"],
                        window_id=left["window_id"],
                        partition=None,
                    ),
                    "entity_b": tx.precision_sources(
                        tenant=right["tenant"],
                        window_id=right["window_id"],
                        partition=None,
                    ),
                }
                tx.record_precision_sensitive(
                    round_id=round_id,
                    kind="TENANT",
                    entity_a=left["tenant"],
                    entity_b=right["tenant"],
                    availability_a_units=left["availability_units"],
                    availability_b_units=right["availability_units"],
                    delta_units=delta,
                    tolerance_units=tolerance_units,
                    decision_depends=True,
                    sources=sources,
                    logical_clock_ms=clock_ms,
                )
                marks.append(
                    {
                        "kind": "TENANT",
                        "entity_a": left["tenant"],
                        "entity_b": right["tenant"],
                        "delta_units": delta,
                        "tolerance_units": tolerance_units,
                    }
                )

        for left, right in combinations(
            sorted(partition_entries.values(), key=lambda x: x["partition"]), 2
        ):
            delta = abs(left["residual_units"] - right["residual_units"])
            if delta < tolerance_units and left["passes"] != right["passes"]:
                sources = {
                    "entity_a": tx.precision_sources(
                        tenant=left["tenant"],
                        window_id=left["window_id"],
                        partition=left["partition"],
                    ),
                    "entity_b": tx.precision_sources(
                        tenant=right["tenant"],
                        window_id=right["window_id"],
                        partition=right["partition"],
                    ),
                }
                tx.record_precision_sensitive(
                    round_id=round_id,
                    kind="PARTITION",
                    entity_a=str(left["partition"]),
                    entity_b=str(right["partition"]),
                    availability_a_units=left["residual_units"],
                    availability_b_units=right["residual_units"],
                    delta_units=delta,
                    tolerance_units=tolerance_units,
                    decision_depends=True,
                    sources=sources,
                    logical_clock_ms=clock_ms,
                )
                marks.append(
                    {
                        "kind": "PARTITION",
                        "entity_a": str(left["partition"]),
                        "entity_b": str(right["partition"]),
                        "delta_units": delta,
                        "tolerance_units": tolerance_units,
                    }
                )
        return marks

    def _flush_round(self, clock_ms: int, trigger: str) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        with self.store.transaction(f"flush:{trigger}") as tx:
            tx.set_clock(clock_ms)
            tx.advance_tokens(clock_ms)
            frozen = tx.freeze_windows(clock_ms)
            candidates = tx.buffered_samples()
            round_id = "r-" + stable_hash(
                {
                    "version": tx.version,
                    "clock_ms": clock_ms,
                    "trigger": trigger,
                    "samples": [item["sample_id"] for item in candidates],
                }
            )[:24]
            token_balance = int(tx.token_state()["balance_units"])
            sensitive = self._detect_precision_sensitivity(
                tx,
                candidates,
                clock_ms=clock_ms,
                round_id=round_id,
                token_balance_units=token_balance,
            )
            budget = self.config.partitions.round_time_budget_ms
            round_limit = self.config.partitions.flush_round_limit
            spent = 0
            persisted_count = 0
            buffered_count = 0
            abandoned_count = 0
            deferred_count = 0
            budget_exhausted_at: int | None = None

            for index, sample in enumerate(candidates):
                if persisted_count >= round_limit:
                    for deferred in candidates[index:]:
                        tx.record_attempt(
                            round_id=round_id,
                            sample=deferred,
                            start_state="NOT_STARTED",
                            outcome="DEFERRED_ROUND_LIMIT",
                            terminal_state="BUFFERED",
                            reason="ROUND_PERSIST_LIMIT_REACHED",
                            budget_before_ms=budget - spent,
                            budget_after_ms=budget - spent,
                        )
                        deferred_count += 1
                    break

                required_units = int(sample["sample_bytes"]) * self._quota_scale
                availability = tx.quota_availability(sample["tenant"], clock_ms)
                token_balance = int(tx.token_state()["balance_units"])
                quota_available = int(availability["available_units"])
                first_constraint: dict[str, Any] | None = None
                if quota_available < required_units:
                    first_constraint = {
                        "constraint": "tenant_quota",
                        "tenant": sample["tenant"],
                        "window_id": int(availability["window_id"]),
                        "required_units": required_units,
                        "available_units": quota_available,
                    }
                elif token_balance < self._token_cost_units:
                    first_constraint = {
                        "constraint": "global_token",
                        "required_units": self._token_cost_units,
                        "available_units": token_balance,
                    }
                if first_constraint is not None:
                    reason = (
                        "QUOTA_INSUFFICIENT"
                        if first_constraint["constraint"] == "tenant_quota"
                        else "TOKEN_INSUFFICIENT"
                    )
                    tx.record_attempt(
                        round_id=round_id,
                        sample=sample,
                        start_state="NOT_STARTED",
                        outcome="BUFFERED",
                        terminal_state="BUFFERED",
                        reason=reason,
                        required_units=required_units,
                        available_units=quota_available,
                        first_constraint=first_constraint,
                        budget_before_ms=budget - spent,
                        budget_after_ms=budget - spent,
                    )
                    tx.update_buffer_decision(sample["sample_id"], "BUFFERED", reason)
                    attempts.append(
                        {
                            "sample_id": sample["sample_id"],
                            "partition": int(sample["partition"]),
                            "start_state": "NOT_STARTED",
                            "outcome": "BUFFERED",
                            "reason": reason,
                            "first_constraint": first_constraint,
                        }
                    )
                    buffered_count += 1
                    continue

                sample_cost = int(sample["sample_period_ms"])
                if spent + sample_cost > budget:
                    budget_exhausted_at = index
                    for abandoned in candidates[index:]:
                        tx.record_attempt(
                            round_id=round_id,
                            sample=abandoned,
                            start_state="NOT_STARTED",
                            outcome="NOT_STARTED_BUDGET",
                            terminal_state="ABANDONED",
                            reason="ROUND_BUDGET_EXHAUSTED",
                            required_units=required_units,
                            available_units=quota_available,
                            budget_before_ms=budget - spent,
                            budget_after_ms=budget - spent,
                        )
                        tx.drop_sample(
                            abandoned["sample_id"],
                            reason="ROUND_BUDGET_EXHAUSTED",
                            decision="ABANDONED_BUDGET",
                        )
                        attempts.append(
                            {
                                "sample_id": abandoned["sample_id"],
                                "partition": int(abandoned["partition"]),
                                "start_state": "NOT_STARTED",
                                "outcome": "NOT_STARTED_BUDGET",
                                "terminal_state": "ABANDONED",
                                "reason": "ROUND_BUDGET_EXHAUSTED",
                            }
                        )
                        abandoned_count += 1
                    break

                tx.conn.execute(
                    "UPDATE samples SET decision='STARTED', updated_version=? "
                    "WHERE sample_id=?",
                    (tx.version, sample["sample_id"]),
                )
                spent += sample_cost
                data_record = {
                    "sample_id": sample["sample_id"],
                    "event_id": sample["event_id"],
                    "identity_hash": sample["identity_hash"],
                    "tenant": sample["tenant"],
                    "metric": sample["metric"],
                    "tags": json.loads(sample["tags_json"]),
                    "logical_time_ms": int(sample["logical_time_ms"]),
                    "sample_seq": int(sample["sample_seq"]),
                    "sample_bytes": int(sample["sample_bytes"]),
                    "sample_period_ms": int(sample["sample_period_ms"]),
                    "partition": int(sample["partition"]),
                    "partition_arrival_seq": int(sample["partition_arrival_seq"]),
                    "global_order_key": sample["global_order_key"],
                    "flush_clock_ms": clock_ms,
                    "quota_window_id": int(availability["window_id"]),
                    "required_units": required_units,
                    "token_cost_units": self._token_cost_units,
                    "round_id": round_id,
                }
                append_result = self.data.append(int(sample["partition"]), data_record)
                failpoint = os.environ.get("METRIC_ENGINE_FAILPOINT")
                if failpoint == "after_data_append_before_commit":
                    os._exit(86)
                tx.charge_quota(
                    sample_id=sample["sample_id"],
                    tenant=sample["tenant"],
                    required_units=required_units,
                    logical_clock_ms=clock_ms,
                    sample_bytes=int(sample["sample_bytes"]),
                    scale=self._quota_scale,
                )
                if failpoint == "after_ledger_charge_before_commit":
                    os._exit(87)
                tx.charge_token(
                    sample_id=sample["sample_id"],
                    cost_units=self._token_cost_units,
                    logical_clock_ms=clock_ms,
                )
                tx.mark_persisted(
                    sample,
                    data_file=self.data.relative_path(int(sample["partition"])),
                    file_offset=int(append_result["offset"]),
                    record_hash=append_result["record_hash"],
                    quota_window_id=int(availability["window_id"]),
                    token_cost_units=self._token_cost_units,
                    logical_clock_ms=clock_ms,
                )
                tx.record_attempt(
                    round_id=round_id,
                    sample=sample,
                    start_state="STARTED",
                    outcome="PERSISTED",
                    terminal_state="PERSISTED",
                    reason=(
                        "DATA_RECORD_ALREADY_PRESENT"
                        if not append_result["appended"]
                        else None
                    ),
                    required_units=required_units,
                    available_units=quota_available,
                    budget_before_ms=budget - spent + sample_cost,
                    budget_after_ms=budget - spent,
                )
                attempts.append(
                    {
                        "sample_id": sample["sample_id"],
                        "partition": int(sample["partition"]),
                        "start_state": "STARTED",
                        "outcome": "PERSISTED",
                        "data_appended": bool(append_result["appended"]),
                    }
                )
                persisted_count += 1

            token_after = int(tx.token_state()["balance_units"])
            summary = {
                "round_id": round_id,
                "trigger": trigger,
                "clock_ms": clock_ms,
                "candidate_count": len(candidates),
                "persisted_count": persisted_count,
                "buffered_count": buffered_count,
                "abandoned_count": abandoned_count,
                "deferred_count": deferred_count,
                "budget_exhausted_at": budget_exhausted_at,
                "frozen_windows": frozen,
                "precision_sensitive": sensitive,
                "attempts": attempts,
            }
            tx.record_round(
                round_id=round_id,
                trigger=trigger,
                logical_clock_ms=clock_ms,
                budget_ms=budget,
                spent_ms=spent,
                candidate_count=len(candidates),
                persisted_count=persisted_count,
                buffered_count=buffered_count,
                abandoned_count=abandoned_count,
                deferred_count=deferred_count,
                token_balance_after_units=token_after,
                summary=summary,
            )
            version = tx.version
        result = {
            **summary,
            "version": version,
            "token_balance_after_units": token_after,
        }
        self.store.refresh_derived_files()
        self.store.append_log(
            {
                "operation": "flush",
                "round_id": round_id,
                "version": version,
                "clock_ms": clock_ms,
                "persisted_count": persisted_count,
                "buffered_count": buffered_count,
                "abandoned_count": abandoned_count,
            }
        )
        return result

    def flush(
        self,
        *,
        clock_ms: int,
        trigger: str = "manual",
        rounds: int = 1,
    ) -> dict[str, Any]:
        if rounds <= 0:
            fail("E_BAD_ROUNDS", "rounds must be positive", rounds=rounds)
        results = []
        for _ in range(rounds):
            result = self._flush_round(clock_ms, trigger)
            results.append(result)
            if result["persisted_count"] == 0:
                break
        return {
            "ok": True,
            "operation": "flush",
            "clock_ms": clock_ms,
            "rounds": results,
            "persisted_count": sum(item["persisted_count"] for item in results),
            "version": self.store.current_version(),
        }

    def resume(self, *, clock_ms: int | None = None) -> dict[str, Any]:
        buffer_health = self.store.buffer_snapshot_health(cleanup=True)
        scans: list[dict[str, Any]] = []
        data_records: list[dict[str, Any]] = []
        partial_total = 0
        for partition in range(self.config.partitions.partition_count):
            scan = self.data.scan(partition, repair=True)
            partial_total += int(scan["partial_bytes"])
            scans.append(
                {
                    "partition": partition,
                    "record_count": len(scan["records"]),
                    "partial_bytes_repaired": int(scan["partial_bytes"]),
                }
            )
            data_records.extend(scan["records"])

        with self.store.transaction("resume") as tx:
            watermark = tx.logical_clock()
            record_clock = max(
                [int(record["flush_clock_ms"]) for record in data_records] or [0]
            )
            effective_clock = max(watermark, record_clock, int(clock_ms or 0))
            tx.set_clock(effective_clock)
            tx.freeze_windows(effective_clock)
            recovery_round = "recovery-" + stable_hash(
                {
                    "version": tx.version,
                    "clock_ms": effective_clock,
                    "samples": sorted(record["sample_id"] for record in data_records),
                }
            )[:20]
            seen: set[str] = set()
            duplicate_count = 0
            redo_count = 0
            already_committed = 0
            conflicts: list[dict[str, Any]] = []

            sorted_records = sorted(
                data_records,
                key=lambda item: (
                    int(item["flush_clock_ms"]),
                    str(item["global_order_key"]),
                ),
            )
            for record in sorted_records:
                sample_id = str(record["sample_id"])
                if sample_id in seen:
                    duplicate_count += 1
                    tx.record_recovery(
                        action="IGNORE_DUPLICATE_DATA_RECORD",
                        sample_id=sample_id,
                        partition=int(record["partition"]),
                        detail={"record_hash": record["record_hash"]},
                        logical_clock_ms=effective_clock,
                    )
                    continue
                seen.add(sample_id)
                sample = tx.sample_by_id(sample_id)
                if sample is None:
                    conflicts.append(
                        {"sample_id": sample_id, "reason": "unknown_sample_in_data_file"}
                    )
                    continue
                if tx.persisted_sample(sample_id) is not None:
                    already_committed += 1
                    continue
                tx.charge_quota(
                    sample_id=sample_id,
                    tenant=sample["tenant"],
                    required_units=int(record["required_units"]),
                    logical_clock_ms=int(record["flush_clock_ms"]),
                    sample_bytes=int(sample["sample_bytes"]),
                    scale=self._quota_scale,
                )
                tx.charge_token(
                    sample_id=sample_id,
                    cost_units=int(record["token_cost_units"]),
                    logical_clock_ms=int(record["flush_clock_ms"]),
                )
                tx.mark_persisted(
                    sample,
                    data_file=self.data.relative_path(int(sample["partition"])),
                    file_offset=int(record["_offset"]),
                    record_hash=str(record["record_hash"]),
                    quota_window_id=int(record["quota_window_id"]),
                    token_cost_units=int(record["token_cost_units"]),
                    logical_clock_ms=int(record["flush_clock_ms"]),
                )
                tx.record_attempt(
                    round_id=recovery_round,
                    sample=sample,
                    start_state="STARTED",
                    outcome="PERSISTED_THEN_FAILED",
                    terminal_state="PERSISTED_AFTER_RECOVERY",
                    reason="DATA_APPENDED_LEDGER_ROLLED_BACK",
                    required_units=int(record["required_units"]),
                    budget_before_ms=None,
                    budget_after_ms=None,
                )
                tx.record_recovery(
                    action="REDO_MISSING_SAMPLE",
                    sample_id=sample_id,
                    partition=int(record["partition"]),
                    detail={
                        "record_hash": record["record_hash"],
                        "quota_window_id": int(record["quota_window_id"]),
                        "token_cost_units": int(record["token_cost_units"]),
                    },
                    logical_clock_ms=effective_clock,
                )
                redo_count += 1
            tx.advance_tokens(effective_clock)
            if conflicts:
                fail(
                    "E_RECOVERY_CONFLICT",
                    "recovery found data records without matching buffered samples",
                    conflicts=conflicts,
                )
            if partial_total:
                tx.record_recovery(
                    action="TRUNCATE_PARTIAL_DATA_TAIL",
                    sample_id=None,
                    partition=None,
                    detail={"bytes": partial_total},
                    logical_clock_ms=effective_clock,
                )
            version = tx.version
        self.store.refresh_derived_files()
        report = {
            "ok": True,
            "operation": "resume",
            "version": version,
            "logical_clock_ms": effective_clock,
            "scanned_partitions": scans,
            "partial_bytes_repaired": partial_total,
            "duplicate_data_records": duplicate_count,
            "already_committed_samples": already_committed,
            "redone_samples": redo_count,
            "stale_buffer_snapshots": buffer_health["stale_snapshots"],
            "temporary_buffer_files_removed": buffer_health["temporary_files"],
            "buffer_snapshots_regenerated": True,
        }
        self.store.append_log(
            {
                "operation": "resume",
                "version": version,
                "logical_clock_ms": effective_clock,
                "redone_samples": redo_count,
                "duplicate_data_records": duplicate_count,
            }
        )
        return report

    def status(self) -> dict[str, Any]:
        conn = self.store._connect()
        try:
            pointer = conn.execute("SELECT * FROM ledger_pointer WHERE id=1").fetchone()
            clock = int(
                conn.execute("SELECT value FROM meta WHERE key='logical_clock_ms'").fetchone()[
                    "value"
                ]
            )
            token = conn.execute("SELECT * FROM token_state WHERE id=1").fetchone()
            token_scale = int(token["fixed_scale"])
            partitions = [
                {
                    "partition": int(row["partition"]),
                    "arrival_seq": int(row["arrival_seq"]),
                    "buffer_depth": int(row["buffer_depth"]),
                    "buffer_digest": row["buffer_digest"],
                }
                for row in conn.execute("SELECT * FROM partitions ORDER BY partition")
            ]
            tenant_rows = []
            tenants = [
                row["tenant"]
                for row in conn.execute(
                    "SELECT tenant FROM tenant_quotas ORDER BY tenant"
                )
            ]
            for tenant in tenants:
                declaration = conn.execute(
                    "SELECT * FROM tenant_quotas WHERE tenant=?", (tenant,)
                ).fetchone()
                windows = []
                for row in conn.execute(
                    """
                    SELECT * FROM quota_windows WHERE tenant=?
                    ORDER BY window_id
                    """,
                    (tenant,),
                ):
                    windows.append(
                        {
                            "window_id": int(row["window_id"]),
                            "window_start_ms": int(row["window_start_ms"]),
                            "window_end_ms": int(row["window_end_ms"]),
                            "status": row["status"],
                            "used_units": int(row["used_units"]),
                            "used_bytes": format(
                                units_to_decimal(
                                    int(row["used_units"]),
                                    int(declaration["fixed_scale"]),
                                ),
                                "f",
                            ),
                            "remaining_units": max(
                                0,
                                int(row["limit_units"])
                                + int(row["borrow_limit_units"])
                                - int(row["used_units"]),
                            ),
                            "borrowed_units": int(row["borrowed_units"]),
                            "borrowed_bytes": format(
                                units_to_decimal(
                                    int(row["borrowed_units"]),
                                    int(declaration["fixed_scale"]),
                                ),
                                "f",
                            ),
                            "return_obligation_units": int(
                                row["return_obligation_units"]
                            ),
                            "frozen_remaining_units": row["frozen_remaining_units"],
                        }
                    )
                used_total = sum(item["used_units"] for item in windows)
                borrowed_total = sum(item["borrowed_units"] for item in windows)
                tenant_rows.append(
                    {
                        "tenant": tenant,
                        "used_units_total": used_total,
                        "used_bytes_total": format(
                            units_to_decimal(
                                used_total, int(declaration["fixed_scale"])
                            ),
                            "f",
                        ),
                        "borrowed_units_total": borrowed_total,
                        "borrowed_bytes_total": format(
                            units_to_decimal(
                                borrowed_total, int(declaration["fixed_scale"])
                            ),
                            "f",
                        ),
                        "windows": windows,
                    }
                )
            sensitive = []
            for row in conn.execute(
                """
                SELECT * FROM precision_sensitive ORDER BY id
                """
            ):
                sensitive.append(
                    {
                        "round_id": row["round_id"],
                        "kind": row["kind"],
                        "entity_a": row["entity_a"],
                        "entity_b": row["entity_b"],
                        "availability_a_units": int(row["availability_a_units"]),
                        "availability_b_units": int(row["availability_b_units"]),
                        "delta_units": int(row["delta_units"]),
                        "tolerance_units": int(row["tolerance_units"]),
                        "decision_depends": bool(row["decision_depends"]),
                        "sources": json.loads(row["sources_json"]),
                        "logical_clock_ms": int(row["logical_clock_ms"]),
                        "version": int(row["version"]),
                    }
                )
            total_buffer = sum(item["buffer_depth"] for item in partitions)
            result = {
                "ok": True,
                "operation": "status",
                "version": int(pointer["version"]),
                "logical_clock_ms": clock,
                "token": {
                    "balance_units": int(token["balance_units"]),
                    "balance_tokens": format(
                        units_to_decimal(int(token["balance_units"]), token_scale), "f"
                    ),
                    "capacity_units": int(token["capacity_units"]),
                    "capacity_tokens": format(
                        units_to_decimal(int(token["capacity_units"]), token_scale), "f"
                    ),
                    "last_refill_ms": int(token["last_refill_ms"]),
                    "fixed_scale": token_scale,
                },
                "buffer_depth": total_buffer,
                "partitions": partitions,
                "tenants": tenant_rows,
                "precision_sensitive": sensitive,
            }
        finally:
            conn.close()
        return json.loads(canonical_json(result))

    def verify(self) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        conn = self.store._connect()
        try:
            pointer = conn.execute("SELECT * FROM ledger_pointer WHERE id=1").fetchone()
            versions = conn.execute(
                "SELECT version, state_sha256, state_json FROM state_versions ORDER BY version"
            ).fetchall()
            if not versions:
                errors.append({"check": "versions", "reason": "no state versions"})
            else:
                expected = 1
                for row in versions:
                    if int(row["version"]) != expected:
                        errors.append(
                            {
                                "check": "versions",
                                "reason": "version gap",
                                "expected": expected,
                                "found": int(row["version"]),
                            }
                        )
                    if stable_hash(json.loads(row["state_json"])) != row["state_sha256"]:
                        errors.append(
                            {
                                "check": "versions",
                                "reason": "state snapshot hash mismatch",
                                "version": int(row["version"]),
                            }
                        )
                    expected += 1
                if int(pointer["version"]) != int(versions[-1]["version"]):
                    errors.append(
                        {
                            "check": "versions",
                            "reason": "ledger pointer is not the newest version",
                            "pointer": int(pointer["version"]),
                            "newest": int(versions[-1]["version"]),
                        }
                    )
                if pointer["snapshot_sha256"] != versions[-1]["state_sha256"]:
                    errors.append(
                        {
                            "check": "versions",
                            "reason": "ledger pointer snapshot hash mismatch",
                        }
                    )

            persisted_rows = {
                row["sample_id"]: dict(row)
                for row in conn.execute("SELECT * FROM persisted_samples")
            }
            persisted_state = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM samples WHERE state='PERSISTED'"
                ).fetchone()["n"]
            )
            if persisted_state != len(persisted_rows):
                errors.append(
                    {
                        "check": "persisted",
                        "reason": "persisted sample and ledger counts differ",
                        "state": persisted_state,
                        "ledger": len(persisted_rows),
                    }
                )
            quota_charge_count = int(
                conn.execute("SELECT COUNT(*) AS n FROM quota_charges").fetchone()["n"]
            )
            token_charge_count = int(
                conn.execute("SELECT COUNT(*) AS n FROM token_charges").fetchone()["n"]
            )
            if quota_charge_count != len(persisted_rows):
                errors.append(
                    {
                        "check": "quota",
                        "reason": "quota charge count does not match persisted samples",
                        "charges": quota_charge_count,
                        "persisted": len(persisted_rows),
                    }
                )
            if token_charge_count != len(persisted_rows):
                errors.append(
                    {
                        "check": "token",
                        "reason": "token charge count does not match persisted samples",
                        "charges": token_charge_count,
                        "persisted": len(persisted_rows),
                    }
                )
            token = conn.execute("SELECT * FROM token_state WHERE id=1").fetchone()
            if not 0 <= int(token["balance_units"]) <= int(token["capacity_units"]):
                errors.append(
                    {
                        "check": "token",
                        "reason": "token balance is outside capacity",
                        "balance_units": int(token["balance_units"]),
                        "capacity_units": int(token["capacity_units"]),
                    }
                )
            for window in conn.execute("SELECT * FROM quota_windows ORDER BY tenant, window_id"):
                used = int(
                    conn.execute(
                        """
                        SELECT COALESCE(SUM(required_units), 0) AS n FROM quota_charges
                        WHERE tenant=? AND window_id=?
                        """,
                        (window["tenant"], window["window_id"]),
                    ).fetchone()["n"]
                )
                borrowed = int(
                    conn.execute(
                        """
                        SELECT COALESCE(SUM(borrowed_units), 0) AS n FROM borrow_ledger
                        WHERE tenant=? AND window_id=?
                        """,
                        (window["tenant"], window["window_id"]),
                    ).fetchone()["n"]
                )
                if used != int(window["used_units"]):
                    errors.append(
                        {
                            "check": "quota",
                            "reason": "window used bytes differ from charge ledger",
                            "tenant": window["tenant"],
                            "window_id": int(window["window_id"]),
                            "window": int(window["used_units"]),
                            "charges": used,
                        }
                    )
                if borrowed != int(window["borrowed_units"]):
                    errors.append(
                        {
                            "check": "borrow",
                            "reason": "window borrowed bytes differ from borrow ledger",
                            "tenant": window["tenant"],
                            "window_id": int(window["window_id"]),
                            "window": int(window["borrowed_units"]),
                            "ledger": borrowed,
                        }
                    )
                if window["status"] == "FROZEN":
                    expected_remaining = max(
                        0,
                        int(window["limit_units"])
                        + int(window["borrow_limit_units"])
                        - int(window["used_units"]),
                    )
                    if window["frozen_remaining_units"] != expected_remaining:
                        errors.append(
                            {
                                "check": "quota_freeze",
                                "reason": "frozen remaining bytes are inconsistent",
                                "tenant": window["tenant"],
                                "window_id": int(window["window_id"]),
                            }
                        )

            data_ids_by_partition: dict[int, set[str]] = {}
            for partition in range(self.config.partitions.partition_count):
                scan = self.data.scan(partition, repair=False)
                if int(scan["partial_bytes"]):
                    errors.append(
                        {
                            "check": "data",
                            "reason": "partial data tail requires recovery",
                            "partition": partition,
                            "partial_bytes": int(scan["partial_bytes"]),
                        }
                    )
                ids: set[str] = set()
                for record in scan["records"]:
                    sample_id = record["sample_id"]
                    if sample_id in ids:
                        errors.append(
                            {
                                "check": "data",
                                "reason": "duplicate data record",
                                "partition": partition,
                                "sample_id": sample_id,
                            }
                        )
                    ids.add(sample_id)
                    if sample_id not in persisted_rows:
                        errors.append(
                            {
                                "check": "data",
                                "reason": "data record has no committed ledger entry",
                                "partition": partition,
                                "sample_id": sample_id,
                            }
                        )
                data_ids_by_partition[partition] = ids
            for sample_id, row in persisted_rows.items():
                if sample_id not in data_ids_by_partition.get(int(row["partition"]), set()):
                    errors.append(
                        {
                            "check": "data",
                            "reason": "committed sample is missing from data file",
                            "partition": int(row["partition"]),
                            "sample_id": sample_id,
                        }
                    )

            for partition in range(self.config.partitions.partition_count):
                row = conn.execute(
                    "SELECT * FROM partitions WHERE partition=?", (partition,)
                ).fetchone()
                seqs = [
                    int(item["partition_arrival_seq"])
                    for item in conn.execute(
                        "SELECT partition_arrival_seq FROM samples WHERE partition=? "
                        "ORDER BY partition_arrival_seq",
                        (partition,),
                    )
                ]
                if seqs != list(range(1, int(row["arrival_seq"]) + 1)):
                    errors.append(
                        {
                            "check": "arrival_seq",
                            "reason": "partition arrival sequence is not contiguous",
                            "partition": partition,
                            "arrival_seq": int(row["arrival_seq"]),
                            "observed": len(seqs),
                        }
                    )
                buffered_ids = [
                    item["sample_id"]
                    for item in conn.execute(
                        "SELECT sample_id FROM samples WHERE state='BUFFERED' "
                        "AND partition=? ORDER BY tenant, logical_time_ms, sample_seq, "
                        "partition_arrival_seq, sample_id",
                        (partition,),
                    )
                ]
                if stable_hash(buffered_ids) != row["buffer_digest"]:
                    errors.append(
                        {
                            "check": "buffer",
                            "reason": "buffer digest mismatch",
                            "partition": partition,
                        }
                    )

            valid_register_ids = {
                int(row["id"]) for row in conn.execute("SELECT id FROM precision_ops")
            }
            for row in conn.execute("SELECT * FROM precision_sensitive ORDER BY id"):
                chains = json.loads(row["sources_json"])
                for chain in chains.values():
                    if not isinstance(chain, list):
                        continue
                    for source in chain:
                        if int(source.get("register_id", -1)) not in valid_register_ids:
                            errors.append(
                                {
                                    "check": "precision",
                                    "reason": "sensitivity source register is missing",
                                    "sensitivity_id": int(row["id"]),
                                    "register_id": source.get("register_id"),
                                }
                            )

            manifest = [
                {
                    "sample_id": row["sample_id"],
                    "tenant": row["tenant"],
                    "metric": row["metric"],
                    "partition": int(row["partition"]),
                    "partition_arrival_seq": int(row["partition_arrival_seq"]),
                    "global_order_key": row["global_order_key"],
                    "state": row["state"],
                }
                for row in conn.execute(
                    "SELECT sample_id, tenant, metric, partition, partition_arrival_seq, "
                    "global_order_key, state FROM samples ORDER BY global_order_key"
                )
            ]
            result = {
                "ok": not errors,
                "operation": "verify",
                "version": int(pointer["version"]),
                "persisted_samples": len(persisted_rows),
                "buffered_samples": int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM samples WHERE state='BUFFERED'"
                    ).fetchone()["n"]
                ),
                "dropped_samples": int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM samples WHERE state='DROPPED'"
                    ).fetchone()["n"]
                ),
                "token_balance_units": int(token["balance_units"]),
                "manifest_digest": stable_hash(manifest),
                "manifest": manifest,
                "errors": errors,
            }
        finally:
            conn.close()
        if errors:
            fail(
                "E_VERIFY_FAILED",
                "ledger verification failed",
                errors=errors,
                verify=result,
            )
        return json.loads(canonical_json(result))

