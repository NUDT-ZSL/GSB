"""跨模块一致性自检。

覆盖业务要求 8 的全部核对项：

1. 准入集合与预算账本可核对；
2. 同 target_domain 共享只读统计量只计一次驻留；
3. 执行摘要的 task_id 集合与准入集合并集一致；
4. 被拒任务不出现在执行摘要中；
5. 并行结果与按 task_id 升序串行基线在声明精度内一致。

任一不一致抛 :class:`EInconsistent`，上下文指明类别与双方取值。
"""

from __future__ import annotations

import math
from typing import Any

from .data import TaskSpec
from .errors import EInconsistent
from .executor import FailedTask
from .ledger import BudgetLedger
from .pipeline import TaskResult, run_serial
from .planner import SchedulePlan, shared_statistics_memory


def check_admission_vs_ledger(
    plan: SchedulePlan,
    ledger: BudgetLedger,
    released_failed: set[str] | None = None,
) -> None:
    """准入集合 = 账本仍预留集合 ∪ 已释放的失败任务集合。"""
    snapshot = ledger.snapshot()
    admitted = set(plan.admitted)
    reserved = set(snapshot["reservations"])
    released_failed = released_failed or set()
    if admitted != reserved | released_failed:
        raise EInconsistent(
            "准入集合与账本预留集合不一致",
            category="admission_ledger",
            admitted=sorted(admitted),
            reserved=sorted(reserved),
            released_failed=sorted(released_failed),
        )
    planned_compute = sum(
        item.compute_units
        for item in plan.estimates
        if item.task_id not in released_failed
    )
    if planned_compute != snapshot["compute_used"]:
        raise EInconsistent(
            "账本计算扣减与计划估算不一致",
            category="admission_ledger",
            planned_compute=planned_compute,
            ledger_compute=snapshot["compute_used"],
        )


def check_shared_statistics_counted_once(plan: SchedulePlan, tasks: list[TaskSpec]) -> None:
    admitted_tasks = [task for task in tasks if task.task_id in set(plan.admitted)]
    expected_mem, keys = shared_statistics_memory(admitted_tasks)
    if expected_mem != plan.shared_stat_memory:
        raise EInconsistent(
            "共享统计量驻留占用与计划不一致",
            category="shared_statistics",
            expected=expected_mem,
            planned=plan.shared_stat_memory,
        )
    # 同一 (domain, dim) 键只应出现一次。
    if len(keys) != len(set(keys)):
        raise EInconsistent(
            "共享统计量被重复计入",
            category="shared_statistics",
            keys=list(keys),
        )


def check_summary_membership(
    plan: SchedulePlan,
    results: dict[str, TaskResult | FailedTask],
) -> None:
    admitted = set(plan.admitted)
    summary_ids = set(results)
    if admitted != summary_ids:
        raise EInconsistent(
            "执行摘要 task_id 集合与准入集合并集不一致",
            category="summary_membership",
            admitted=sorted(admitted),
            summary=sorted(summary_ids),
        )
    rejected_ids = {item.task_id for item in plan.rejected}
    leaked = rejected_ids & summary_ids
    if leaked:
        raise EInconsistent(
            "被拒任务出现在执行摘要中",
            category="rejected_leak",
            leaked=sorted(leaked),
        )


def check_parallel_vs_serial(
    plan: SchedulePlan,
    tasks: list[TaskSpec],
    config,
    parallel_ok: dict[str, TaskResult],
    precision: float,
) -> dict[str, float]:
    """并行成功结果 vs task_id 升序串行基线，逐任务分数与指纹比对。"""
    ok_ids = set(parallel_ok)
    admitted_tasks = [
        task
        for task in tasks
        if task.task_id in set(plan.admitted) and task.task_id in ok_ids
    ]
    serial = run_serial(
        admitted_tasks,
        config.pipeline,
        config.transfer_gate,
        config.score_gate,
        effective_samples={
            key: value
            for key, value in plan.effective_samples.items()
            if key in ok_ids
        },
    )
    if set(serial) != set(parallel_ok):
        raise EInconsistent(
            "串行基线与并行成功集合不一致",
            category="serial_baseline",
            serial=sorted(serial),
            parallel=sorted(parallel_ok),
        )
    diffs: dict[str, float] = {}
    for task_id, serial_result in serial.items():
        parallel_result = parallel_ok[task_id]
        score_diff = abs(parallel_result.score - serial_result.score)
        mse_diff = abs(parallel_result.test_mse - serial_result.test_mse)
        if not math.isfinite(score_diff) or score_diff > precision:
            raise EInconsistent(
                f"任务 {task_id} 并行/串行分数超出声明精度",
                category="serial_baseline",
                task_id=task_id,
                score_diff=score_diff,
                precision=precision,
            )
        if mse_diff > precision:
            raise EInconsistent(
                f"任务 {task_id} 并行/串行 MSE 超出声明精度",
                category="serial_baseline",
                task_id=task_id,
                mse_diff=mse_diff,
                precision=precision,
            )
        if parallel_result.fingerprints != serial_result.fingerprints:
            raise EInconsistent(
                f"任务 {task_id} 并行/串行中间量指纹不一致",
                category="serial_baseline",
                task_id=task_id,
                parallel=parallel_result.fingerprints,
                serial=serial_result.fingerprints,
            )
        diffs[task_id] = score_diff
    return diffs


def run_all_checks(
    plan: SchedulePlan,
    ledger: BudgetLedger,
    tasks: list[TaskSpec],
    results: dict[str, TaskResult | FailedTask],
    config,
    skip_serial: bool = False,
    released_failed: set[str] | None = None,
) -> dict[str, Any]:
    """执行全部可在当前上下文完成的自检，返回核对证据。"""
    check_admission_vs_ledger(plan, ledger, released_failed)
    check_shared_statistics_counted_once(plan, tasks)
    check_summary_membership(plan, results)
    evidence = {
        "admission_ledger": "ok",
        "shared_statistics": "ok",
        "summary_membership": "ok",
        "rejected_leak": "ok",
    }
    if not skip_serial:
        parallel_ok = {
            task_id: result
            for task_id, result in results.items()
            if isinstance(result, TaskResult)
        }
        diffs = check_parallel_vs_serial(
            plan, tasks, config, parallel_ok, config.execution.serial_precision
        )
        evidence["serial_baseline"] = {"max_abs_score_diff": max(diffs.values(), default=0.0)}
    return evidence

