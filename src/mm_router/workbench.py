"""End-to-end deterministic acceptance run for the local diagnostic workbench."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .cache import ContentAddressedCache, plan_invalidation
from .canonical import atomic_write_json
from .checkpoint import CheckpointStore, schedule_key
from .config_io import read_config
from .diagnostics import (
    build_diagnostic_report,
    verify_decision_determinism,
    verify_invalidation_plan,
    verify_recovery_consistency,
)
from .errors import WorkbenchError
from .generate import generate_sample, read_requests
from .ledger import CommitLedger, StageLock
from .routing import RoutingPipeline
from .scheduler import (
    ResourcePolicy,
    ScheduleResult,
    SimulatedTermination,
    run_schedule,
)


def _fresh_directory(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _catch_code(function: Any) -> dict[str, Any]:
    try:
        value = function()
        return {"business_code": "OK", "value": value}
    except WorkbenchError as exc:
        return exc.to_dict()


def _run_scheduler_once(
    requests: Any,
    config: Any,
    *,
    root: Path,
    lock: StageLock,
    resource_policy: ResourcePolicy,
    stop_after_batches: int | None,
    use_processes: bool,
) -> ScheduleResult:
    return run_schedule(
        requests,
        config,
        ContentAddressedCache(root / "cache"),
        CheckpointStore(root / "checkpoints", lock=lock),
        workers=config.scheduler_workers,
        batch_size=4,
        resource_policy=resource_policy,
        use_processes=use_processes,
        stop_after_batches=stop_after_batches,
    )


def run_acceptance_demo(
    root: str | Path,
    *,
    seed: int,
    use_processes: bool = True,
) -> dict[str, Any]:
    run_root = _fresh_directory(Path(root))
    sample = generate_sample(run_root / "sample", seed=seed)
    requests_v1 = read_requests(sample["requests"])
    requests_v2 = read_requests(sample["evolved_requests"])
    config_v1 = read_config(sample["config_v1"]).config
    config_v2 = read_config(
        sample["config_v2"],
        previous_updated_at_ms=config_v1.updated_at_ms,
    ).config
    upgraded_v1 = read_config(sample["upgraded_v2"]).config
    downgraded_v2 = read_config(sample["downgraded_v1"]).config

    cache = ContentAddressedCache(run_root / "routing_cache")
    pipeline = RoutingPipeline(cache)
    first_v1 = tuple(pipeline.execute(request, config_v1) for request in requests_v1)
    replayed_v1 = tuple(pipeline.execute(request, config_v1) for request in requests_v1)
    deterministic_check = verify_decision_determinism(
        [item.to_dict() for item in first_v1],
        [item.to_dict() for item in replayed_v1],
    )
    invalidation = plan_invalidation(
        requests_v1,
        requests_v2,
        config_v1,
        config_v2,
    )
    evolved_decisions = tuple(
        pipeline.execute(request, config_v2) for request in requests_v2
    )
    evolved_statuses = {
        decision.request_id: decision.statuses() for decision in evolved_decisions
    }
    invalidation_check = verify_invalidation_plan(
        invalidation,
        cache_statuses=evolved_statuses,
    )
    shared_lock = StageLock(run_root / "stage.lock")
    generous_policy = ResourcePolicy(
        max_float_ops=5_000_000,
        max_resident_mb=128.0,
        priority=("precision", "latency", "memory"),
    )
    termination: dict[str, Any]
    try:
        _run_scheduler_once(
            requests_v1,
            config_v1,
            root=run_root / "scheduler_recovery",
            lock=shared_lock,
            resource_policy=generous_policy,
            stop_after_batches=2,
            use_processes=use_processes,
        )
        termination = {"type": "none"}
    except SimulatedTermination as exc:
        termination = exc.to_dict()
    recovered = _run_scheduler_once(
        requests_v1,
        config_v1,
        root=run_root / "scheduler_recovery",
        lock=shared_lock,
        resource_policy=generous_policy,
        stop_after_batches=None,
        use_processes=use_processes,
    )
    full_replay = _run_scheduler_once(
        requests_v1,
        config_v1,
        root=run_root / "scheduler_full_replay",
        lock=shared_lock,
        resource_policy=generous_policy,
        stop_after_batches=None,
        use_processes=use_processes,
    )
    recovery_check = verify_recovery_consistency(
        recovered.decisions,
        full_replay.decisions,
    )

    budget_policy = ResourcePolicy(
        max_float_ops=15_000,
        max_resident_mb=30.0,
        priority=("precision", "latency", "memory"),
    )
    budget_result: dict[str, Any]
    try:
        budget_schedule = _run_scheduler_once(
            requests_v1,
            config_v1,
            root=run_root / "scheduler_budget",
            lock=shared_lock,
            resource_policy=budget_policy,
            stop_after_batches=None,
            use_processes=use_processes,
        )
        budget_result = {
            "business_code": budget_schedule.business_code,
            "resource_decision": budget_schedule.resource_decision.to_dict(),
            "actual_weighted_float_ops": budget_schedule.actual_weighted_float_ops,
        }
    except WorkbenchError as exc:
        budget_result = exc.to_dict()

    ledger_root = run_root / "commit_ledger"
    ledger = CommitLedger(ledger_root, lock=shared_lock)
    first_commit = ledger.commit(
        kind="config",
        snapshot={"generation": 1, "config": config_v1.semantic_dict()},
        version=1,
    )
    busy_errors: list[dict[str, Any]] = []
    with shared_lock.hold():
        busy_errors.append(
            _catch_code(
                lambda: ledger.commit(
                    kind="config",
                    snapshot={"generation": 2, "config": config_v2.semantic_dict()},
                    version=2,
                )
            )
        )
        busy_errors.append(
            _catch_code(
                lambda: CheckpointStore(
                    run_root / "stage_busy_checkpoint",
                    lock=shared_lock,
                ).commit_batch(
                    schedule_key=schedule_key(requests_v1, config_v1.fingerprint()),
                    completed_request_ids=[requests_v1[0].request_id],
                    decision_records={
                        requests_v1[0].request_id: recovered.decisions[0]
                    },
                    batch_index=0,
                )
            )
        )
    drift_commit = ledger.commit(
        kind="config",
        snapshot={"generation": 2, "config": config_v2.semantic_dict()},
        version=2,
        expected_parent_version=999,
        expected_parent_checksum="0" * 64,
    )
    rollback_error = _catch_code(
        lambda: ledger.commit(
            kind="config",
            snapshot={"generation": 1, "config": config_v1.semantic_dict()},
            version=1,
        )
    )

    report = build_diagnostic_report(
        cache=cache,
        requests=requests_v2,
        config=config_v2,
        invalidation_plan=invalidation,
        cache_statuses=evolved_statuses,
        recovered_records=recovered.decisions,
        replay_records=full_replay.decisions,
        deterministic_replay_records=[item.to_dict() for item in replayed_v1],
    )
    report_path = report.write(run_root / "diagnostic_report.json")

    summary = {
        "schema": "mm-router-acceptance-summary",
        "seed": seed,
        "environment": {
            "platform": "Linux/WSL Ubuntu",
            "process_pool_used": use_processes,
        },
        "sample": sample,
        "configs": {
            "v1_generation": config_v1.generation,
            "v1_revision": config_v1.revision,
            "v1_thresholds": config_v1.threshold_map(),
            "v2_generation": config_v2.generation,
            "v2_revision": config_v2.revision,
            "v2_thresholds": config_v2.threshold_map(),
            "v1_upgraded_generation": upgraded_v1.generation,
            "v1_upgraded_tombstones": len(upgraded_v1.tombstones),
            "v2_downgraded_thresholds": downgraded_v2.threshold_map(),
        },
        "routing": {
            "request_count": len(requests_v1),
            "byte_identical_replay": deterministic_check.to_dict(),
            "first_business_codes": {
                decision.request_id: decision.business_code
                for decision in first_v1
            },
            "total_operations": sum(
                decision.total_operations for decision in first_v1
            ),
        },
        "invalidation": {
            "plan": invalidation.to_dict(),
            "check": invalidation_check.to_dict(),
        },
        "recovery": {
            "termination": termination,
            "checkpoint_version": recovered.checkpoint_version,
            "recovered_request_ids": list(recovered.recovered_request_ids),
            "recomputed_request_ids": list(recovered.recomputed_request_ids),
            "check": recovery_check.to_dict(),
        },
        "resource_adjudication": budget_result,
        "commits": {
            "first_commit": first_commit.to_dict(),
            "busy_errors": busy_errors,
            "drift_commit": drift_commit.to_dict(),
            "rollback_error": rollback_error,
        },
        "diagnostics": {
            "report_path": str(report_path),
            "code": report.code,
            "checks": [check.to_dict() for check in report.checks],
        },
    }
    summary_path = run_root / "summary.json"
    atomic_write_json(summary_path, summary)
    summary["summary_path"] = str(summary_path)
    summary["diagnostic_report"] = str(report_path)
    return summary

