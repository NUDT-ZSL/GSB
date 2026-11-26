"""Command orchestration for load/build/schedule/inject/resume/status/verify."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from .cost import (
    DriftEvent,
    evaluate_candidates,
    generate_drift_events,
)
from .errors import (
    INVALID_ARGUMENT,
    INVALID_THREAD_OCCUPANCY,
    NO_ACTIVE_PLAN,
    UNKNOWN_JOB,
    BusinessError,
)
from .invalidation import (
    compute_cache_closure,
    compute_chain_closure,
    generate_snapshot_chains,
)
from .models import (
    Plan,
    PlatformStats,
    Task,
    parse_plan_file,
    parse_platform_stats_file,
    parse_tasks_file,
)
from .scheduler import ScheduleConfig, schedule_plan
from .storage import Storage
from .subtasks import (
    SubtaskChain,
    flatten_chains,
    generate_plan_chains,
    plan_subtask_summary,
    simulate_output_digest,
)
from .util import digest_obj


DEFAULT_DB = "var/engine_diag.sqlite3"


def _existing_job_ids(db_path: str | os.PathLike[str]) -> set[str]:
    path = Path(db_path)
    if not path.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return set()
    try:
        rows = conn.execute("SELECT job_id FROM tasks").fetchall()
        return {str(row[0]) for row in rows}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def load_workspace(
    *,
    db: str,
    tasks_path: str | None = None,
    stats_path: str | None = None,
    plan_path: str | None = None,
    max_threads: int = 16,
) -> dict[str, Any]:
    if not any((tasks_path, stats_path, plan_path)):
        raise BusinessError(
            INVALID_ARGUMENT,
            "load requires at least one of tasks, stats, or plan",
        )
    tasks = parse_tasks_file(tasks_path) if tasks_path else []
    stats = (
        parse_platform_stats_file(stats_path, max_threads=max_threads)
        if stats_path
        else None
    )
    plan = parse_plan_file(plan_path) if plan_path else None

    if plan is not None:
        available = {task.job_id for task in tasks} | _existing_job_ids(db)
        missing = sorted(set(plan.jobs) - available)
        if missing:
            raise BusinessError(
                UNKNOWN_JOB,
                "plan references jobs that are neither in this load nor persisted",
                details={"plan_version": plan.plan_version, "job_ids": missing},
            )
        if stats is not None:
            over = [
                entry.to_dict()
                for entry in stats.entries
                if entry.thread_occupancy > plan.max_threads
            ]
            if over:
                raise BusinessError(
                    INVALID_THREAD_OCCUPANCY,
                    "platform thread occupancy exceeds plan max_threads",
                    details={
                        "plan_version": plan.plan_version,
                        "max_threads": plan.max_threads,
                        "entries": over,
                    },
                )

    with Storage(db) as storage:
        summary = storage.ingest(tasks=tasks, stats=stats, plan=plan)
    return {
        "ok": True,
        "command": "load",
        "db": str(db),
        "inputs": {
            "tasks": tasks_path,
            "stats": stats_path,
            "plan": plan_path,
        },
        **summary,
    }


def _load_runtime(
    storage: Storage, plan_version: str | None
) -> tuple[Plan, dict[str, Task], PlatformStats, dict[str, SubtaskChain], list[DriftEvent]]:
    plan = storage.get_plan(plan_version)
    tasks = storage.get_tasks()
    missing = sorted(set(plan.jobs) - set(tasks))
    if missing:
        raise BusinessError(
            UNKNOWN_JOB,
            "plan references tasks that are not loaded",
            details={"plan_version": plan.plan_version, "job_ids": missing},
        )
    platform = storage.get_platform_stats()
    chains = generate_plan_chains(plan, tasks)
    events = storage.drift_events(plan.plan_version)
    return plan, tasks, platform, chains, events


def _save_closure_if_needed(
    storage: Storage, closure: dict[str, Any]
) -> dict[str, Any] | None:
    if not closure["affected"] and not closure["protected_committed"]:
        return None
    saved = storage.save_closure(closure)
    return {
        "closure_id": saved["closure_id"],
        "fingerprint": saved["fingerprint"],
        "reason": saved["reason"],
        "inserted": saved["inserted"],
        "affected_count": len(saved["affected"]),
        "reused_count": len(saved["reused"]),
        "protected_committed_count": len(saved["protected_committed"]),
    }


def _build_closures(
    *,
    storage: Storage,
    plan: Plan,
    tasks: dict[str, Task],
    chains: dict[str, SubtaskChain],
    committed_ids: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    closures: list[dict[str, Any]] = []

    previous_version = storage.previous_plan_version()
    if previous_version and previous_version != plan.plan_version:
        old_plan = storage.get_plan(previous_version)
        revisions: dict[str, int] = {}
        for job_id in old_plan.jobs:
            checkpoint = storage.checkpoint(previous_version, job_id)
            if checkpoint is not None:
                revisions[job_id] = int(checkpoint["revision"])
        old_chains = generate_snapshot_chains(
            plan=old_plan,
            current_tasks=tasks,
            revisions=revisions,
            storage=storage,
        )
        new_revisions: dict[str, int] = {}
        has_new_checkpoint = False
        for job_id in plan.jobs:
            checkpoint = storage.checkpoint(plan.plan_version, job_id)
            if checkpoint is None:
                new_revisions[job_id] = tasks[job_id].revision
            else:
                new_revisions[job_id] = int(checkpoint["revision"])
                has_new_checkpoint = True
        new_snapshot = (
            generate_snapshot_chains(
                plan=plan,
                current_tasks=tasks,
                revisions=new_revisions,
                storage=storage,
            )
            if has_new_checkpoint
            else chains
        )
        closure = compute_chain_closure(
            plan_version=plan.plan_version,
            reason="plan_version_switch",
            old_chains=old_chains,
            new_chains=new_snapshot,
            committed_ids=committed_ids,
            details={
                "previous_plan_version": previous_version,
                "new_plan_version": plan.plan_version,
            },
        )
        saved = _save_closure_if_needed(storage, closure)
        if saved:
            closures.append(saved)

    revisions = {}
    revision_changed = False
    for job_id in plan.jobs:
        checkpoint = storage.checkpoint(plan.plan_version, job_id)
        if checkpoint is None:
            revisions[job_id] = tasks[job_id].revision
        else:
            revisions[job_id] = int(checkpoint["revision"])
            if int(checkpoint["revision"]) != tasks[job_id].revision:
                revision_changed = True
    if revision_changed:
        old_chains = generate_snapshot_chains(
            plan=plan,
            current_tasks=tasks,
            revisions=revisions,
            storage=storage,
        )
        closure = compute_chain_closure(
            plan_version=plan.plan_version,
            reason="task_revision",
            old_chains=old_chains,
            new_chains=chains,
            committed_ids=committed_ids,
            details={"revisions": revisions},
        )
        saved = _save_closure_if_needed(storage, closure)
        if saved:
            closures.append(saved)

    pending_changes = storage.pending_changes()
    cache_changes = [
        change for change in pending_changes if change["kind"] == "cache_layer_change"
    ]
    consumed_changes: list[str] = []
    if cache_changes:
        closure = compute_cache_closure(
            plan_version=plan.plan_version,
            new_chains=chains,
            committed_ids=committed_ids,
            change_ids=[change["change_id"] for change in cache_changes],
            changes=cache_changes,
        )
        saved = _save_closure_if_needed(storage, closure)
        if saved:
            closures.append(saved)
        consumed_changes = [change["change_id"] for change in cache_changes]
    return closures, consumed_changes


def _build_core(
    *,
    db: str,
    plan_version: str | None,
    crash_after: int | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    with Storage(db) as storage:
        orphaned = storage.orphan_pending_attempts() if resume else 0
        plan, tasks, platform, chains, events = _load_runtime(storage, plan_version)
        evaluation = evaluate_candidates(
            plan=plan,
            chains=chains,
            platform=platform,
            events=events,
        )
        selected = plan.candidate(evaluation["selected_candidate_id"])
        selected_result = evaluation["candidates"][selected.candidate_id]
        selected_steps = {
            step["subtask_id"]: step for step in selected_result["steps"]
        }
        ordered_ids = list(selected_result["aggregation_order"])
        if set(ordered_ids) != {subtask.subtask_id for subtask in flatten_chains(chains)}:
            raise BusinessError(
                "STATE_CONFLICT",
                "candidate aggregation order does not cover all subtasks",
            )

        committed_ids = {entry["subtask_id"] for entry in storage.all_ledger()}
        closures, consumed_changes = _build_closures(
            storage=storage,
            plan=plan,
            tasks=tasks,
            chains=chains,
            committed_ids=committed_ids,
        )
        storage.register_subtasks(plan=plan, chains=chains)

        all_subtasks = {subtask.subtask_id: subtask for subtask in flatten_chains(chains)}
        inserted = 0
        reused = 0
        late_rejected = 0
        for index, subtask_id in enumerate(ordered_ids):
            subtask = all_subtasks[subtask_id]
            attempt = storage.begin_attempt(subtask.subtask_id, plan.plan_version)
            if attempt["state"] == "committed":
                reused += 1
                continue
            if crash_after is not None and index == crash_after:
                os._exit(75)
            output_digest = simulate_output_digest(subtask)
            finished = storage.finish_attempt(
                attempt_id=attempt["attempt_id"],
                subtask=subtask,
                plan_version=plan.plan_version,
                output_digest=output_digest,
                cost=selected_steps[subtask.subtask_id],
            )
            if finished["state"] == "inserted":
                inserted += 1
            elif finished["state"] == "reused":
                reused += 1
            else:
                late_rejected += 1

        for job_id in sorted(chains):
            storage.save_checkpoint(
                plan=plan, chain=chains[job_id], task=tasks[job_id]
            )
        watermark = storage.advance_watermark(plan=plan, chains=chains)
        storage.save_cost_evaluation(plan.plan_version, evaluation)
        storage.consume_changes(consumed_changes)
        max_clock = max(
            [
                step["logical_clock_ms"] + int(step["duration_ms"])
                for step in selected_result["steps"]
            ]
            or [0]
        )
        logical_clock = storage.advance_logical_clock(max_clock)
        ledger = storage.ledger_for_plan(plan.plan_version)
        return {
            "ok": True,
            "command": "resume" if resume else "build",
            "plan_version": plan.plan_version,
            "global_seed": plan.global_seed,
            "subtask_summary": plan_subtask_summary(chains),
            "selected_candidate_id": selected.candidate_id,
            "candidate_comparison": evaluation["comparison"],
            "candidates": evaluation["candidates"],
            "drift_truth": evaluation["drift_truth"],
            "ledger": {
                "inserted": inserted,
                "reused": reused,
                "late_rejected": late_rejected,
                "orphaned_attempts_cleaned": orphaned,
                "entries_for_plan": len(ledger),
            },
            "invalidations": closures,
            "watermark": watermark,
            "logical_clock_ms": logical_clock,
            "crash_after": crash_after,
        }


def build_workspace(
    *,
    db: str,
    plan_version: str | None = None,
    crash_after: int | None = None,
) -> dict[str, Any]:
    return _build_core(
        db=db,
        plan_version=plan_version,
        crash_after=crash_after,
        resume=False,
    )


def resume_workspace(
    *, db: str, plan_version: str | None = None
) -> dict[str, Any]:
    return _build_core(db=db, plan_version=plan_version, resume=True)


def schedule_workspace(
    *,
    db: str,
    plan_version: str | None,
    budget: float,
    max_concurrency: int,
    rounds: int,
    start_clock_ms: int,
    round_span_ms: int,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    with Storage(db) as storage:
        plan, tasks, platform, chains, events = _load_runtime(storage, plan_version)
        evaluation = evaluate_candidates(
            plan=plan,
            chains=chains,
            platform=platform,
            events=events,
        )
        chosen_id = candidate_id or evaluation["selected_candidate_id"]
        candidate = plan.candidate(chosen_id)
        cost_result = evaluation["candidates"][candidate.candidate_id]
        config = ScheduleConfig(
            budget=budget,
            max_concurrency=max_concurrency,
            rounds=rounds,
            start_clock_ms=start_clock_ms,
            round_span_ms=round_span_ms,
        )
        run = schedule_plan(
            plan=plan,
            candidate=candidate,
            chains=chains,
            cost_result=cost_result,
            events=events,
            config=config,
        )
        saved = storage.save_schedule(run)
        storage.save_cost_evaluation(plan.plan_version, evaluation)
        last_clock = max(
            (
                round_data["start_clock_ms"] + round_span_ms
                for round_data in run["rounds"]
            ),
            default=start_clock_ms,
        )
        logical_clock = storage.advance_logical_clock(last_clock)
        return {
            "ok": True,
            "command": "schedule",
            "inserted": saved["inserted"],
            "plan_version": plan.plan_version,
            "logical_clock_ms": logical_clock,
            "schedule": saved["run"],
            "recognized_arbitrations": saved["arbitrations"],
        }


def inject_workspace(
    *,
    db: str,
    plan_version: str | None,
    seed: int | None,
    drift_count: int,
    anomaly_count: int,
    duration_steps: int,
    clear: bool,
    late_subtask_id: str | None = None,
    late_digest: str | None = None,
) -> dict[str, Any]:
    if (late_subtask_id is None) != (late_digest is None):
        raise BusinessError(
            INVALID_ARGUMENT,
            "late-subtask-id and late-digest must be supplied together",
        )
    with Storage(db) as storage:
        plan, tasks, platform, chains, events = _load_runtime(storage, plan_version)
        cleared = storage.clear_drift_events(plan.plan_version) if clear else 0
        injection_seed = plan.global_seed if seed is None else seed
        generated = generate_drift_events(
            plan=plan,
            chains=chains,
            seed=injection_seed,
            count=drift_count,
            anomaly_count=anomaly_count,
            duration_steps=duration_steps,
        )
        late = None
        if late_subtask_id is not None and late_digest is not None:
            late = storage.record_late_result(
                subtask_id=late_subtask_id,
                plan_version=plan.plan_version,
                incoming_digest=late_digest,
            )
        saved = storage.save_drift_events(generated)
        all_events = storage.drift_events(plan.plan_version)
        truth = evaluate_candidates(
            plan=plan,
            chains=chains,
            platform=platform,
            events=all_events,
        )
        return {
            "ok": True,
            "command": "inject",
            "plan_version": plan.plan_version,
            "seed": injection_seed,
            "cleared": cleared,
            "inserted": saved["inserted"],
            "generated_total": saved["total"],
            "events": [event.to_dict() for event in all_events],
            "late_arrival": late,
            "drift_truth": truth["drift_truth"],
            "candidate_comparison": truth["comparison"],
        }


def _closure_for_job(
    closure: dict[str, Any],
    job_id: str,
    subtask_to_job: dict[str, str],
) -> dict[str, Any] | None:
    if closure["reason"] == "cache_layer_change":
        return {
            "closure_id": closure["closure_id"],
            "reason": closure["reason"],
            "affected_count": len(closure["affected"]),
            "reused_count": len(closure["reused"]),
            "protected_committed_count": len(closure["protected_committed"]),
            "details": closure["details"],
        }
    relevant = [
        item
        for item in closure["items"]
        if subtask_to_job.get(item["subtask_id"]) == job_id
    ]
    if not relevant:
        return None
    return {
        "closure_id": closure["closure_id"],
        "reason": closure["reason"],
        "affected_count": len(closure["affected"]),
        "reused_count": len(closure["reused"]),
        "protected_committed_count": len(closure["protected_committed"]),
        "details": closure["details"],
        "items": sorted(relevant, key=lambda item: item["subtask_id"]),
    }


def status_workspace(
    *, db: str, plan_version: str | None = None
) -> dict[str, Any]:
    with Storage(db) as storage:
        version = plan_version or storage.active_plan_version()
        if not version:
            raise BusinessError(NO_ACTIVE_PLAN, "no active plan version")
        plan = storage.get_plan(version)
        tasks = storage.get_tasks()
        subtask_to_job = storage.subtask_job_map()
        closures = storage.closures(version)
        arbitrations = storage.recognized_arbitrations(version)
        ledger = storage.all_ledger()
        events = storage.drift_events(version)
        latest_schedule = storage.latest_schedule(version)
        arbitration_conflicts = storage.arbitration_conflicts()
        pending_attempts = storage.pending_attempts()
        watermarks = storage.watermarks()
        logical_clock = storage.logical_clock_ms()
        evaluation = storage.cost_evaluation(version)

        ledger_by_job: dict[str, list[dict[str, Any]]] = {}
        for entry in ledger:
            ledger_by_job.setdefault(entry["job_id"], []).append(entry)
        arbitration_by_job: dict[str, list[dict[str, Any]]] = {}
        for decision in arbitrations:
            arbitration_by_job.setdefault(decision["job_id"], []).append(decision)
        latest_items: dict[str, list[dict[str, Any]]] = {}
        if latest_schedule:
            for item in latest_schedule.get("items", []):
                latest_items.setdefault(item["job_id"], []).append(item)

        task_rows = []
        all_job_ids = sorted(set(plan.jobs) | set(tasks))
        for job_id in all_job_ids:
            task = tasks.get(job_id)
            job_closures = [
                closure
                for closure in (
                    _closure_for_job(item, job_id, subtask_to_job)
                    for item in closures
                )
                if closure is not None
            ]
            job_anomalies = []
            for event in events:
                if event.anomaly_type is None:
                    continue
                if event.job_id == job_id or (
                    event.subtask_id
                    and subtask_to_job.get(event.subtask_id) == job_id
                ):
                    active = event.active_at(logical_clock)
                    job_anomalies.append(
                        {
                            **event.to_dict(),
                            "persistent": True,
                            "active_now": active,
                        }
                    )
            task_rows.append(
                {
                    "job_id": job_id,
                    "in_plan": job_id in plan.jobs,
                    "revision": task.revision if task else None,
                    "op_kind": task.op_kind if task else None,
                    "dtype": task.dtype if task else None,
                    "execution_ledger": sorted(
                        ledger_by_job.get(job_id, []),
                        key=lambda item: (
                            item["job_revision"],
                            item["subtask_id"],
                        ),
                    ),
                    "arbitration_decisions": sorted(
                        arbitration_by_job.get(job_id, []),
                        key=lambda item: (
                            item["deadline_clock_ms"],
                            item["decision_id"],
                        ),
                    ),
                    "invalidation_closures": sorted(
                        job_closures,
                        key=lambda item: (item["reason"], item["closure_id"]),
                    ),
                    "anomalies": sorted(
                        job_anomalies,
                        key=lambda item: (
                            item["logical_clock_ms"],
                            item["event_id"],
                        ),
                    ),
                    "latest_schedule_items": sorted(
                        latest_items.get(job_id, []),
                        key=lambda item: (item["ordinal"], item["subtask_id"]),
                    ),
                }
            )

        active_anomalies = sorted(
            [
                {
                    **event.to_dict(),
                    "persistent": True,
                    "active_now": event.active_at(logical_clock),
                }
                for event in events
                if event.anomaly_type is not None
            ],
            key=lambda item: (
                item["logical_clock_ms"],
                item["event_id"],
            ),
        )
        output: dict[str, Any] = {
            "ok": True,
            "command": "status",
            "schema_version": 1,
            "plan_version": version,
            "active_plan_version": storage.active_plan_version(),
            "previous_plan_version": storage.previous_plan_version(),
            "logical_clock_ms": logical_clock,
            "watermark": watermarks,
            "cost": {
                "selected_candidate_id": (
                    evaluation["selected_candidate_id"] if evaluation else None
                ),
                "comparison": evaluation["comparison"] if evaluation else [],
                "result_digest": evaluation["result_digest"] if evaluation else None,
            },
            "tasks": task_rows,
            "invalidation_closures": sorted(
                closures, key=lambda item: (item["reason"], item["closure_id"])
            ),
            "recognized_arbitrations": sorted(
                arbitrations,
                key=lambda item: (
                    item["deadline_clock_ms"],
                    item["priority"],
                    item["job_id"],
                    item["decision_id"],
                ),
            ),
            "latest_schedule": (
                {
                    "run_id": latest_schedule["run_id"],
                    "candidate_id": latest_schedule["candidate_id"],
                    "run_config": latest_schedule["run_config"],
                    "result_digest": latest_schedule["result_digest"],
                    "summary": latest_schedule["summary"],
                    "rounds": latest_schedule["rounds"],
                    "anomalies": latest_schedule["anomalies"],
                }
                if latest_schedule
                else None
            ),
            "active_anomalies": active_anomalies,
            "late_arrivals": storage.late_arrivals(version),
            "arbitration_conflicts": arbitration_conflicts,
            "pending_attempts": pending_attempts,
            "summary": {
                "tasks": len(task_rows),
                "ledger_entries": len(ledger),
                "arbitration_decisions": len(arbitrations),
                "invalidation_closures": len(closures),
                "anomalies": len(active_anomalies),
                "late_arrivals": len(storage.late_arrivals(version)),
                "arbitration_conflicts": len(arbitration_conflicts),
                "pending_attempts": len(pending_attempts),
                "logical_clock_ms": logical_clock,
                "watermark_version": watermarks["watermark_version"],
            },
        }
        output["result_digest"] = digest_obj(output)
        return output


def _check(
    checks: list[dict[str, Any]],
    name: str,
    ok: bool,
    *,
    severity: str = "error",
    details: dict[str, Any] | None = None,
) -> None:
    checks.append(
        {
            "check": name,
            "ok": bool(ok),
            "severity": severity,
            "details": details or {},
        }
    )


def verify_workspace(
    *, db: str, plan_version: str | None = None
) -> dict[str, Any]:
    with Storage(db) as storage:
        checks: list[dict[str, Any]] = []
        version = plan_version or storage.active_plan_version()
        if not version:
            raise BusinessError(NO_ACTIVE_PLAN, "no active plan version")
        plan = storage.get_plan(version)
        tasks = storage.get_tasks()
        platform = storage.get_platform_stats()
        events = storage.drift_events(version)
        chains = generate_plan_chains(plan, tasks)
        expected_subtasks = {
            subtask.subtask_id: subtask for subtask in flatten_chains(chains)
        }
        stored_subtasks = {
            subtask.subtask_id: subtask
            for subtask in storage.stored_subtasks(version)
        }
        missing = sorted(set(expected_subtasks) - set(stored_subtasks))
        mismatched = sorted(
            subtask_id
            for subtask_id in expected_subtasks
            if subtask_id in stored_subtasks
            and expected_subtasks[subtask_id].subtask_hash
            != stored_subtasks[subtask_id].subtask_hash
        )
        extras = sorted(set(stored_subtasks) - set(expected_subtasks))
        _check(
            checks,
            "subtasks_regenerated_from_fixed_seed",
            not missing and not mismatched,
            details={"missing": missing, "hash_mismatch": mismatched},
        )
        _check(
            checks,
            "historical_plan_subtask_rows_present",
            True,
            severity="warning",
            details={"extra_historical_or_old_revision": extras},
        )

        all_stored = storage.all_stored_subtasks()
        ledger = storage.all_ledger()
        bad_ledger: list[str] = []
        precision_mismatch: list[str] = []
        for entry in ledger:
            subtask = all_stored.get(entry["subtask_id"])
            if subtask is None:
                bad_ledger.append(entry["subtask_id"])
                continue
            if simulate_output_digest(subtask) != entry["output_digest"]:
                bad_ledger.append(entry["subtask_id"])
            if subtask.precision_sensitive != entry["precision_sensitive"]:
                precision_mismatch.append(entry["subtask_id"])
        _check(
            checks,
            "ledger_outputs_rebuild_exactly",
            not bad_ledger,
            details={"bad_subtask_ids": bad_ledger, "ledger_entries": len(ledger)},
        )
        _check(
            checks,
            "precision_sensitive_checkpoints",
            not precision_mismatch,
            details={"mismatched_subtask_ids": precision_mismatch},
        )

        duplicate_count = storage.duplicate_ledger_count()
        _check(
            checks,
            "unique_subtask_ledger",
            duplicate_count == 0,
            details={"duplicate_count": duplicate_count},
        )
        pending_attempts = storage.pending_attempts()
        _check(
            checks,
            "no_unrecovered_pending_attempts",
            not pending_attempts,
            details={
                "pending_attempt_ids": [
                    attempt["attempt_id"] for attempt in pending_attempts
                ]
            },
        )
        integrity = storage.integrity_check()
        _check(
            checks,
            "sqlite_integrity",
            integrity == "ok",
            details={"integrity_check": integrity},
        )

        recomputed = evaluate_candidates(
            plan=plan,
            chains=chains,
            platform=platform,
            events=events,
        )
        stored_evaluation = storage.cost_evaluation(version)
        _check(
            checks,
            "cost_evaluation_reproducible",
            stored_evaluation is not None
            and stored_evaluation.get("result_digest") == recomputed["result_digest"],
            details={
                "stored_digest": (
                    stored_evaluation.get("result_digest")
                    if stored_evaluation
                    else None
                ),
                "recomputed_digest": recomputed["result_digest"],
            },
        )
        forecast_count = len(storage.cost_forecasts(version))
        expected_forecast_count = sum(
            len(result["steps"]) for result in recomputed["candidates"].values()
        )
        _check(
            checks,
            "cost_aggregation_order_persisted",
            forecast_count == expected_forecast_count,
            details={
                "stored_steps": forecast_count,
                "expected_steps": expected_forecast_count,
            },
        )

        budget_errors: list[dict[str, Any]] = []
        timeout_abandons: list[str] = []
        for run in storage.schedule_runs(version):
            item_costs = {
                item["subtask_id"]: float(item["cost"]["total_cost"])
                for item in run["items"]
            }
            for round_data in run["rounds"]:
                if float(round_data["used_budget"]) > float(round_data["budget"]) + 1e-9:
                    budget_errors.append(
                        {
                            "run_id": run["run_id"],
                            "round": round_data["round_index"],
                            "reason": "budget_exceeded",
                        }
                    )
                if int(round_data["slots_used"]) > int(round_data["slots"]):
                    budget_errors.append(
                        {
                            "run_id": run["run_id"],
                            "round": round_data["round_index"],
                            "reason": "concurrency_exceeded",
                        }
                    )
                expected_used = round(
                    sum(
                        item_costs.get(subtask_id, 0.0)
                        for subtask_id in round_data["started_subtask_ids"]
                    ),
                    6,
                )
                if abs(expected_used - float(round_data["used_budget"])) > 1e-6:
                    budget_errors.append(
                        {
                            "run_id": run["run_id"],
                            "round": round_data["round_index"],
                            "reason": "launch_cost_accounting_mismatch",
                            "expected": expected_used,
                            "actual": round_data["used_budget"],
                        }
                    )
            for item in run["items"]:
                if item["status"] == "abandoned_budget" and "timeout" in item["reason"]:
                    timeout_abandons.append(item["subtask_id"])
        _check(
            checks,
            "schedule_budget_and_concurrency",
            not budget_errors,
            details={"errors": budget_errors},
        )
        _check(
            checks,
            "active_abandon_is_not_timeout",
            not timeout_abandons,
            details={"subtask_ids": timeout_abandons},
        )

        conflicts = storage.arbitration_conflicts()
        _check(
            checks,
            "deadline_arbitration_recognized_once",
            not conflicts,
            severity="warning" if conflicts else "error",
            details={
                "conflicting_replays_preserved_first_decision": len(conflicts),
            },
        )

        late = storage.late_arrivals(version)
        late_bad = []
        for arrival in late:
            entry = storage.ledger_entry(arrival["subtask_id"])
            if entry is None or entry["output_digest"] != arrival["accepted_digest"]:
                late_bad.append(arrival["late_id"])
        _check(
            checks,
            "late_arrivals_do_not_change_accepted_results",
            not late_bad,
            details={"bad_late_ids": late_bad, "late_arrivals": len(late)},
        )

        closures = storage.closures(version)
        fingerprints = [closure["fingerprint"] for closure in closures]
        protected_missing = []
        for closure in closures:
            for subtask_id in closure["protected_committed"]:
                if storage.ledger_entry(subtask_id) is None:
                    protected_missing.append(subtask_id)
        _check(
            checks,
            "incremental_invalidations_are_unique",
            len(fingerprints) == len(set(fingerprints)),
            details={"closures": len(closures)},
        )
        _check(
            checks,
            "committed_results_protected_from_revision_closure",
            not protected_missing,
            details={"missing_protected": sorted(protected_missing)},
        )

        watermark = storage.watermarks()
        watermark_errors = []
        for row in watermark["watermarks"]:
            chain = chains.get(row["job_id"])
            if chain is None:
                watermark_errors.append(
                    {"job_id": row["job_id"], "reason": "chain_missing"}
                )
                continue
            if row["revision"] != chain.revision:
                watermark_errors.append(
                    {"job_id": row["job_id"], "reason": "revision_mismatch"}
                )
            if any(
                storage.ledger_entry(subtask.subtask_id) is None
                for subtask in chain.subtasks
            ):
                watermark_errors.append(
                    {"job_id": row["job_id"], "reason": "ledger_incomplete"}
                )
        _check(
            checks,
            "watermark_requires_complete_ledger",
            not watermark_errors,
            details={"errors": watermark_errors},
        )

        ok = all(
            check["ok"]
            for check in checks
            if check["severity"] == "error"
        )
        output = {
            "ok": ok,
            "command": "verify",
            "plan_version": version,
            "checks": checks,
            "summary": {
                "passed": sum(1 for check in checks if check["ok"]),
                "failed": sum(1 for check in checks if not check["ok"]),
                "errors": sum(
                    1
                    for check in checks
                    if not check["ok"] and check["severity"] == "error"
                ),
                "warnings": sum(
                    1 for check in checks if not check["ok"] and check["severity"] == "warning"
                ),
            },
        }
        output["result_digest"] = digest_obj(output)
        return output

