from transfer_scheduler.errors import (
    E_COMPUTE_BUDGET,
    E_MEMORY_BUDGET,
    E_SLOT_BUDGET,
)
from transfer_scheduler.planner import (
    build_schedule_plan,
    estimate_compute,
    estimate_memory,
    shared_statistics_memory,
)


def test_all_admitted_under_generous_budget(tasks, config):
    plan = build_schedule_plan(tasks, config.budget)
    assert set(plan.admitted) == {task.task_id for task in tasks}
    assert not plan.rejected
    assert all(len(group) <= config.budget.parallel_slots for group in plan.groups)
    assert plan.total_compute <= config.budget.compute_budget
    assert plan.peak_memory <= config.budget.memory_budget_bytes


def test_groups_are_deterministic_and_partition_admitted(tasks, config):
    first = build_schedule_plan(tasks, config.budget)
    second = build_schedule_plan(list(reversed(tasks)), config.budget)
    assert first.groups == second.groups
    flattened = [task_id for group in first.groups for task_id in group]
    assert sorted(flattened) == sorted(first.admitted)


def test_compute_budget_rejection_code(tasks, tight_config):
    plan = build_schedule_plan(tasks, tight_config.budget)
    codes = {item.code for item in plan.rejected}
    assert codes, "收紧预算后应当出现拒绝"
    assert E_COMPUTE_BUDGET in codes or E_MEMORY_BUDGET in codes or E_SLOT_BUDGET in codes
    for item in plan.rejected:
        assert item.conflict_type
        assert item.reason


def test_slot_budget_explicitly_reported(tasks, tight_config):
    from dataclasses import replace

    budget = replace(tight_config.budget, max_queued_tasks=2, compute_budget=10**12,
                     memory_budget_bytes=10**12)
    plan = build_schedule_plan(tasks, budget)
    assert len(plan.admitted) == 2
    assert any(item.code == E_SLOT_BUDGET for item in plan.rejected)


def test_memory_budget_rejection(tasks, tight_config):
    from dataclasses import replace

    budget = replace(tight_config.budget, memory_budget_bytes=200_000,
                     compute_budget=10**12, max_queued_tasks=99)
    plan = build_schedule_plan(tasks, budget)
    assert any(item.code == E_MEMORY_BUDGET for item in plan.rejected)


def test_arbitration_order_priority_first(tasks, config):
    plan = build_schedule_plan(tasks, config.budget)
    ranks = {
        task.task_id: task.priority
        for task in sorted(tasks, key=lambda item: item.task_id)
    }
    # 准入序列的优先级在同共享收益/样本维度下应整体不劣于被拒任务。
    rejected_ids = {item.task_id for item in plan.rejected}
    if rejected_ids:
        admitted_priorities = [ranks[task_id] for task_id in plan.admitted]
        rejected_priorities = [ranks[task_id] for task_id in rejected_ids]
        assert min(admitted_priorities) <= max(rejected_priorities)


def test_shared_statistics_counted_once(tasks, config):
    memory, keys = shared_statistics_memory(tasks)
    assert len(keys) == len(set(keys))
    plan = build_schedule_plan(tasks, config.budget)
    assert plan.shared_stat_memory == memory


def test_estimates_are_positive_and_documented(tasks, config):
    task = tasks[0]
    compute = estimate_compute(task, task.sample_count)
    memory = estimate_memory(task, task.sample_count)
    assert compute > 0 and memory > 0
    plan = build_schedule_plan(tasks, config.budget)
    assert all("compute=" in estimate.basis or "computed_compute" in estimate.basis
               for estimate in plan.estimates)
