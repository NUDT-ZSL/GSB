"""Runtime engine coordinating generation, execution, adaptive decisions, and state."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .blocks import BlockStore
from .canonical import digest_value
from .data import generate_batch
from .join import execute_batch
from .models import BatchSpec, ExecutionShape, Scenario
from .reopt import assess_drift, choose_shape, observation_records
from .storage import StateStore


@dataclass(frozen=True)
class CrashPlan:
    batch_id: str | None = None
    point: str = "after-execute"

    def should_trigger(self, point: str, batch: BatchSpec) -> bool:
        if point != self.point:
            return False
        return self.batch_id is None or self.batch_id == batch.batch_id


@dataclass
class RunResult:
    run_id: str
    plan_id: str
    started_from_seq: int
    processed_batches: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    drift_triggers: list[dict[str, Any]] = field(default_factory=list)
    suppressed_requests: list[dict[str, Any]] = field(default_factory=list)
    orphaned_staging: list[dict[str, Any]] = field(default_factory=list)
    final_watermark: int = -1
    final_shape: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "command": "run",
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "started_from_seq": self.started_from_seq,
            "processed_batches": self.processed_batches,
            "decision_error_codes": [
                decision["error_code"]
                for decision in self.decisions
                if decision.get("error_code")
            ],
            "warnings": self.warnings,
            "drift_triggers": self.drift_triggers,
            "suppressed_requests": self.suppressed_requests,
            "orphaned_staging": self.orphaned_staging,
            "final_watermark": self.final_watermark,
            "final_shape": self.final_shape,
            "complete": self.complete,
            "decisions": self.decisions,
        }


class RuntimeEngine:
    def __init__(
        self,
        scenario: Scenario,
        *,
        db_path: str | Path,
        blocks_path: str | Path,
        lock_path: str | Path | None = None,
        lock_timeout_s: float = 30.0,
    ) -> None:
        self.scenario = scenario
        self.db_path = Path(db_path)
        self.blocks_path = Path(blocks_path)
        lock = Path(lock_path) if lock_path is not None else self.db_path.with_suffix(self.db_path.suffix + ".lock")
        self.store = StateStore(self.db_path, lock, lock_timeout_s=lock_timeout_s)
        self.blocks = BlockStore(self.blocks_path)

    def close(self) -> None:
        self.store.close()

    def initialize(self) -> tuple[str, bool]:
        return self.store.initialize(self.scenario)

    def require_existing(self) -> str:
        return self.store.require_run(self.scenario.plan.plan_id)

    def _prepare(self, run_id: str) -> dict[str, Any]:
        with self.store.lock():
            staging = self.store.cleanup_staging(run_id)
            state, shape, _ = self.store.validate_locked(run_id)
            return {
                "state": state,
                "shape": shape,
                "orphaned_staging": staging,
            }

    def _maybe_crash(self, crash: CrashPlan | None, point: str, batch: BatchSpec) -> None:
        if crash is None or not crash.should_trigger(point, batch):
            return
        message = (
            f"forced termination at batch={batch.batch_id} point={point} "
            f"pid={os.getpid()}\n"
        )
        sys.stderr.write(message)
        sys.stderr.flush()
        sys.stdout.flush()
        os._exit(137)

    def run(
        self,
        *,
        resume: bool = False,
        max_batches: int | None = None,
        crash: CrashPlan | None = None,
    ) -> RunResult:
        if resume:
            run_id = self.require_existing()
        else:
            run_id, _ = self.initialize()
        prepared = self._prepare(run_id)
        state = prepared["state"]
        started_from = int(state["watermark_seq"]) + 1
        result = RunResult(
            run_id=run_id,
            plan_id=self.scenario.plan.plan_id,
            started_from_seq=started_from,
            orphaned_staging=prepared["orphaned_staging"],
        )
        processed = 0
        while True:
            if max_batches is not None and processed >= max_batches:
                break
            with self.store.lock():
                state = self.store.get_state(run_id)
                shape = self.store.get_shape(run_id)
                next_seq = int(state["watermark_seq"]) + 1
                if next_seq >= len(self.scenario.plan.batches):
                    result.complete = True
                    break
                batch = self.scenario.plan.batch_by_seq[next_seq]
                table_batches = generate_batch(self.scenario, batch)
                self._maybe_crash(crash, "after-generation", batch)
                stage_callback = (
                    (
                        lambda _step, current_batch=batch: self._maybe_crash(
                            crash, "during-join", current_batch
                        )
                    )
                    if crash is not None and crash.point == "during-join"
                    else None
                )
                execution = execute_batch(
                    self.scenario,
                    batch,
                    table_batches,
                    shape,
                    stage_callback=stage_callback,
                )
                self._maybe_crash(crash, "after-execute", batch)
                records = observation_records(self.scenario, execution)
                history = self.store.list_observations(run_id, before_seq=batch.arrival_seq - 1)
                assessment = assess_drift(
                    self.scenario,
                    current_records=records,
                    history_records=history,
                    current_batch=batch,
                    last_decision_ms=(
                        int(state["last_decision_ms"])
                        if state["last_decision_ms"] is not None
                        else None
                    ),
                )
                decision = None
                if assessment["triggered"] and not assessment["suppressed"]:
                    decision = choose_shape(
                        self.scenario,
                        current_shape=shape,
                        batch=batch,
                        history_records=history + records,
                        drift=assessment,
                    )
                    if decision.get("status") == "SWITCHED":
                        selected = decision["selected"]
                        proposed = ExecutionShape(
                            version=int(selected["version"]),
                            join_order=tuple(selected["join_order"]),
                            algorithms=tuple(selected["algorithms"]),
                            reason=str(selected.get("reason", "drift_reoptimization")),
                        )
                        self.store.stage_shape(run_id, proposed)
                        self._maybe_crash(crash, "after-staging", batch)
                self._maybe_crash(crash, "before-commit", batch)
                block_path = self.blocks.write_batch(batch, table_batches, execution)
                commit_digest = digest_value(
                    {
                        "execution_digest": execution.execution_digest,
                        "observations": [
                            digest_value(record) for record in records
                        ],
                        "assessment": assessment,
                        "decision": decision,
                        "committed_shape_version": shape.version,
                    }
                )
                committed = self.store.commit_batch(
                    run_id=run_id,
                    result=execution,
                    observation_records=records,
                    assessment=assessment,
                    decision=decision,
                    expected_seq=next_seq,
                    expected_shape_version=shape.version,
                    commit_digest=commit_digest,
                    block_path=block_path,
                )
                self._maybe_crash(crash, "after-commit", batch)
            processed += 1
            processed_entry = {
                "batch_id": batch.batch_id,
                "arrival_seq": batch.arrival_seq,
                "shape_version_before": shape.version,
                "shape_version_after": committed["shape_version"],
                "shape_digest_after": committed["shape_digest"],
                "input_digest": execution.input_digest,
                "output_digest": execution.output_digest,
                "output_rows": execution.output_rows,
                "commit_digest": commit_digest,
                "block_path": block_path,
                "duplicate": committed.get("duplicate", False),
                "drift_triggered": bool(assessment.get("triggered")),
                "drift_suppressed": bool(assessment.get("suppressed")),
                "max_deviation_ratio": assessment.get("max_deviation_ratio"),
                "threshold_margin": assessment.get("threshold_margin"),
            }
            result.processed_batches.append(processed_entry)
            if assessment.get("triggered") and not assessment.get("suppressed"):
                result.drift_triggers.append(
                    {
                        "batch_id": batch.batch_id,
                        "arrival_seq": batch.arrival_seq,
                        "logical_time_ms": assessment["logical_time_ms"],
                        "primary": assessment["primary"],
                        "sources": assessment["sources"],
                        "threshold": assessment["threshold"],
                        "threshold_margin": assessment["threshold_margin"],
                    }
                )
            elif assessment.get("suppressed"):
                result.suppressed_requests.append(
                    {
                        "batch_id": batch.batch_id,
                        "arrival_seq": batch.arrival_seq,
                        "logical_time_ms": assessment["logical_time_ms"],
                        "reason": assessment["reason"],
                        "remaining_cooldown_ms": assessment.get("remaining_cooldown_ms"),
                        "primary": assessment["primary"],
                        "max_deviation_ratio": assessment["max_deviation_ratio"],
                    }
                )
            if decision is not None:
                result.decisions.append(
                    {
                        "batch_id": batch.batch_id,
                        "arrival_seq": batch.arrival_seq,
                        **decision,
                    }
                )
                if decision.get("error_code"):
                    result.warnings.append(
                        {
                            "code": decision["error_code"],
                            "batch_id": batch.batch_id,
                            "message": "re-optimization retained the previous shape",
                            "reason": decision.get("reason"),
                        }
                    )
            state = self.store.get_state(run_id)
            result.final_watermark = int(state["watermark_seq"])
            result.final_shape = self.store.get_shape(run_id).to_dict()
        final_state = self.store.get_state(run_id)
        result.final_watermark = int(final_state["watermark_seq"])
        result.final_shape = self.store.get_shape(run_id).to_dict()
        result.complete = result.final_watermark >= len(self.scenario.plan.batches) - 1
        return result

    def status(self) -> dict[str, Any]:
        run_id = self.require_existing()
        with self.store.lock():
            state = self.store.status(run_id)
        return state

    def recover(self) -> dict[str, Any]:
        run_id = self.require_existing()
        return self.store.recover(run_id)

