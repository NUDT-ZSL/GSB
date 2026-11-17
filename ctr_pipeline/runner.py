"""End-to-end runner: deterministic scheduling, concurrent compute, ordered commits."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import PipelineConfig, load_config
from .errors import (
    E_CONFIG_MISMATCH,
    E_INPUT_MISMATCH,
    E_STATE_INVALID,
    FailureRecord,
    PipelineError,
)
from .io_utils import content_digest
from .loader import load_all
from .models import InjectionSpec
from .report import build_fatal_report, build_report, write_report
from .scheduler import BudgetPlan, RequestPlan, plan_budget
from .snapshot import SnapshotStore
from .stages import ComputeOutcome, compute_request


@dataclass
class RunOutcome:
    status: str
    exit_code: int
    report: dict[str, Any] | None


@dataclass
class Action:
    request_id: str
    plan: RequestPlan | None
    load_failure: FailureRecord | None

    @property
    def budget_events(self) -> list[dict[str, Any]]:
        if self.plan is None:
            return []
        return self.plan.budget_events


def _run_id(config_hash: str, params_hash: str, candidates_hash: str) -> str:
    return "run-" + content_digest(
        {
            "config": config_hash,
            "params": params_hash,
            "candidates": candidates_hash,
        }
    )[:16]


def _actions_for(
    requests: dict[str, Any],
    load_failures: list[FailureRecord],
    budget_plan: BudgetPlan,
) -> list[Action]:
    actions: dict[str, Action] = {}
    for failure in load_failures:
        if failure.request_id in actions:
            raise PipelineError(
                E_STATE_INVALID,
                "load",
                f"multiple load failures for request {failure.request_id!r}",
            )
        actions[failure.request_id] = Action(
            request_id=failure.request_id,
            plan=None,
            load_failure=failure,
        )
    for plan in budget_plan.plans:
        if plan.request_id in actions:
            raise PipelineError(
                E_STATE_INVALID,
                "schedule",
                f"request {plan.request_id!r} appears in both load and schedule results",
            )
        actions[plan.request_id] = Action(
            request_id=plan.request_id,
            plan=plan,
            load_failure=None,
        )
    # Valid requests must be scheduled in request-ID order.
    valid_ids = [plan.request_id for plan in budget_plan.plans]
    if valid_ids != sorted(valid_ids):
        raise PipelineError(E_STATE_INVALID, "schedule", "budget plans are not request-ID sorted")
    return [actions[request_id] for request_id in sorted(actions)]


def _expected_curve(actions: Iterable[Action]) -> list[dict[str, Any]]:
    curve: list[dict[str, Any]] = []
    for action in actions:
        curve.extend(dict(event) for event in action.budget_events)
    return curve


def _static_outcome(action: Action) -> ComputeOutcome:
    if action.load_failure is not None:
        return ComputeOutcome(
            success=False,
            request_report=None,
            stage_artifacts={},
            failure=action.load_failure,
            injection_hits=[],
        )
    if action.plan is not None and action.plan.failure is not None:
        return ComputeOutcome(
            success=False,
            request_report=None,
            stage_artifacts={},
            failure=action.plan.failure,
            injection_hits=[],
        )
    raise PipelineError(E_STATE_INVALID, "schedule", "static action has no outcome")


def _commit_outcome(
    store: SnapshotStore,
    manifest: dict[str, Any],
    action: Action,
    outcome: ComputeOutcome,
) -> None:
    if outcome.success:
        if outcome.request_report is None:
            raise PipelineError(E_STATE_INVALID, "snapshot", "successful outcome has no report")
        store.commit_success(
            manifest,
            request_id=action.request_id,
            artifacts=outcome.stage_artifacts,
            request_report=outcome.request_report,
            budget_events=action.budget_events,
            injection_hits=outcome.injection_hits,
        )
    else:
        if outcome.failure is None:
            raise PipelineError(E_STATE_INVALID, "snapshot", "failed outcome has no failure")
        failure = outcome.failure
        if failure.ready_queue_position is None and action.plan is not None:
            failure.ready_queue_position = action.plan.ready_queue_position
        store.commit_failure(
            manifest,
            failure=failure,
            budget_events=action.budget_events,
            injection_hits=outcome.injection_hits,
        )


def _validate_resume(
    manifest: dict[str, Any],
    cfg: PipelineConfig,
    params_hash: str,
    candidates_hash: str,
    actions: list[Action],
    expected_curve: list[dict[str, Any]],
) -> None:
    if manifest["config_hash"] != cfg.config_hash or manifest["params_hash"] != params_hash:
        raise PipelineError(
            E_CONFIG_MISMATCH,
            "snapshot",
            "persisted configuration or parameter-package hash does not match the current input",
        )
    if manifest["candidates_hash"] != candidates_hash:
        raise PipelineError(
            E_INPUT_MISMATCH,
            "snapshot",
            "persisted candidate-set hash does not match the current input",
        )
    cursor = int(manifest["cursor_index"])
    action_ids = [action.request_id for action in actions]
    if cursor < 0 or cursor > len(action_ids):
        raise PipelineError(E_STATE_INVALID, "snapshot", "cursor is outside action range")
    if list(manifest["processed_request_ids"]) != action_ids[:cursor]:
        raise PipelineError(
            E_STATE_INVALID,
            "snapshot",
            "processed request IDs do not match the sorted action prefix",
        )
    prefix = expected_curve[: len(manifest["budget_curve"])]
    if list(manifest["budget_curve"]) != prefix:
        raise PipelineError(
            E_STATE_INVALID,
            "snapshot",
            "persisted budget curve is not a prefix of the deterministic plan",
        )


def run_pipeline(
    *,
    config_path: str | Path,
    candidates_path: str | Path,
    params_path: str | Path,
    state_dir: str | Path,
    report_path: str | Path,
    concurrency_override: int | None = None,
    exit_after_requests: int | None = None,
    crash_after_requests: int | None = None,
    stop_event: threading.Event | None = None,
) -> RunOutcome:
    report_file = Path(report_path).resolve()
    input_paths = {
        "config": str(Path(config_path).resolve()),
        "candidates": str(Path(candidates_path).resolve()),
        "params": str(Path(params_path).resolve()),
    }
    try:
        cfg = load_config(config_path)
    except PipelineError as exc:
        fatal = build_fatal_report(
            status="config_error",
            code=exc.code,
            stage=exc.stage,
            exception_type=type(exc).__name__,
            message=exc.message,
        )
        write_report(report_file, fatal)
        return RunOutcome(status="config_error", exit_code=3, report=fatal)

    effective_concurrency = concurrency_override or cfg.concurrency
    if isinstance(effective_concurrency, bool) or not isinstance(effective_concurrency, int):
        raise ValueError("concurrency_override must be an integer")
    if effective_concurrency < 1:
        raise ValueError("concurrency_override must be >= 1")

    store = SnapshotStore(state_dir)
    try:
        store.ensure_writable()
    except OSError as exc:
        fatal = build_fatal_report(
            status="fatal",
            code=E_STATE_INVALID,
            stage="snapshot",
            exception_type=type(exc).__name__,
            message=f"state directory is not writable: {exc}",
            config_hash=cfg.config_hash,
        )
        write_report(report_file, fatal)
        return RunOutcome(status="fatal", exit_code=3, report=fatal)

    try:
        manifest = store.load_manifest()
    except PipelineError as exc:
        if exc.code != E_CONFIG_MISMATCH:
            raise
        fatal = build_fatal_report(
            status="rejected",
            code=exc.code,
            stage=exc.stage,
            exception_type=type(exc).__name__,
            message=exc.message,
            config_hash=cfg.config_hash,
        )
        write_report(report_file, fatal)
        return RunOutcome(status="rejected", exit_code=2, report=fatal)

    if manifest is not None and manifest["config_hash"] != cfg.config_hash:
        message = "persisted configuration hash does not match the current input"
        fatal = build_fatal_report(
            status="rejected",
            code=E_CONFIG_MISMATCH,
            stage="snapshot",
            exception_type="ConfigMismatch",
            message=message,
            config_hash=cfg.config_hash,
        )
        write_report(report_file, fatal)
        return RunOutcome(status="rejected", exit_code=2, report=fatal)

    load_result, injections = load_all(cfg, candidates_path, params_path)
    if load_result.params is None:
        fatal = build_fatal_report(
            status="fatal",
            code=load_result.failures[0].code if load_result.failures else E_STATE_INVALID,
            stage="load",
            exception_type=(
                load_result.failures[0].exception_type if load_result.failures else "PipelineError"
            ),
            message=(load_result.failures[0].message if load_result.failures else "params missing"),
            config_hash=cfg.config_hash,
            extra_failures=load_result.failures[1:],
        )
        write_report(report_file, fatal)
        return RunOutcome(status="fatal", exit_code=3, report=fatal)

    params = load_result.params
    if load_result.global_failure is not None:
        fatal = build_fatal_report(
            status="fatal",
            code=load_result.global_failure.code,
            stage=load_result.global_failure.stage,
            exception_type=load_result.global_failure.exception_type,
            message=load_result.global_failure.message,
            config_hash=cfg.config_hash,
        )
        write_report(report_file, fatal)
        return RunOutcome(status="fatal", exit_code=3, report=fatal)

    budget_plan = plan_budget(load_result.requests, cfg)
    actions = _actions_for(load_result.requests, load_result.failures, budget_plan)
    expected_curve = _expected_curve(actions)
    if expected_curve != budget_plan.curve:
        raise PipelineError(
            E_STATE_INVALID,
            "schedule",
            "flattened action budget curve differs from scheduler plan",
        )

    if manifest is None:
        manifest = store.new_manifest(
            run_id=_run_id(cfg.config_hash, params.params_hash, load_result.candidates_hash),
            config_hash=cfg.config_hash,
            params_hash=params.params_hash,
            candidates_hash=load_result.candidates_hash,
        )
        store.save_manifest(manifest)
    else:
        try:
            _validate_resume(
                manifest,
                cfg,
                params.params_hash,
                load_result.candidates_hash,
                actions,
                expected_curve,
            )
        except PipelineError as exc:
            if exc.code not in {E_CONFIG_MISMATCH, E_INPUT_MISMATCH}:
                raise
            fatal = build_fatal_report(
                status="rejected",
                code=exc.code,
                stage=exc.stage,
                exception_type=type(exc).__name__,
                message=exc.message,
                config_hash=cfg.config_hash,
            )
            write_report(report_file, fatal)
            return RunOutcome(status="rejected", exit_code=2, report=fatal)

    cursor = int(manifest["cursor_index"])
    remaining_actions = actions[cursor:]
    processed_this_run = 0
    interrupted = False
    pending_flush_since = 0
    stop_requested = stop_event or threading.Event()

    def flush(force: bool = False) -> None:
        nonlocal pending_flush_since
        if force or pending_flush_since >= cfg.snapshot_commit_interval:
            store.save_manifest(manifest)
            pending_flush_since = 0

    with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
        index = 0
        while index < len(remaining_actions):
            window = remaining_actions[index : index + effective_concurrency]
            futures = []
            for action in window:
                if action.plan is not None and action.plan.status == "success":
                    assert action.plan.request is not None
                    futures.append(
                        executor.submit(
                            compute_request,
                            action.plan.request,
                            cfg,
                            params,
                            injections,
                        )
                    )
                else:
                    futures.append(None)

            for action, future in zip(window, futures):
                outcome = future.result() if future is not None else _static_outcome(action)
                _commit_outcome(store, manifest, action, outcome)
                processed_this_run += 1
                pending_flush_since += 1
                flush(force=False)

                if crash_after_requests is not None and processed_this_run >= crash_after_requests:
                    flush(force=True)
                    os._exit(137)

                if exit_after_requests is not None and processed_this_run >= exit_after_requests:
                    interrupted = True
                    flush(force=True)
                    break
            if interrupted:
                break
            if stop_requested.is_set():
                interrupted = True
                flush(force=True)
                break
            index += len(window)

    if not interrupted and int(manifest["cursor_index"]) < len(actions):
        raise PipelineError(E_INTERNAL, "schedule", "runner stopped before all actions committed")
    if not interrupted:
        flush(force=True)
        status = "completed"
    else:
        store.save_manifest(manifest)
        status = "interrupted"

    report = build_report(
        cfg=cfg,
        store=store,
        manifest=manifest,
        injections=injections,
        status=status,
        effective_concurrency=effective_concurrency,
        input_paths=input_paths,
    )
    write_report(report_file, report)
    return RunOutcome(status=status, exit_code=0, report=report)
