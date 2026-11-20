from dataclasses import replace

import pytest

from transfer_scheduler.config import BudgetConfig
from transfer_scheduler.demo import (
    demo_budget_conflict,
    demo_threshold_stability,
    demo_worker_failure,
)
from transfer_scheduler.errors import E_INCONSISTENT, EInconsistent
from transfer_scheduler.executor import FaultInjection, ParallelExecutor, merge_results
from transfer_scheduler.planner import build_schedule_plan
from transfer_scheduler.service import WorkflowService
from transfer_scheduler.snapshots import LEDGER_KIND, PLAN_KIND, SUMMARY_KIND


def test_full_workflow_in_process(tasks, config, tmp_path):
    service = WorkflowService(config, tmp_path)
    summary = service.run(tasks, run_id="integration", in_process=True)
    assert set(summary.admitted) == {task.task_id for task in tasks}
    assert set(summary.scores) == set(summary.admitted)
    assert not summary.unavailable
    assert summary.evidence["admission_ledger"] == "ok"
    assert summary.evidence["serial_baseline"]["max_abs_score_diff"] == 0.0
    assert service.snapshots.current(PLAN_KIND).version == 1
    assert service.snapshots.current(LEDGER_KIND).version == 1
    assert service.snapshots.current(SUMMARY_KIND).version == 1


def test_budget_conflict_demo_has_structured_rejections(tasks, tmp_path):
    summary = demo_budget_conflict(tasks, tmp_path)
    assert summary.rejected
    for item in summary.rejected:
        assert item["code"].startswith("E_")
        assert item["conflict_type"]
    rejected_ids = {item["task_id"] for item in summary.rejected}
    assert rejected_ids.isdisjoint(summary.scores)


def test_worker_failure_isolated_and_marked_unavailable(tasks, config, tmp_path):
    plan = build_schedule_plan(tasks, config.budget)
    injection = FaultInjection(failures={tasks[0].task_id: "adapter_train"})
    executor = ParallelExecutor(config, injection)
    results = executor.execute_in_process(plan, tasks)
    ok, failed = merge_results(results)
    assert tasks[0].task_id in failed
    assert failed[tasks[0].task_id].code == "E_WORKER_FAILURE"
    assert failed[tasks[0].task_id].stage == "adapter_train"
    assert len(ok) == len(tasks) - 1


def test_spawn_pool_matches_serial_baseline(tasks, config, tmp_path):
    plan = build_schedule_plan(tasks, config.budget)
    executor = ParallelExecutor(config)
    results = executor.execute(plan, tasks)
    ok, failed = merge_results(results)
    assert not failed
    from transfer_scheduler.consistency import check_parallel_vs_serial

    diffs = check_parallel_vs_serial(
        plan, tasks, config, ok, config.execution.serial_precision
    )
    assert all(diff == 0.0 for diff in diffs.values())


def test_failure_demo_summary_marks_unavailable(tasks, tmp_path):
    summary = demo_worker_failure(tasks, tmp_path, in_process=True)
    assert summary.unavailable
    for item in summary.unavailable.values():
        assert item["status"] == "unavailable"
        assert item["code"] == "E_WORKER_FAILURE"


def test_threshold_demo_is_repeat_stable():
    decisions = demo_threshold_stability()
    assert decisions["repeat_stable"] is True


def test_restart_restores_snapshot_versions(tasks, config, tmp_path):
    service = WorkflowService(config, tmp_path)
    service.run(tasks, run_id="before-restart", in_process=True)
    revived = WorkflowService(config, tmp_path)
    versions = revived.restore_from_snapshots()
    assert versions[PLAN_KIND] == 1
    assert versions[LEDGER_KIND] == 1
    assert versions[SUMMARY_KIND] == 1


def test_inconsistency_detected_on_tampered_summary(tasks, config, tmp_path):
    service = WorkflowService(config, tmp_path)
    plan = build_schedule_plan(tasks, config.budget)
    service.ledger.reconcile_with_plan(plan)
    from transfer_scheduler.pipeline import FailedTask

    # 用伪造 FailedTask 制造摘要集合缺员。
    tampered = {
        task.task_id: FailedTask(
            task_id=task.task_id,
            status="unavailable",
            code="E_WORKER_FAILURE",
            stage="unknown",
            message="x",
        )
        for task in tasks[:-1]
    }
    with pytest.raises(EInconsistent) as info:
        from transfer_scheduler.consistency import check_summary_membership

        check_summary_membership(plan, tampered)
    assert info.value.context["category"] == "summary_membership"


def test_rejected_task_never_leaks_into_summary(tasks, tight_config, tmp_path):
    service = WorkflowService(tight_config, tmp_path)
    summary = service.run(tasks, run_id="tight", in_process=True, skip_serial_check=True)
    rejected = {item["task_id"] for item in summary.rejected}
    assert rejected.isdisjoint(summary.scores)
    assert rejected.isdisjoint(summary.unavailable)
