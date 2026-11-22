from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .cost import ReoptDecision, decide_reopt
from .errors import EngineError
from .generator import Batch, DeterministicGenerator
from .join import RuntimeData, canonicalize_rows, execute_shape, rows_digest
from .models import BatchSpec, SampleSpec, Shape
from .observation import Observation, build_observation
from .storage import BatchRecord, RuntimeState, Storage


@dataclass(frozen=True)
class CommitResult:
    committed: bool
    idempotent: bool
    batch: dict[str, Any] | None
    observation: dict[str, Any] | None
    drift: dict[str, Any] | None
    reopt: dict[str, Any] | None
    error_code: str | None
    state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "committed": self.committed,
            "idempotent": self.idempotent,
            "batch": self.batch,
            "observation": self.observation,
            "drift": self.drift,
            "reopt": self.reopt,
            "error_code": self.error_code,
            "state": self.state,
            "ok": self.error_code is None,
        }


class Engine:
    def __init__(self, sample: SampleSpec, db_path: str | Path):
        self.sample = sample
        self.db_path = Path(db_path)
        self.generator = DeterministicGenerator(sample)
        self.storage = Storage(self.db_path)
        self.storage.initialize(sample)

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _load_runtime(self, tx: sqlite3.Connection) -> tuple[RuntimeState, RuntimeData, list[BatchRecord], list[dict[str, Any]]]:
        state = self.storage.state(tx)
        records = self.storage.batch_records(tx)
        if len(records) != state.committed_count:
            raise EngineError(
                "E_CORRUPT_STATE",
                "runtime watermark does not match committed batch count",
                {"watermark_count": state.committed_count, "batch_rows": len(records)},
            )
        data = RuntimeData()
        summaries: list[dict[str, Any]] = []
        for ordinal, record in enumerate(records):
            if record.ordinal != ordinal:
                raise EngineError(
                    "E_CORRUPT_STATE",
                    "committed batch ordinals are not contiguous",
                    {"expected": ordinal, "actual": record.ordinal},
                )
            expected_spec = self.generator.batch_specs[ordinal]
            if record.arrival != expected_spec.arrival or record.table_name != expected_spec.table:
                raise EngineError(
                    "E_CORRUPT_STATE",
                    "committed batch does not match plan order",
                    {"ordinal": ordinal, "expected": expected_spec.__dict__, "actual": record.__dict__},
                )
            batch = self.generator.batch(expected_spec)
            if batch.digest != record.digest or batch.row_count != record.row_count:
                raise EngineError(
                    "E_CORRUPT_STATE",
                    "committed batch digest does not match deterministic regeneration",
                    {"arrival": record.arrival, "stored": record.digest, "regenerated": batch.digest},
                )
            data.append_batch(batch.table, batch.columns, batch.row_count)
            summaries.append(batch.summary())
        return state, data, records, summaries

    def _result_for(self, data: RuntimeData, shape: Shape) -> list[dict[str, Any]]:
        return execute_shape(data, shape, self.sample.plan.joins)

    def _update_accumulator(
        self,
        previous: dict[str, Any],
        observation: Observation,
    ) -> dict[str, Any]:
        accumulator = json.loads(json.dumps(previous, sort_keys=True))
        for source in observation.drift_sources:
            table = source["source"]["table"]
            column = source["source"]["join_key"]
            key = f"{table}.{column}"
            entry = accumulator.get(key)
            if entry is None:
                accumulator[key] = {
                    "source": source["source"],
                    "first_seq": observation.seq,
                    "last_seq": observation.seq,
                    "first_arrival": observation.arrival,
                    "last_arrival": observation.arrival,
                    "observations": 1,
                    "max_drift_ratio": source["drift_ratio"],
                    "latest_drift_ratio": source["drift_ratio"],
                    "max_error_bound": source["error_bound"],
                }
            else:
                entry["last_seq"] = observation.seq
                entry["last_arrival"] = observation.arrival
                entry["observations"] += 1
                entry["latest_drift_ratio"] = source["drift_ratio"]
                entry["max_drift_ratio"] = max(entry["max_drift_ratio"], source["drift_ratio"])
                entry["max_error_bound"] = max(entry["max_error_bound"], source["error_bound"])
        return accumulator

    def _decision(
        self,
        state: RuntimeState,
        data: RuntimeData,
        observation: Observation,
        sources: list[dict[str, Any]],
    ) -> ReoptDecision:
        row_counts = {table.name: data.row_count(table.name) for table in self.sample.tables}
        committed = self.generator.batch_specs[: observation.committed_count]
        interval = (committed[0].arrival, committed[-1].arrival) if committed else None
        return decide_reopt(
            self.sample,
            state.shape,
            row_counts,
            observation.edge_selectivities,
            sum(row_counts.values()),
            interval,
            sources,
        )

    def _commit_loaded_batch(
        self,
        tx: sqlite3.Connection,
        state: RuntimeState,
        data: RuntimeData,
        spec: BatchSpec,
        crash_hook: Callable[[], None] | None,
    ) -> CommitResult:
        batch = self.generator.batch(spec)
        next_data = data.clone()
        next_data.append_batch(batch.table, batch.columns, batch.row_count)
        seq = state.committed_count + 1
        result_rows = self._result_for(next_data, state.shape)
        observation = build_observation(
            self.sample,
            next_data,
            state.shape,
            self.generator.batch_specs[:seq],
            result_rows,
            state.shape_version,
            seq,
        )
        clock_ms = seq * self.sample.config.clock_step_ms
        candidates = [
            source
            for source in observation.drift_sources
            if source["drift_ratio"] > self.sample.config.drift_threshold
        ]
        accumulator = self._update_accumulator(state.drift_accumulator, observation)
        drift_event: dict[str, Any] | None = None
        reopt_event: dict[str, Any] | None = None
        new_shape = state.shape
        new_version = state.shape_version
        last_reopt_clock = state.last_reopt_clock_ms
        precision_sensitive = state.precision_sensitive
        decision_error: str | None = None
        final_rows = result_rows
        final_digest = rows_digest(result_rows)

        if candidates:
            cooldown_elapsed = clock_ms - state.last_reopt_clock_ms >= self.sample.config.cooldown_ms
            if cooldown_elapsed:
                decision = self._decision(state, next_data, observation, candidates)
                decision_error = decision.error_code
                precision_sensitive = precision_sensitive or decision.precision_sensitive
                last_reopt_clock = clock_ms
                rejected = list(decision.rejected_candidates)
                rejected.extend(
                    {
                        "source": source["source"],
                        "drift_ratio": source["drift_ratio"],
                        "reason": "secondary_drift_source",
                    }
                    for source in candidates[1:]
                )
                drift_event = {
                    "seq": seq,
                    "arrival": spec.arrival,
                    "trigger": True,
                    "clock_ms": clock_ms,
                    "sources": candidates,
                    "rejected": rejected,
                    "decision": "triggered",
                    "error_code": decision.error_code,
                    "details": {
                        "drift_threshold": self.sample.config.drift_threshold,
                        "cooldown_ms": self.sample.config.cooldown_ms,
                        "logical_clock_ms": clock_ms,
                        "previous_reopt_clock_ms": state.last_reopt_clock_ms,
                        "observation_batch_interval": [1, spec.arrival],
                    },
                }
                self.storage.insert_drift_event(
                    tx,
                    seq=seq,
                    arrival=spec.arrival,
                    trigger=True,
                    clock_ms=clock_ms,
                    sources=candidates,
                    rejected=rejected,
                    decision="triggered",
                    error_code=decision.error_code,
                    details=drift_event["details"],
                )
                if decision.outcome == "switched":
                    new_shape = decision.to_shape
                    new_version = state.shape_version + 1
                    switched_rows = self._result_for(next_data, new_shape)
                    final_digest = rows_digest(switched_rows)
                    if final_digest != observation.result_digest:
                        raise EngineError(
                            "E_CORRUPT_STATE",
                            "re-optimized shape changed canonical join rows",
                            {
                                "arrival": spec.arrival,
                                "before_digest": observation.result_digest,
                                "after_digest": final_digest,
                            },
                        )
                    final_rows = switched_rows
                    self.storage.insert_shape_history(
                        tx,
                        version=new_version,
                        seq=seq,
                        arrival=spec.arrival,
                        shape=new_shape,
                        reason="runtime_drift",
                    )
                self.storage.insert_reopt_event(
                    tx,
                    seq=seq,
                    arrival=spec.arrival,
                    from_version=state.shape_version,
                    to_version=new_version,
                    from_shape=state.shape,
                    to_shape=new_shape,
                    trigger_sources=candidates,
                    rejected_candidates=decision.rejected_candidates,
                    switch_cost=decision.switch_cost,
                    cost_before=decision.current_estimate.cost,
                    cost_after=decision.chosen_estimate.cost,
                    precision_sensitive=decision.precision_sensitive,
                    error_chain=decision.error_chain,
                    outcome=decision.outcome,
                    error_code=decision.error_code,
                )
                reopt_event = {
                    "seq": seq,
                    "arrival": spec.arrival,
                    "from_version": state.shape_version,
                    "to_version": new_version,
                    "version_changed": state.shape_version != new_version,
                    "from_shape": state.shape.to_dict(),
                    "to_shape": new_shape.to_dict(),
                    "switch_cost": decision.switch_cost,
                    "cost_before": decision.current_estimate.cost,
                    "cost_after": decision.chosen_estimate.cost,
                    "precision_sensitive": decision.precision_sensitive,
                    "outcome": decision.outcome,
                    "error_code": decision.error_code,
                }
            else:
                rejected = [
                    {
                        "source": source["source"],
                        "drift_ratio": source["drift_ratio"],
                        "reason": "cooldown",
                        "available_at_clock_ms": state.last_reopt_clock_ms
                        + self.sample.config.cooldown_ms,
                    }
                    for source in candidates
                ]
                drift_event = {
                    "seq": seq,
                    "arrival": spec.arrival,
                    "trigger": False,
                    "clock_ms": clock_ms,
                    "sources": candidates,
                    "rejected": rejected,
                    "decision": "cooldown_suppressed",
                    "error_code": None,
                    "details": {
                        "drift_threshold": self.sample.config.drift_threshold,
                        "cooldown_ms": self.sample.config.cooldown_ms,
                        "logical_clock_ms": clock_ms,
                        "last_reopt_clock_ms": state.last_reopt_clock_ms,
                        "observation_batch_interval": [1, spec.arrival],
                    },
                }
                self.storage.insert_drift_event(
                    tx,
                    seq=seq,
                    arrival=spec.arrival,
                    trigger=False,
                    clock_ms=clock_ms,
                    sources=candidates,
                    rejected=rejected,
                    decision="cooldown_suppressed",
                    error_code=None,
                    details=drift_event["details"],
                )

        self.storage.insert_batch(
            tx,
            spec.arrival,
            state.committed_count,
            batch.table,
            batch.row_count,
            batch.digest,
            batch.column_digests,
        )
        self.storage.insert_observation(
            tx,
            seq=seq,
            arrival=spec.arrival,
            committed_count=seq,
            input_rows=observation.input_rows,
            output_rows=observation.output_rows,
            observed_selectivity=observation.observed_selectivity,
            drift_sources=observation.drift_sources,
            error_chain=observation.error_chain,
            result_digest=final_digest,
            shape_version=state.shape_version,
        )
        self.storage.update_state(
            tx,
            watermark_arrival=spec.arrival,
            committed_count=seq,
            shape_version=new_version,
            shape=new_shape,
            last_reopt_clock_ms=last_reopt_clock,
            drift_accumulator=accumulator,
            precision_sensitive=precision_sensitive,
            result_digest=final_digest,
            result_rows=len(final_rows),
        )
        if crash_hook is not None:
            crash_hook()
        new_state = RuntimeState(
            watermark_arrival=spec.arrival,
            committed_count=seq,
            shape_version=new_version,
            shape=new_shape,
            last_reopt_clock_ms=last_reopt_clock,
            drift_accumulator=accumulator,
            precision_sensitive=precision_sensitive,
            result_digest=final_digest,
            result_rows=len(final_rows),
        )
        return CommitResult(
            committed=True,
            idempotent=False,
            batch=batch.summary(),
            observation=observation.to_dict(),
            drift=drift_event,
            reopt=reopt_event,
            error_code=decision_error,
            state=new_state.to_dict(),
        )

    def commit_arrival(
        self,
        arrival: int,
        crash_hook: Callable[[], None] | None = None,
    ) -> CommitResult:
        def execute(tx: sqlite3.Connection) -> CommitResult:
            state, data, records, _ = self._load_runtime(tx)
            record = self.storage.batch_record(arrival, tx)
            if record is not None:
                spec = self.generator.batch_by_arrival(arrival)
                batch = self.generator.batch(spec)
                if batch.digest != record.digest or batch.row_count != record.row_count:
                    raise EngineError(
                        "E_DUPLICATE_BATCH",
                        "batch arrival is already committed with a different digest",
                        {
                            "arrival": arrival,
                            "committed_digest": record.digest,
                            "submitted_digest": batch.digest,
                        },
                    )
                return CommitResult(
                    committed=False,
                    idempotent=True,
                    batch=batch.summary(),
                    observation=None,
                    drift=None,
                    reopt=None,
                    error_code=None,
                    state=state.to_dict(),
                )
            if state.committed_count >= len(self.generator.batch_specs):
                raise EngineError("E_NO_DATA", "all plan batches are already committed")
            expected = self.generator.batch_specs[state.committed_count]
            if arrival != expected.arrival:
                raise EngineError(
                    "E_OUT_OF_ORDER",
                    "batch is not the next uncommitted arrival",
                    {"expected_arrival": expected.arrival, "requested_arrival": arrival},
                )
            return self._commit_loaded_batch(tx, state, data, expected, crash_hook)

        with self.storage.transaction() as tx:
            return execute(tx)

    def advance_one(self, crash_arrival: int | None = None) -> CommitResult | None:
        with self.storage.transaction() as tx:
            state, data, _, _ = self._load_runtime(tx)
            if state.committed_count >= len(self.generator.batch_specs):
                return None
            spec = self.generator.batch_specs[state.committed_count]
            crash_hook = None
            if crash_arrival == spec.arrival:
                crash_hook = _hard_exit
            return self._commit_loaded_batch(tx, state, data, spec, crash_hook)

    def run(self, max_batches: int | None = None, crash_arrival: int | None = None) -> list[CommitResult]:
        results: list[CommitResult] = []
        while max_batches is None or len(results) < max_batches:
            result = self.advance_one(crash_arrival=crash_arrival)
            if result is None:
                break
            results.append(result)
            if result.error_code == "E_SWITCH_BUDGET":
                break
        return results

    def run_until(self, target_committed: int) -> list[CommitResult]:
        results: list[CommitResult] = []
        while True:
            state = self.storage.state()
            if state.committed_count >= target_committed:
                break
            result = self.advance_one()
            if result is None:
                break
            results.append(result)
            if result.error_code is not None:
                break
        return results

    def status(self, observation_limit: int | None = None) -> dict[str, Any]:
        with self.storage.read_snapshot() as tx:
            state = self.storage.state(tx)
            records = self.storage.batch_records(tx)
            observations = self.storage.observations(observation_limit, tx)
            drift_events = self.storage.drift_events(tx)
            reopt_events = self.storage.reopt_events(tx)
            shape_history = self.storage.shape_history(tx)
        batches = [
            {
                "arrival": record.arrival,
                "ordinal": record.ordinal,
                "table": record.table_name,
                "row_count": record.row_count,
                "digest": record.digest,
            }
            for record in records
        ]
        batches.sort(key=lambda item: item["arrival"])
        payload = {
            "ok": True,
            "sample_id": self.sample.sample_id,
            "query_id": self.sample.plan.query_id,
            "watermark": {
                "last_arrival": state.watermark_arrival,
                "committed_batches": state.committed_count,
                "total_batches": len(self.generator.batch_specs),
            },
            "shape_version": state.shape_version,
            "current_shape": state.shape.to_dict(),
            "last_reopt_clock_ms": state.last_reopt_clock_ms,
            "precision_sensitive": state.precision_sensitive,
            "result": {"rows": state.result_rows, "digest": state.result_digest},
            "drift_accumulator": state.drift_accumulator,
            "committed_batches": batches,
            "observations": observations,
            "drift_records": drift_events,
            "reopt_records": reopt_events,
            "shape_history": shape_history,
        }
        return payload

    def observe(self, limit: int | None = None) -> dict[str, Any]:
        with self.storage.read_snapshot() as tx:
            state = self.storage.state(tx)
            observations = self.storage.observations(limit, tx)
        return {
            "ok": True,
            "watermark": {
                "last_arrival": state.watermark_arrival,
                "committed_batches": state.committed_count,
            },
            "shape_version": state.shape_version,
            "observations": observations,
        }

    def force_reopt(self, force: bool = False) -> dict[str, Any]:
        def execute(tx: sqlite3.Connection) -> dict[str, Any]:
            state, data, _, _ = self._load_runtime(tx)
            if state.committed_count == 0:
                raise EngineError("E_NO_DATA", "no committed batches are available for re-optimization")
            current_clock = state.committed_count * self.sample.config.clock_step_ms
            sources = [
                {
                    "source": entry["source"],
                    "drift_ratio": entry["max_drift_ratio"],
                    "error_bound": entry["max_error_bound"],
                    "batch_interval": [entry["first_arrival"], entry["last_arrival"]],
                    "observed_batches": [entry["first_seq"], entry["last_seq"]],
                }
                for entry in state.drift_accumulator.values()
                if entry["max_drift_ratio"] > self.sample.config.drift_threshold
            ]
            sources.sort(
                key=lambda source: (
                    -source["drift_ratio"],
                    source["source"]["table"],
                    source["source"]["join_key"],
                )
            )
            if not sources and not force:
                raise EngineError("E_NO_DATA", "no accumulated drift exceeds the configured threshold")
            if not sources:
                sources = [
                    {
                        "source": {"table": self.sample.tables[0].name, "join_key": "*"},
                        "drift_ratio": 0.0,
                        "error_bound": 0.0,
                        "batch_interval": [1, state.watermark_arrival],
                        "observed_batches": [1, state.committed_count],
                        "reason": "manual_force",
                    }
                ]
            if not force and current_clock - state.last_reopt_clock_ms < self.sample.config.cooldown_ms:
                rejected = [
                    {
                        "source": source["source"],
                        "drift_ratio": source["drift_ratio"],
                        "reason": "cooldown",
                        "available_at_clock_ms": state.last_reopt_clock_ms
                        + self.sample.config.cooldown_ms,
                    }
                    for source in sources
                ]
                self.storage.insert_drift_event(
                    tx,
                    seq=state.committed_count,
                    arrival=state.watermark_arrival,
                    trigger=False,
                    clock_ms=current_clock,
                    sources=sources,
                    rejected=rejected,
                    decision="reopt_cooldown_suppressed",
                    error_code=None,
                    details={"manual": True, "force": force},
                )
                return {
                    "ok": True,
                    "outcome": "cooldown_suppressed",
                    "shape_version": state.shape_version,
                    "sources": sources,
                    "rejected": rejected,
                }

            observation = build_observation(
                self.sample,
                data,
                state.shape,
                self.generator.batch_specs[: state.committed_count],
                self._result_for(data, state.shape),
                state.shape_version,
                state.committed_count,
            )
            decision = self._decision(state, data, observation, sources)
            new_shape = state.shape
            new_version = state.shape_version
            precision = state.precision_sensitive or decision.precision_sensitive
            result_rows = self._result_for(data, state.shape)
            if decision.outcome == "switched":
                new_shape = decision.to_shape
                new_version += 1
                switched_rows = self._result_for(data, new_shape)
                if rows_digest(switched_rows) != rows_digest(result_rows):
                    raise EngineError("E_CORRUPT_STATE", "manual re-optimization changed canonical rows")
                result_rows = switched_rows
                self.storage.insert_shape_history(
                    tx,
                    version=new_version,
                    seq=state.committed_count,
                    arrival=state.watermark_arrival,
                    shape=new_shape,
                    reason="manual_reopt" if force else "runtime_drift",
                )
            self.storage.insert_drift_event(
                tx,
                seq=state.committed_count,
                arrival=state.watermark_arrival,
                trigger=True,
                clock_ms=current_clock,
                sources=sources,
                rejected=decision.rejected_candidates,
                decision="manual_reopt",
                error_code=decision.error_code,
                details={
                    "force": force,
                    "outcome": decision.outcome,
                    "observation_batch_interval": [1, state.watermark_arrival],
                },
            )
            self.storage.insert_reopt_event(
                tx,
                seq=state.committed_count,
                arrival=state.watermark_arrival,
                from_version=state.shape_version,
                to_version=new_version,
                from_shape=state.shape,
                to_shape=new_shape,
                trigger_sources=sources,
                rejected_candidates=decision.rejected_candidates,
                switch_cost=decision.switch_cost,
                cost_before=decision.current_estimate.cost,
                cost_after=decision.chosen_estimate.cost,
                precision_sensitive=decision.precision_sensitive,
                error_chain=decision.error_chain,
                outcome=decision.outcome,
                error_code=decision.error_code,
            )
            self.storage.update_state(
                tx,
                watermark_arrival=state.watermark_arrival,
                committed_count=state.committed_count,
                shape_version=new_version,
                shape=new_shape,
                last_reopt_clock_ms=current_clock,
                drift_accumulator=state.drift_accumulator,
                precision_sensitive=precision,
                result_digest=rows_digest(result_rows),
                result_rows=len(result_rows),
            )
            return {
                "ok": decision.error_code is None,
                "outcome": decision.outcome,
                "shape_version": new_version,
                "previous_shape_version": state.shape_version,
                "from_shape": state.shape.to_dict(),
                "to_shape": new_shape.to_dict(),
                "sources": sources,
                "precision_sensitive": decision.precision_sensitive,
                "error_code": decision.error_code,
                "switch_cost": decision.switch_cost,
                "rejected_candidates": decision.rejected_candidates,
            }

        with self.storage.transaction() as tx:
            return execute(tx)

    def verify(self) -> dict[str, Any]:
        with self.storage.read_snapshot() as tx:
            state = self.storage.state(tx)
            records = self.storage.batch_records(tx)
            observations = self.storage.observations(None, tx)
            history = self.storage.shape_history(tx)
        if state.committed_count != len(records):
            return {
                "ok": False,
                "error_code": "E_VERIFY_FAILED",
                "reason": "watermark and committed batch count differ",
                "committed_count": state.committed_count,
                "batch_records": len(records),
            }
        if len(observations) != state.committed_count:
            return {
                "ok": False,
                "error_code": "E_VERIFY_FAILED",
                "reason": "observation count and committed batch count differ",
                "observations": len(observations),
                "committed_count": state.committed_count,
            }
        shape_by_version = {
            item["version"]: Shape(tuple(item["shape"]["join_order"]), tuple(item["shape"]["algorithms"]))
            for item in history
        }
        data = RuntimeData()
        replay_checks: list[dict[str, Any]] = []
        for ordinal, record in enumerate(records):
            spec = self.generator.batch_specs[ordinal]
            generated = self.generator.batch(spec)
            if generated.digest != record.digest or generated.row_count != record.row_count:
                return {
                    "ok": False,
                    "error_code": "E_VERIFY_FAILED",
                    "reason": "stored batch digest mismatch",
                    "arrival": record.arrival,
                    "stored_digest": record.digest,
                    "regenerated_digest": generated.digest,
                }
            data.append_batch(generated.table, generated.columns, generated.row_count)
            observation = observations[ordinal]
            shape = shape_by_version[observation["shape_version"]]
            rows = self._result_for(data, shape)
            digest = rows_digest(rows)
            expected_input = sum(data.row_count(table.name) for table in self.sample.tables)
            if (
                len(rows) != observation["output_rows"]
                or digest != observation["result_digest"]
                or expected_input != observation["input_rows"]
            ):
                return {
                    "ok": False,
                    "error_code": "E_VERIFY_FAILED",
                    "reason": "observation replay mismatch",
                    "arrival": record.arrival,
                    "expected": {
                        "input_rows": expected_input,
                        "output_rows": len(rows),
                        "result_digest": digest,
                    },
                    "stored": {
                        "input_rows": observation["input_rows"],
                        "output_rows": observation["output_rows"],
                        "result_digest": observation["result_digest"],
                    },
                }
            replay_checks.append({"arrival": record.arrival, "output_rows": len(rows), "digest": digest})

        initial_rows = canonicalize_rows(self._result_for(data, self.sample.plan.initial_shape))
        current_shape_rows = canonicalize_rows(self._result_for(data, state.shape))
        first_mismatch: int | None = None
        mismatch: dict[str, Any] | None = None
        for index, (left, right) in enumerate(zip(initial_rows, current_shape_rows, strict=False)):
            if left != right:
                first_mismatch = index
                mismatch = {"left": left, "right": right}
                break
        row_by_row_match = (
            len(initial_rows) == len(current_shape_rows) and first_mismatch is None
        )
        current_digest = rows_digest(current_shape_rows)
        digest_match = (
            current_digest == state.result_digest
            and len(current_shape_rows) == state.result_rows
        )
        ok = row_by_row_match and digest_match and len(replay_checks) == state.committed_count
        return {
            "ok": ok,
            "error_code": None if ok else "E_VERIFY_FAILED",
            "watermark": {
                "last_arrival": state.watermark_arrival,
                "committed_batches": state.committed_count,
            },
            "shape_version": state.shape_version,
            "batch_digest_checks": len(replay_checks),
            "observation_replays": len(replay_checks),
            "truth": {
                "row_count": len(initial_rows),
                "digest": rows_digest(initial_rows),
            },
            "current_shape": {
                "row_count": len(current_shape_rows),
                "digest": current_digest,
            },
            "row_by_row_match": row_by_row_match,
            "first_mismatch_index": first_mismatch,
            "first_mismatch": mismatch,
            "persisted_digest_match": digest_match,
            "precision_sensitive": state.precision_sensitive,
        }


def _hard_exit() -> None:
    os.kill(os.getpid(), 9)
