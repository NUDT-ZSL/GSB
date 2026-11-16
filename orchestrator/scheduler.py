"""Resource-constrained parallel scheduler with a full dependency state machine."""
from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from .clock import Clock
from .dag_loader import dag_fingerprint, resolve_stage_script
from .executor import (
    AttemptOutcome,
    OutputSummary,
    SubprocessStageRunner,
    build_stage_env,
    hash_output_file,
)
from .models import (
    Attempt,
    PipelineDag,
    Stage,
    StageResult,
    TraceEvent,
)
from .snapshot import (
    Snapshot,
    fresh_snapshot,
    save_snapshot,
)


class Scheduler:
    def __init__(
        self,
        dag: PipelineDag,
        base_dir: str,
        workspace_dir: str,
        snapshot_path: str,
        clock: Optional[Clock] = None,
        runner: Optional[SubprocessStageRunner] = None,
        failure_config: Optional[Dict[str, int]] = None,
        recovered: Optional[Dict[str, StageResult]] = None,
        recovery_count: int = 0,
        session_id: Optional[str] = None,
    ):
        self.dag = dag
        self.base_dir = os.path.abspath(base_dir)
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.snapshot_path = snapshot_path
        self.clock = clock or Clock()
        self.runner = runner or SubprocessStageRunner()
        self.failure_config = failure_config or {}
        self.session_id = session_id or f"sess-{uuid.uuid4().hex[:12]}"
        if not dag.source_path:
            dag.source_path = os.path.join(base_dir, "dags", "__inline__.json")

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._seq = 0
        self.trace: List[TraceEvent] = []
        self.results: Dict[str, StageResult] = {}
        self.running: Dict[str, threading.Event] = {}
        self.used_resources = 0
        self.wait_started: Dict[str, int] = {}
        self.wait_reasons: Dict[str, str] = {}
        self.recovered_ids = set((recovered or {}).keys())

        for sid in dag.stage_ids_sorted():
            if recovered and sid in recovered:
                self.results[sid] = recovered[sid]
            else:
                self.results[sid] = StageResult(
                    stage_id=sid,
                    state="pending",
                    output_path=self._output_abs(dag.stages[sid]),
                )

        fingerprint = dag_fingerprint(dag, self.base_dir)
        self.snapshot: Snapshot = fresh_snapshot(
            session_id=self.session_id,
            dag_definition=dag.to_dict(),
            dag_version=dag.dag_version,
            dag_fingerprint=fingerprint,
            budget=dag.global_resource_budget,
            default_max_concurrency=dag.default_max_concurrency,
            default_timeout_ms=dag.default_timeout_ms,
            created_iso=self.clock.iso_now(),
        )
        self.snapshot.recovery_count = recovery_count
        if recovered:
            self.snapshot.completed_stages = sorted(recovered)
            self.snapshot.states = {sid: result for sid, result in recovered.items()}

    # ------------------------------------------------------------------ paths
    def _output_abs(self, stage: Stage) -> str:
        candidate = stage.output_path
        if os.path.isabs(candidate):
            return os.path.normpath(candidate)
        return os.path.normpath(os.path.join(self.workspace_dir, candidate))

    def _script_abs(self, stage: Stage) -> str:
        return resolve_stage_script(self.dag, stage.script_path)

    # ------------------------------------------------------------------ events
    def _emit(
        self,
        stage_id: str,
        event: str,
        reason: str,
        resource_units: int = 0,
        start_mono_ms: Optional[int] = None,
        end_mono_ms: Optional[int] = None,
        extra: Optional[dict] = None,
    ) -> TraceEvent:
        record = TraceEvent(
            seq=self._seq,
            iso_time=self.clock.iso_now(),
            mono_ms=self.clock.mono_ms(),
            stage_id=stage_id,
            event=event,
            reason=reason,
            resource_units=resource_units,
            start_mono_ms=start_mono_ms,
            end_mono_ms=end_mono_ms,
            extra=extra or {},
        )
        self._seq += 1
        self.trace.append(record)
        return record

    def _persist_locked(self) -> None:
        self.snapshot.trace = list(self.trace)
        self.snapshot.states = dict(self.results)
        self.snapshot.completed_stages = sorted(
            sid
            for sid, result in self.results.items()
            if result.state == "succeeded"
        )
        self.snapshot.updated_iso = self.clock.iso_now()
        self.snapshot.finished = all(
            result.state in ("succeeded", "failed", "skipped")
            for result in self.results.values()
        )
        save_snapshot(self.snapshot_path, self.snapshot)

    # ------------------------------------------------------------- readiness
    def _stage_limit(self, stage: Stage) -> int:
        return stage.max_concurrency or self.dag.default_max_concurrency

    def _stage_timeout(self, stage: Stage) -> Optional[int]:
        return stage.timeout_ms or self.dag.default_timeout_ms

    def _terminal(self, stage_id: str) -> bool:
        return self.results[stage_id].state in ("succeeded", "failed", "skipped")

    def _evaluate_skip(self, stage: Stage) -> Optional[str]:
        for upstream in stage.strong_deps:
            if self.results[upstream].state in ("failed", "skipped"):
                return upstream
        return None

    def _is_ready(self, stage: Stage) -> bool:
        result = self.results[stage.stage_id]
        if result.state != "pending":
            return False
        for upstream in stage.strong_deps:
            if self.results[upstream].state != "succeeded":
                return False
        for upstream in stage.weak_deps:
            if not self._terminal(upstream):
                return False
        return True

    def _missing_weak_upstreams(self, stage: Stage) -> List[str]:
        return sorted(
            upstream
            for upstream in stage.weak_deps
            if self.results[upstream].state in ("failed", "skipped")
        )

    def _input_map(self, stage: Stage) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for upstream in stage.strong_deps + stage.weak_deps:
            upstream_result = self.results[upstream]
            if upstream_result.state == "succeeded":
                mapping[upstream] = upstream_result.output_path
        return dict(sorted(mapping.items()))

    def _propagate_skips(self) -> bool:
        changed = False
        for stage_id in self.dag.stage_ids_sorted():
            stage = self.dag.stages[stage_id]
            result = self.results[stage_id]
            if result.state != "pending":
                continue
            trigger = self._evaluate_skip(stage)
            if trigger is not None:
                result.state = "skipped"
                result.skipped_by = trigger
                result.missing_upstreams = sorted(
                    set(result.missing_upstreams) | {trigger}
                )
                self._emit(
                    stage_id,
                    "state_change",
                    f"skipped_due_to_failed_strong_upstream:{trigger}",
                    resource_units=0,
                    extra={"skipped_by": trigger},
                )
                changed = True
        return changed

    # ------------------------------------------------------------------ run
    def run(self) -> Snapshot:
        per_stage_limits = [
            stage.max_concurrency or self.dag.default_max_concurrency
            for stage in self.dag.stages.values()
        ]
        max_workers = max(1, self.dag.default_max_concurrency, *per_stage_limits)
        pool = ThreadPoolExecutor(max_workers=max_workers)
        pending_futures = set()
        try:
            with self._cond:
                self._mark_recovered_locked()
                self._propagate_skips_locked()
                self._persist_locked()
                while not self._all_terminal_locked():
                    self._dispatch_locked(pool, pending_futures)
                    done = {future for future in pending_futures if future.done()}
                    pending_futures -= done
                    for future in done:
                        future.result()
                    self._propagate_skips_locked()
                    self._persist_locked()
                    if not self._all_terminal_locked():
                        self._cond.wait(timeout=0.02)
                self._persist_locked()
        finally:
            pool.shutdown(wait=True)
        return self.snapshot

    def _all_terminal_locked(self) -> bool:
        return all(
            result.state in ("succeeded", "failed", "skipped")
            for result in self.results.values()
        )

    def _propagate_skips_locked(self) -> None:
        while self._propagate_skips():
            pass

    def _mark_recovered_locked(self) -> None:
        for sid in sorted(self.recovered_ids):
            result = self.results[sid]
            if result.state == "succeeded":
                self._emit(
                    sid,
                    "state_change",
                    "recovered_from_snapshot",
                    resource_units=self.dag.stages[sid].resource_units,
                    extra={"output_sha256": result.output_sha256},
                )

    def _dispatch_locked(self, pool: ThreadPoolExecutor, pending_futures: set) -> None:
        active_futures = {future for future in pending_futures if not future.done()}
        candidates = [
            self.dag.stages[sid]
            for sid in self.dag.stage_ids_sorted()
            if self._is_ready(self.dag.stages[sid])
        ]
        for stage in candidates:
            limit = self._stage_limit(stage)
            active_for_stage = sum(
                1 for sid in self.running if sid == stage.stage_id
            )
            if active_for_stage >= limit:
                self._record_waiting_locked(
                    stage, f"stage_concurrency_quota:{limit}"
                )
                continue
            if len(self.running) >= self.dag.default_max_concurrency:
                self._record_waiting_locked(
                    stage,
                    f"global_concurrency_quota:{self.dag.default_max_concurrency}",
                )
                continue
            if self.used_resources + stage.resource_units > self.dag.global_resource_budget:
                self._record_waiting_locked(
                    stage,
                    "resource_budget_unavailable",
                    extra={
                        "used": self.used_resources,
                        "budget": self.dag.global_resource_budget,
                        "requested": stage.resource_units,
                    },
                )
                continue
            future = self._launch_locked(stage, pool)
            pending_futures.add(future)

    def _record_waiting_locked(self, stage: Stage, reason: str, extra=None) -> None:
        now = self.clock.mono_ms()
        first_wait = stage.stage_id not in self.wait_started
        if first_wait:
            self.wait_started[stage.stage_id] = now
            self.wait_reasons[stage.stage_id] = reason
            self._emit(
                stage.stage_id,
                "waiting",
                reason,
                resource_units=stage.resource_units,
                start_mono_ms=now,
                extra=extra or {},
            )

    def _launch_locked(self, stage: Stage, pool: ThreadPoolExecutor):
        result = self.results[stage.stage_id]
        wait_started = self.wait_started.pop(stage.stage_id, None)
        self.wait_reasons.pop(stage.stage_id, None)
        result.state = "running"
        self.used_resources += stage.resource_units
        start = self.clock.mono_ms()
        result.start_mono_ms = start
        result.started_iso = self.clock.iso_now()
        self._emit(
            stage.stage_id,
            "state_change",
            "ready_to_running",
            resource_units=stage.resource_units,
            start_mono_ms=start,
            extra={"wait_started_mono_ms": wait_started},
        )
        stop_event = threading.Event()
        self.running[stage.stage_id] = stop_event
        return pool.submit(self._execute_stage, stage)

    # ------------------------------------------------------------- execution
    def _execute_stage(self, stage: Stage) -> None:
        with self._cond:
            missing = self._missing_weak_upstreams(stage)
            input_map = self._input_map(stage)
        total_attempts = stage.retries + 1
        outcome: Optional[AttemptOutcome] = None
        summary: Optional[OutputSummary] = None
        for attempt_no in range(1, total_attempts + 1):
            env = build_stage_env(
                stage_id=stage.stage_id,
                output_path=self._output_abs(stage),
                input_map=input_map,
                missing_upstreams=missing,
                attempt_no=attempt_no,
                failure_config=self.failure_config.get(stage.stage_id)
                and {stage.stage_id: self.failure_config[stage.stage_id]},
                script_dir=os.path.dirname(self._script_abs(stage)),
            )
            outcome = self.runner.run_attempt(
                script_abs=self._script_abs(stage),
                env=env,
                timeout_ms=self._stage_timeout(stage),
                clock=self.clock,
            )
            with self._cond:
                self._record_attempt_locked(stage, attempt_no, outcome)
            output_abs = self._output_abs(stage)
            if outcome.exit_code == 0 and not outcome.timed_out and os.path.isfile(output_abs):
                summary = hash_output_file(output_abs)
                break
        self._finalize_stage_locked(stage, outcome, summary, missing)

    def _record_attempt_locked(
        self, stage: Stage, attempt_no: int, outcome: AttemptOutcome
    ) -> None:
        attempt = Attempt(
            attempt_no=attempt_no,
            stage_id=stage.stage_id,
            start_mono_ms=outcome.start_mono_ms,
            end_mono_ms=outcome.end_mono_ms,
            duration_ms=outcome.duration_ms,
            exit_code=outcome.exit_code,
            timed_out=outcome.timed_out,
            stderr_norm=outcome.stderr_norm,
            reason=outcome.reason,
        )
        self.results[stage.stage_id].attempts.append(attempt)
        self._emit(
            stage.stage_id,
            "attempt",
            outcome.reason,
            resource_units=stage.resource_units,
            start_mono_ms=outcome.start_mono_ms,
            end_mono_ms=outcome.end_mono_ms,
            extra={
                "attempt_no": attempt_no,
                "exit_code": outcome.exit_code,
                "timed_out": outcome.timed_out,
                "duration_ms": outcome.duration_ms,
            },
        )

    def _finalize_stage_locked(
        self,
        stage: Stage,
        outcome: Optional[AttemptOutcome],
        summary: Optional[OutputSummary],
        missing: List[str],
    ) -> None:
        with self._cond:
            result = self.results[stage.stage_id]
            self.running.pop(stage.stage_id, None)
            self.used_resources -= stage.resource_units
            end = self.clock.mono_ms()
            result.end_mono_ms = end
            result.ended_iso = self.clock.iso_now()
            if summary is not None:
                result.state = "succeeded"
                result.output_path = summary.output_path
                result.output_sha256 = summary.sha256
                result.output_bytes = summary.byte_size
                result.output_records = summary.record_count
                result.missing_upstreams = sorted(missing)
                self._emit(
                    stage.stage_id,
                    "state_change",
                    "running_to_succeeded",
                    resource_units=stage.resource_units,
                    start_mono_ms=result.start_mono_ms,
                    end_mono_ms=end,
                    extra={
                        "output_sha256": summary.sha256,
                        "output_bytes": summary.byte_size,
                        "output_records": summary.record_count,
                        "missing_upstreams": sorted(missing),
                    },
                )
            else:
                result.state = "failed"
                result.missing_upstreams = sorted(missing)
                last_reason = outcome.reason if outcome else "unknown"
                self._emit(
                    stage.stage_id,
                    "state_change",
                    f"running_to_failed:{last_reason}",
                    resource_units=stage.resource_units,
                    start_mono_ms=result.start_mono_ms,
                    end_mono_ms=end,
                    extra={"attempts_recorded": len(result.attempts)},
                )
            self._persist_locked()
            self._cond.notify_all()

    # ------------------------------------------------------------- recovery
    def mark_unknown_after_crash(self, snapshot: Snapshot) -> List[str]:
        """Stage IDs that were not completed and therefore require a rerun."""
        completed = set(snapshot.completed_stages)
        unknown = sorted(
            sid for sid in self.dag.stage_ids_sorted() if sid not in completed
        )
        for sid in unknown:
            self.results[sid].state = "unknown"
            self._emit(sid, "state_change", "interrupted_by_hard_stop_requires_rerun")
        self.snapshot.unknown_stages = unknown
        self._persist_locked()
        return unknown
