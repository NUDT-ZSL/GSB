"""End-to-end deterministic convergence engine."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .canonical import canonical_json, digest_value
from .errors import EngineError
from .evaluate import evaluate_view
from .injection import InjectionController
from .loader import ManifestLoader
from .merge import merge_versions
from .migration import migrate_v1_entries
from .replacement import classify_change
from .store import Store, file_lock


PIPELINE_STEPS = ("parse", "migrate", "merge", "evaluate", "stage", "switch")


@dataclass
class EngineResult:
    view: dict[str, Any] | None
    ok: bool
    idempotent: bool
    operation_id: str | None
    steps_executed: list[str]
    steps_replayed: list[str]
    recovered: bool
    state_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "idempotent": self.idempotent,
            "operation_id": self.operation_id,
            "steps_executed": self.steps_executed,
            "steps_replayed": self.steps_replayed,
            "recovered": self.recovered,
            "state_path": self.state_path,
            "view": self.view,
        }


class ConvergenceEngine:
    def __init__(
        self,
        *,
        v1_path: str | Path,
        v2_meta_path: str | Path,
        v2_shards_dir: str | Path,
        state_path: str | Path,
        environment_order: list[str],
        tolerances: dict[str, Any],
        clock_tolerance_ms: int = 0,
        rollback_policy: str = "reject",
        rollback_window_ms: int = 0,
        injection_path: str | Path | None = None,
    ) -> None:
        if not environment_order:
            raise ValueError("environment_order must not be empty")
        self.environment_order = list(environment_order)
        self.tolerances = copy.deepcopy(tolerances)
        self.clock_tolerance_ms = int(clock_tolerance_ms)
        self.rollback_policy = rollback_policy
        self.rollback_window_ms = int(rollback_window_ms)
        self.loader = ManifestLoader(
            v1_path=v1_path,
            v2_meta_path=v2_meta_path,
            v2_shards_dir=v2_shards_dir,
            expected_environments=self.environment_order,
        )
        self.state_path = Path(state_path).resolve()
        self.injections = InjectionController.load(injection_path)
        self.evaluation_config = {
            "environment_order": self.environment_order,
            "tolerances": self.tolerances,
            "clock_tolerance_ms": self.clock_tolerance_ms,
            "rollback_policy": self.rollback_policy,
            "rollback_window_ms": self.rollback_window_ms,
        }

    def run(
        self,
        *,
        clock_ms: int,
        crash_after_step: str | None = None,
        recover: bool = False,
    ) -> EngineResult:
        if crash_after_step is not None and crash_after_step not in PIPELINE_STEPS:
            raise ValueError(f"unknown crash step: {crash_after_step}")
        if not isinstance(clock_ms, int) or isinstance(clock_ms, bool) or clock_ms < 0:
            raise ValueError("clock_ms must be a non-negative integer")

        injected_rollback = self.injections.find("evaluate", "clock_rollback")
        if injected_rollback is not None:
            raise EngineError(
                "E_CLOCK_ROLLBACK",
                "injected logical clock rollback",
                stage="evaluate",
                details={
                    "injection": injected_rollback.id,
                    "clock_ms": clock_ms,
                    "requested_rollback_ms": injected_rollback.parameters.get(
                        "rollback_ms", 10000
                    ),
                },
            )

        source_fingerprint = self.loader.source_fingerprint()
        package_key = digest_value(
            {
                "source_fingerprint": source_fingerprint,
                "evaluation_config": self.evaluation_config,
                "injection_digest": self.injections.digest(),
            }
        )
        operation_id = digest_value(
            {
                "package_key": package_key,
                "injection_digest": self.injections.digest(),
            }
        )[:32]
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")

        with file_lock(lock_path):
            store: Store | None = None
            operation: dict[str, Any] | None = None
            current: dict[str, Any] | None = None
            parsed: dict[str, Any] | None = None
            recovered = False
            steps_executed: list[str] = []
            steps_replayed: list[str] = []

            if self.state_path.exists():
                store = Store(self.state_path)
                operation = store.get_operation(operation_id)
                if operation is not None and operation["status"] == "running":
                    recovered = True
                effective_clock = (
                    int(operation["clock_ms"]) if recovered else clock_ms
                )
                store.check_clock(effective_clock, self.clock_tolerance_ms)
                current = store.get_current_view()
                current_matches = (
                    current is not None
                    and current["persisted"]["package_key"] == package_key
                )
                if current_matches and not recovered:
                    return self._result(
                        self._submission_view(
                            current,
                            {
                                "change_kind": "noop",
                                "reason": "current_pointer_matches_package_key",
                                "basis": ["package_key"],
                            },
                        ),
                        idempotent=True,
                        operation_id=operation_id,
                        steps_executed=[],
                        steps_replayed=[],
                        recovered=False,
                    )
                if current_matches and recovered:
                    store.complete_operation(operation_id, effective_clock)
                    return self._result(
                        self._submission_view(
                            current,
                            {
                                "change_kind": "noop",
                                "reason": "recovery_found_committed_view",
                                "basis": ["package_key"],
                            },
                        ),
                        idempotent=True,
                        operation_id=operation_id,
                        steps_executed=[],
                        steps_replayed=[],
                        recovered=True,
                    )
            else:
                effective_clock = clock_ms

            if not recovered:
                parsed = self.loader.load(
                    clock_ms=effective_clock,
                    injections=self.injections,
                ).payload
                if store is None:
                    store = Store(self.state_path)
                store.check_clock(effective_clock, self.clock_tolerance_ms)
                current = store.get_current_view()
                if (
                    current is not None
                    and current["persisted"]["package_key"] == package_key
                ):
                    return self._result(
                        self._submission_view(
                            current,
                            {
                                "change_kind": "noop",
                                "reason": "current_pointer_matches_package_key",
                                "basis": ["package_key"],
                            },
                        ),
                        idempotent=True,
                        operation_id=operation_id,
                        steps_executed=[],
                        steps_replayed=[],
                        recovered=False,
                    )
                store.create_operation(
                    op_id=operation_id,
                    package_key=package_key,
                    clock_ms=effective_clock,
                )
            else:
                if store is None:
                    raise EngineError(
                        "E_STATE_CORRUPT",
                        "recovery requested but no state database exists",
                        stage="persist",
                        details={"state_path": str(self.state_path)},
                    )

            assert store is not None
            try:
                parse_output = self._step(
                    store=store,
                    operation_id=operation_id,
                    step_name="parse",
                    clock_ms=effective_clock,
                    crash_after_step=crash_after_step,
                    steps_executed=steps_executed,
                    steps_replayed=steps_replayed,
                    compute=lambda: parsed
                    if parsed is not None
                    else self.loader.load(
                        clock_ms=effective_clock,
                        injections=self.injections,
                    ).payload,
                )
                migrated = self._step(
                    store=store,
                    operation_id=operation_id,
                    step_name="migrate",
                    clock_ms=effective_clock,
                    crash_after_step=crash_after_step,
                    steps_executed=steps_executed,
                    steps_replayed=steps_replayed,
                    compute=lambda: self._migrate_with_injection(
                        parse_output,
                        clock_ms=effective_clock,
                    ),
                )
                merged = self._step(
                    store=store,
                    operation_id=operation_id,
                    step_name="merge",
                    clock_ms=effective_clock,
                    crash_after_step=crash_after_step,
                    steps_executed=steps_executed,
                    steps_replayed=steps_replayed,
                    compute=lambda: self._merge_with_injection(migrated),
                )
                evaluated = self._step(
                    store=store,
                    operation_id=operation_id,
                    step_name="evaluate",
                    clock_ms=effective_clock,
                    crash_after_step=crash_after_step,
                    steps_executed=steps_executed,
                    steps_replayed=steps_replayed,
                    compute=lambda: evaluate_view(
                        merged,
                        environment_order=self.environment_order,
                        tolerances=self.tolerances,
                        injections=self.injections,
                    ),
                )

                current_descriptor = store.get_current_descriptor()
                change_decision = classify_change(
                    current_descriptor,
                    evaluated["descriptor"],
                    clock_ms=effective_clock,
                    rollback_policy=self.rollback_policy,
                    rollback_window_ms=self.rollback_window_ms,
                )
                if change_decision["change_kind"] == "noop":
                    store.save_step(
                        op_id=operation_id,
                        step_name="classify",
                        output=change_decision,
                        clock_ms=effective_clock,
                    )
                    store.complete_operation(operation_id, effective_clock)
                    existing = store.get_current_view()
                    if existing is None:
                        raise EngineError(
                            "E_STATE_CORRUPT",
                            "noop classification has no current view",
                            stage="replace",
                            details={},
                        )
                    return self._result(
                        self._submission_view(existing, change_decision),
                        idempotent=True,
                        operation_id=operation_id,
                        steps_executed=steps_executed,
                        steps_replayed=steps_replayed,
                        recovered=recovered,
                    )

                view_id = "view-" + digest_value(
                    {
                        "package_key": package_key,
                        "clock_ms": effective_clock,
                        "change_kind": change_decision["change_kind"],
                    }
                )[:24]
                final_view = copy.deepcopy(evaluated)
                final_view["view_id"] = view_id
                final_view["change"] = change_decision
                final_view["operation"] = {
                    "operation_id": operation_id,
                    "package_key": package_key,
                    "clock_ms": effective_clock,
                }

                stage_cached = store.get_step(operation_id, "stage")
                if stage_cached is None:
                    store.stage_view(
                        op_id=operation_id,
                        step_name="stage",
                        view_id=view_id,
                        package_key=package_key,
                        descriptor=evaluated["descriptor"],
                        change_decision=change_decision,
                        view=final_view,
                        clock_ms=effective_clock,
                    )
                    steps_executed.append("stage")
                    self._maybe_crash(crash_after_step, "stage")
                else:
                    steps_replayed.append("stage")

                switch_cached = store.get_step(operation_id, "switch")
                if switch_cached is None:
                    store.switch_view(
                        op_id=operation_id,
                        step_name="switch",
                        view_id=view_id,
                        package_key=package_key,
                        clock_ms=effective_clock,
                    )
                    steps_executed.append("switch")
                    self._maybe_crash(crash_after_step, "switch")
                else:
                    steps_replayed.append("switch")

                store.complete_operation(operation_id, effective_clock)
                committed = store.get_view_by_package_key(package_key)
                if committed is None:
                    raise EngineError(
                        "E_STATE_CORRUPT",
                        "view switch completed without a committed view",
                        stage="persist",
                        details={"package_key": package_key},
                    )
                return self._result(
                    committed,
                    idempotent=bool(switch_cached),
                    operation_id=operation_id,
                    steps_executed=steps_executed,
                    steps_replayed=steps_replayed,
                    recovered=recovered,
                )
            except EngineError as error:
                store.fail_operation(operation_id, effective_clock, error)
                raise
            except BaseException as error:
                if isinstance(error, SystemExit):
                    raise
                wrapped = EngineError(
                    "E_STATE_CORRUPT",
                    "unexpected failure during manifest processing",
                    stage="persist",
                    details={"reason": repr(error)},
                )
                store.fail_operation(operation_id, effective_clock, wrapped)
                raise wrapped from error

    def _merge_with_injection(self, migrated: dict[str, Any]) -> dict[str, Any]:
        generation_injection = self.injections.find("merge", "generation_conflict")
        if generation_injection is not None:
            raise EngineError(
                "E_GENERATION_CONFLICT",
                "injected manifest generation conflict",
                stage="merge",
                details={
                    "injection": generation_injection.id,
                    "environment": generation_injection.parameters.get(
                        "environment", "development"
                    ),
                    "service": generation_injection.parameters.get(
                        "service", "orders-api"
                    ),
                    "expected_generation": generation_injection.parameters.get(
                        "expected_generation", 12
                    ),
                    "actual_generation": generation_injection.parameters.get(
                        "actual_generation", 11
                    ),
                },
            )
        return merge_versions(
            migrated,
            environment_order=self.environment_order,
        )

    def _migrate_with_injection(
        self,
        parse_output: dict[str, Any],
        *,
        clock_ms: int,
    ) -> dict[str, Any]:
        migration_injection = self.injections.find("migrate", "migration_failure")
        if migration_injection is not None:
            raise EngineError(
                "E_MIGRATION",
                "injected v1 semantic migration failure",
                stage="migrate",
                details={
                    "injection": migration_injection.id,
                    "environment": migration_injection.parameters.get(
                        "environment", "development"
                    ),
                    "service": migration_injection.parameters.get(
                        "service", "reporting-api"
                    ),
                    "mapping": "v1_missing_absolute_effective_time",
                },
            )
        return migrate_v1_entries(parse_output, clock_ms=clock_ms)

    def _step(
        self,
        *,
        store: Store,
        operation_id: str,
        step_name: str,
        clock_ms: int,
        crash_after_step: str | None,
        steps_executed: list[str],
        steps_replayed: list[str],
        compute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        cached = store.get_step(operation_id, step_name)
        if cached is not None:
            steps_replayed.append(step_name)
            return cached["output"]
        output = compute()
        store.save_step(
            op_id=operation_id,
            step_name=step_name,
            output=output,
            clock_ms=clock_ms,
        )
        steps_executed.append(step_name)
        self._maybe_crash(crash_after_step, step_name)
        return output

    @staticmethod
    def _maybe_crash(crash_after_step: str | None, completed_step: str) -> None:
        if crash_after_step == completed_step:
            os._exit(90)

    def _result(
        self,
        view: dict[str, Any] | None,
        *,
        idempotent: bool,
        operation_id: str | None,
        steps_executed: list[str],
        steps_replayed: list[str],
        recovered: bool,
    ) -> EngineResult:
        return EngineResult(
            view=view,
            ok=True,
            idempotent=idempotent,
            operation_id=operation_id,
            steps_executed=steps_executed,
            steps_replayed=steps_replayed,
            recovered=recovered,
            state_path=str(self.state_path),
        )

    @staticmethod
    def _submission_view(
        view: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        output = copy.deepcopy(view)
        output["submission"] = decision
        return output
