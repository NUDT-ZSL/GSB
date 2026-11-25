from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from typing import Any

from .aggregate import (
    ExactTruthPath,
    WindowAggregate,
    canonical_window_result,
    estimate_memory_bytes,
    estimate_logical_ms,
    find_first_divergence,
    logical_elapsed_ms,
)
from .costmodel import WindowObservation, adjudicate_switch, evaluate_drift
from .errors import AggregateMismatchError, BusinessRuleError, ErrorCode, RadaptError
from .fixedpoint import ErrorLedger, frac_str
from .generator import BatchEnvelope, generate_batch, read_batch_file, verify_envelope
from .model import BatchSpec, Bundle, FormSpec
from .scheduler import ParallelScheduler, ScheduledTask, STATUS_COMPLETED
from .storage import RuntimeStorage


class RuntimeEngine:
    def __init__(
        self,
        bundle: Bundle,
        db_path: str | Path = ":memory:",
        *,
        sample_dir: str | Path | None = None,
        crash_after_batch: int | None = None,
        crash_mode: str = "sigkill",
        verify_truth_each_batch: bool = True,
        auto_close_at_end: bool = True,
    ) -> None:
        self.bundle = bundle
        self.sample_dir = Path(sample_dir) if sample_dir else None
        self.crash_after_batch = crash_after_batch
        self.crash_mode = crash_mode
        self.verify_truth_each_batch = verify_truth_each_batch
        self.auto_close_at_end = auto_close_at_end
        self.storage = RuntimeStorage(db_path)
        self.ledger = ErrorLedger(bundle.config.fixed_point_scale, bundle.config.rounding_mode)
        self.scheduler = ParallelScheduler(bundle.config)
        self._envelopes: dict[int, BatchEnvelope] = {}
        self._truth: dict[str, ExactTruthPath] = {}
        self.report: dict[str, Any] = {
            "processed_batches": [],
            "skipped_committed_batches": [],
            "observations": [],
            "drift_checks": [],
            "drift_events": [],
            "switch_decisions": [],
            "scheduler_rounds": [],
            "precision_flags": [],
            "finalizations": [],
            "business_error_codes": [],
            "truth_verification": [],
        }
        initial_form = bundle.plan.initial_form
        version = self.storage.initialize_form(initial_form, 0)
        self.report["initial_form_version"] = version
        self.report["recovery_snapshot_before_run"] = self.storage.recovery_snapshot()
        self.pending_drift = self._load_pending_drift()

    def close(self) -> None:
        self.storage.save_ledger(self.ledger.to_dict())
        self.storage.close()

    def __enter__(self) -> "RuntimeEngine":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.storage.save_ledger(self.ledger.to_dict())
        self.storage.close()

    def _load_pending_drift(self) -> dict[str, Any] | None:
        applied_key = self.storage.get_meta("applied_drift_event_key")
        row = self.storage.conn.execute(
            "SELECT payload_json FROM drift_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        event = json.loads(row["payload_json"])
        if applied_key and event.get("event_key") == applied_key:
            return None
        return event

    def _table_for(self, spec: BatchSpec):
        return self.bundle.table(spec.table)

    def _truth_for(self, window_id: str) -> ExactTruthPath:
        if window_id not in self._truth:
            window = self.bundle.plan.window(window_id)
            table = self.bundle.table(window.table)
            self._truth[window_id] = ExactTruthPath(window, table)
        return self._truth[window_id]

    def load_envelope(self, spec: BatchSpec) -> BatchEnvelope:
        if spec.seq in self._envelopes:
            return self._envelopes[spec.seq]
        table = self._table_for(spec)
        if self.sample_dir is not None:
            path = self.sample_dir / "batches" / f"batch-{spec.seq:04d}.json"
            if path.exists():
                envelope = read_batch_file(path, table, spec)
                self._envelopes[spec.seq] = envelope
                return envelope
        envelope = generate_batch(table, table.global_seed, spec)
        verify_envelope(envelope, table)
        self._envelopes[spec.seq] = envelope
        return envelope

    def _form_for_window(self, window_id: str) -> tuple[int, FormSpec, str]:
        current = self.storage.current_form()
        if current is None:
            raise BusinessRuleError(ErrorCode.PERSISTENCE_INVALID, "current form pointer is missing")
        current_version, current_form = current
        row = self.storage.get_window_row(window_id)
        if row is not None:
            pinned_version = int(row["form_version"])
            pinned = self.storage.get_form_version(pinned_version)
            if pinned is not None:
                return pinned_version, pinned, pinned.operators.get(window_id, current_form.operators.get(window_id, "hash"))
        return current_version, current_form, current_form.operators.get(
            window_id, self.bundle.plan.window(window_id).operator
        )

    def run(self) -> dict[str, Any]:
        batches = self.bundle.arrivals.batches
        for index, spec in enumerate(batches):
            envelope = self.load_envelope(spec)
            truth = self._truth_for(spec.window_id)
            if self.storage.batch_exists(spec.seq):
                truth.consume_envelope(envelope)
                self.report["skipped_committed_batches"].append(spec.seq)
                self._evaluate_drift_for_spec(spec)
                self._finalize_if_boundary(index, spec)
                continue

            version, form, operator = self._form_for_window(spec.window_id)
            window = self.bundle.plan.window(spec.window_id)
            row = self.storage.get_window_row(spec.window_id)
            if row is not None:
                aggregate = WindowAggregate.from_dict(json.loads(row["state_json"]), window)
            else:
                aggregate = WindowAggregate(window)

            estimated_groups = max(1, window.cost_estimate.output_cardinality)
            logical_estimate = estimate_logical_ms(operator, spec.row_count, estimated_groups)
            memory_estimate = estimate_memory_bytes(
                self._table_for(spec),
                spec.row_count,
                estimated_groups,
                len(window.aggregations),
            )

            def execute_batch() -> tuple[WindowAggregate, Any, int]:
                started_ns = time.perf_counter_ns()
                counters = aggregate.add_batch(envelope, self._table_for(spec), operator=operator)
                wall_elapsed_ns = time.perf_counter_ns() - started_ns
                return aggregate, counters, wall_elapsed_ns

            task = ScheduledTask(
                task_id=f"batch-{spec.seq}-{spec.window_id}",
                window_id=spec.window_id,
                batch_seq=spec.seq,
                logical_estimate_ms=logical_estimate,
                memory_estimate_bytes=memory_estimate,
                fn=execute_batch,
            )
            round_report = self.scheduler.run_round([task])
            self.report["scheduler_rounds"].append(round_report.to_dict())
            if round_report.failed_after_start:
                outcome = round_report.failed_after_start[0]
                raise BusinessRuleError(
                    ErrorCode.TASK_FAILED_AFTER_START,
                    outcome.error_message or "aggregation task failed after start",
                    outcome.to_dict(),
                )
            if not round_report.completed:
                outcome = round_report.outcomes[0]
                code = (
                    ErrorCode.ROUND_BUDGET_EXHAUSTED
                    if outcome.status.startswith("abandoned")
                    else ErrorCode.ROUND_BUDGET_EXHAUSTED
                )
                self.report["business_error_codes"].append(code)
                raise BusinessRuleError(code, "batch task was not started", outcome.to_dict())

            outcome = round_report.completed[0]
            _, counters, wall_elapsed_ns = outcome.result
            actual_elapsed = logical_elapsed_ms(operator, spec.row_count, counters.output_groups)
            truth.consume_envelope(envelope)
            if self.verify_truth_each_batch:
                self._assert_current_matches_truth(spec, aggregate, truth)

            metrics = {
                "operator": operator,
                "form_id": form.form_id,
                "form_version": version,
                "logical_elapsed_ms": frac_str(actual_elapsed),
                "logical_estimate_ms": frac_str(logical_estimate),
                "wall_elapsed_ns_audit_only": wall_elapsed_ns,
                "memory_estimate_bytes": memory_estimate,
                "counters": asdict(counters),
                "scheduler_status": STATUS_COMPLETED,
            }
            self.storage.commit_batch(
                seq=spec.seq,
                table_name=spec.table,
                window_id=spec.window_id,
                arrival_clock=spec.arrival_ms,
                row_count=spec.row_count,
                digest=envelope.summary.digest,
                summary=envelope.summary.to_dict(),
                metrics=metrics,
                state_json=aggregate.dumps(),
                form_version=version,
                committed_clock=spec.arrival_ms,
            )
            self.report["processed_batches"].append(
                {"seq": spec.seq, "window_id": spec.window_id, "operator": operator, "digest": envelope.summary.digest}
            )
            self._crash_if_requested(spec.seq)
            self._evaluate_drift_for_spec(spec)
            self._finalize_if_boundary(index, spec)

        if self.auto_close_at_end:
            for window in self.bundle.plan.windows:
                row = self.storage.get_window_row(window.window_id)
                if row is not None and row["status"] != "finalized":
                    aggregate = WindowAggregate.from_dict(json.loads(row["state_json"]), window)
                    result = canonical_window_result(aggregate)
                    self.storage.finalize_window(window.window_id, result, self.storage.clock_watermark())
                    self.report["finalizations"].append({"window_id": window.window_id, "reason": "end_of_script"})

        self.report["truth_verification"] = self.verify_all_windows()
        self.report["recovery_snapshot_after_run"] = self.storage.recovery_snapshot()
        self.storage.save_ledger(self.ledger.to_dict())
        return self.report

    def _assert_current_matches_truth(
        self,
        spec: BatchSpec,
        aggregate: WindowAggregate,
        truth: ExactTruthPath,
    ) -> None:
        expected = truth.canonical()
        actual = aggregate.canonical()
        if expected == actual:
            return
        first_key = next(
            (key for key in sorted(set(expected) | set(actual)) if expected.get(key) != actual.get(key)),
            None,
        )
        report = {
            "window_id": spec.window_id,
            "first_divergent_batch": spec.seq,
            "summary_digest": self._envelopes[spec.seq].summary.digest,
            "batch_row_count": spec.row_count,
            "key_json": first_key,
            "expected": expected.get(first_key),
            "actual": actual.get(first_key),
            "truth_rows": truth.aggregate.rows,
            "stream_rows": aggregate.rows,
            "truth_group_count": len(expected),
            "stream_group_count": len(actual),
        }
        raise AggregateMismatchError(
            ErrorCode.AGGREGATE_MISMATCH,
            f"aggregate mismatch first observed at batch {spec.seq}",
            report,
        )

    def _crash_if_requested(self, seq: int) -> None:
        if self.crash_after_batch is None or seq != self.crash_after_batch:
            return
        self.storage.save_ledger(self.ledger.to_dict())
        if self.crash_mode == "raise":
            raise BusinessRuleError(
                ErrorCode.CRASH_INJECTED,
                f"injected forced termination after committed batch {seq}",
                {"seq": seq},
            )
        os.kill(os.getpid(), signal.SIGKILL)

    def _build_observation(self, window_id: str, clock_ms: int) -> WindowObservation:
        rows = self.storage.batch_rows(window_id)
        if not rows:
            raise BusinessRuleError(ErrorCode.PERSISTENCE_INVALID, f"no committed rows for window {window_id}")
        elapsed = Fraction(0)
        digests: list[str] = []
        seqs: list[int] = []
        for row in rows:
            seqs.append(int(row["seq"]))
            digests.append(row["digest"])
            metrics = json.loads(row["metrics_json"] or "{}")
            elapsed += Fraction(metrics.get("logical_elapsed_ms", "0"))
        state_row = self.storage.get_window_row(window_id)
        cardinality = 0
        if state_row is not None:
            state = json.loads(state_row["state_json"])
            cardinality = len(state.get("groups", {}))
        return WindowObservation(
            table_name=rows[0]["table_name"],
            window_id=window_id,
            clock_ms=clock_ms,
            batch_seqs=tuple(seqs),
            batch_digests=tuple(digests),
            input_rows=sum(int(row["row_count"]) for row in rows),
            output_cardinality=cardinality,
            elapsed_ms=elapsed,
        )

    def _evaluate_drift_for_spec(self, spec: BatchSpec) -> dict[str, Any] | None:
        committed_count = len(self.storage.batch_rows(spec.window_id))
        if committed_count == 0:
            return None
        observation = self._build_observation(spec.window_id, spec.arrival_ms)
        window = self.bundle.plan.window(spec.window_id)
        event = evaluate_drift(
            observation,
            window,
            self.bundle.config,
            self.ledger,
            last_trigger_clock_ms=self.storage.last_drift_clock(),
            observation_count=committed_count,
        )
        self.report["observations"].append(observation.to_dict())
        self.report["drift_checks"].append(event)
        if event["triggered"]:
            inserted = self.storage.save_drift_event(event)
            event["persisted_new_record"] = inserted
            self.pending_drift = event
            self.report["drift_events"].append(event)
        elif event.get("cooldown_ok") is False or event.get("threshold_exceeded"):
            self.report.setdefault("suppressed_drift_checks", []).append(
                {
                    "window_id": spec.window_id,
                    "batch_interval": event["used_observation_batch_interval"],
                    "reason": "cooldown" if not event["cooldown_ok"] else "not_triggered",
                    "cooldown_remaining_ms": event["cooldown_remaining_ms"],
                }
            )
        self.storage.save_ledger(self.ledger.to_dict())
        return event

    def _is_boundary(self, index: int, spec: BatchSpec) -> bool:
        if spec.window_end:
            return True
        if index + 1 < len(self.bundle.arrivals.batches):
            return self.bundle.arrivals.batches[index + 1].window_id != spec.window_id
        return False

    def _finalize_if_boundary(self, index: int, spec: BatchSpec) -> None:
        if not self._is_boundary(index, spec):
            return
        row = self.storage.get_window_row(spec.window_id)
        if row is None:
            return
        if row["status"] == "finalized":
            self.report["finalizations"].append(
                {"window_id": spec.window_id, "status": "already_finalized_skipped", "watermark_seq": row["watermark_seq"]}
            )
            return

        window = self.bundle.plan.window(spec.window_id)
        aggregate = WindowAggregate.from_dict(json.loads(row["state_json"]), window)
        result = canonical_window_result(aggregate)
        clock_ms = spec.arrival_ms
        switch_report: dict[str, Any] | None = None
        new_form: FormSpec | None = None
        requested_version: int | None = None
        metadata: dict[str, str] = {}

        if self.pending_drift and self.pending_drift.get("window_id") == spec.window_id:
            current = self.storage.current_form()
            if current is None:
                raise BusinessRuleError(ErrorCode.PERSISTENCE_INVALID, "form pointer missing")
            _, current_form = current
            observation = self._build_observation(spec.window_id, clock_ms)
            decision = adjudicate_switch(
                current_form,
                self.bundle.plan.candidates,
                self.bundle.plan,
                self.bundle.config,
                self.ledger,
                observation=observation,
                drift_event=self.pending_drift,
            )
            selected_form = decision.pop("selected_form", None)
            if decision.get("switched"):
                new_form = selected_form
                requested_version = selected_form.target_version if selected_form else None
                metadata["applied_drift_event_key"] = self.pending_drift["event_key"]
                for flag in decision.get("precision_flags", []):
                    self.storage.save_precision_flag(flag, clock_ms)
                    self.report["precision_flags"].append(flag)
                self.pending_drift = None
            else:
                self.report["business_error_codes"].append(decision["error_code"])
            switch_report = decision
            self.report["switch_decisions"].append(decision)

        version = self.storage.finalize_window_and_switch(
            window_id=spec.window_id,
            result=result,
            clock_ms=clock_ms,
            new_form=new_form,
            requested_version=requested_version,
            metadata=metadata or None,
        )
        self.report["finalizations"].append(
            {
                "window_id": spec.window_id,
                "status": "finalized",
                "watermark_seq": spec.seq,
                "new_form_version": version,
                "switched": new_form is not None,
            }
        )
        self.storage.save_ledger(self.ledger.to_dict())

    def verify_all_windows(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for window in self.bundle.plan.windows:
            specs = [spec for spec in self.bundle.arrivals.batches if spec.window_id == window.window_id]
            envelopes = [self.load_envelope(spec) for spec in specs]
            truth = ExactTruthPath(window, self.bundle.table(window.table))
            for envelope in envelopes:
                truth.consume_envelope(envelope)
            row = self.storage.get_window_row(window.window_id)
            if row is None:
                results.append({"window_id": window.window_id, "matched": False, "reason": "missing_state"})
                continue
            persisted = WindowAggregate.from_dict(json.loads(row["state_json"]), window)
            matched = persisted.canonical() == truth.canonical()
            item = {
                "window_id": window.window_id,
                "matched": matched,
                "persisted_status": row["status"],
                "rows": persisted.rows,
                "group_count": len(persisted.groups),
                "first_divergent_batch": None,
            }
            if not matched:
                divergence = find_first_divergence(
                    window,
                    self.bundle.table(window.table),
                    envelopes,
                    stream_operator="hash",
                )
                item["first_divergence"] = divergence
                item["error_code"] = ErrorCode.AGGREGATE_MISMATCH
                raise AggregateMismatchError(
                    ErrorCode.AGGREGATE_MISMATCH,
                    f"persisted window {window.window_id!r} does not match exact truth",
                    item,
                )
            results.append(item)
        return results

    def recovery_snapshot(self) -> dict[str, Any]:
        return self.storage.recovery_snapshot()
