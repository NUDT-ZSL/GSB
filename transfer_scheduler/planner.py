"""三重约束调度计划器。

为每个任务估算计算代价（整数计算单元）与驻留内存（字节），在
并行槽位、总计算预算、驻留内存三重约束下决定准入集合、执行顺序与
并行分组，并完整记录估算依据、裁决路径与冲突类型。

裁决优先级顺序由 :attr:`BudgetConfig.arbitration_order` 声明，默认：
任务优先级 → 共享统计量收益 → 样本规模（同级以 task_id 兜底，保证确定）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import ADAPTER_KIND_HYPERPARAMS, BudgetConfig
from .data import TaskSpec, domain_statistics
from .errors import (
    E_COMPUTE_BUDGET,
    E_MEMORY_BUDGET,
    E_SLOT_BUDGET,
)

FLOAT64_BYTES = 8


@dataclass(frozen=True)
class CostEstimate:
    """单任务资源估算，公式同时以字符串形式留档。"""

    task_id: str
    compute_units: int
    memory_bytes: int
    effective_sample_count: int
    degraded: bool
    basis: str


@dataclass(frozen=True)
class RejectedTask:
    """被拒/降级任务的结构化裁决记录。"""

    task_id: str
    code: str
    conflict_type: str
    reason: str
    arbitration_rank: int
    requested_compute: int
    requested_memory: int


@dataclass(frozen=True)
class SchedulePlan:
    """调度计划（不可变快照源）。"""

    plan_id: str
    admitted: tuple[str, ...]
    rejected: tuple[RejectedTask, ...]
    groups: tuple[tuple[str, ...], ...]
    estimates: tuple[CostEstimate, ...]
    arbitration_trail: tuple[str, ...]
    effective_samples: dict[str, int]
    total_compute: int
    peak_memory: int
    shared_stat_memory: int
    budget: dict
    paths: tuple[str, ...]


def estimate_compute(task: TaskSpec, effective_samples: int) -> int:
    """计算代价 = 数据生成 + 训练步进 + 目标域评估，全部为整数单元。"""
    hyper = ADAPTER_KIND_HYPERPARAMS[task.adapter_kind]
    data_cost = effective_samples * task.input_dim
    train_cost = hyper["steps"] * effective_samples * task.input_dim
    eval_count = max(16, effective_samples // 4)
    eval_cost = eval_count * task.input_dim
    return int(data_cost + train_cost + eval_cost)


def estimate_memory(task: TaskSpec, effective_samples: int) -> int:
    """驻留内存估算（float64 字节数），训练显式计入梯度缓冲。"""
    n = effective_samples
    d = task.input_dim
    buffers = {
        "features": n * d,
        "standardized": n * d,
        "mapped": n * d,
        "train_xy": 2 * n * d,
        "weights_truth": d,
        "adapter_weight": d,
        "gradient_buffer": d,
        "target_eval": max(16, n // 4) * d,
    }
    return int(FLOAT64_BYTES * sum(buffers.values()))


def shared_statistics_memory(tasks: list[TaskSpec]) -> tuple[int, tuple[str, ...]]:
    """同 target_domain 的只读统计量全局共享，只计一次驻留占用。"""
    keys = sorted({(task.target_domain, task.input_dim) for task in tasks})
    total = 0
    for domain, dim in keys:
        stats = domain_statistics(domain, dim)
        total += FLOAT64_BYTES * (stats["mean"].size + stats["std"].size)
    return total, tuple(f"{domain}:{dim}" for domain, dim in keys)


def _arbitration_key(task: TaskSpec, shared_benefit: dict[str, int], order: tuple[str, ...]):
    parts = []
    for key in order:
        if key == "priority":
            parts.append(task.priority)  # 数值小优先级高
        elif key == "shared_stat_benefit":
            # 共享收益越大越优先 → 取负实现升序优先
            parts.append(-shared_benefit[task.task_id])
        elif key == "sample_count":
            parts.append(task.sample_count)  # 样本小者先准入，节省预算
        else:
            raise ValueError(f"未知裁决维度: {key}")
    parts.append(task.task_id)  # 确定性兜底
    return tuple(parts)


def _shared_benefit(tasks: list[TaskSpec]) -> dict[str, int]:
    """每个任务因 target_domain 共享而节省的统计量内存（字节）。"""
    group_count: dict[tuple[str, int], int] = {}
    for task in tasks:
        group_count[(task.target_domain, task.input_dim)] = (
            group_count.get((task.target_domain, task.input_dim), 0) + 1
        )
    benefit = {}
    for task in tasks:
        count = group_count[(task.target_domain, task.input_dim)]
        saved = FLOAT64_BYTES * 2 * task.input_dim * (count - 1)
        benefit[task.task_id] = saved
    return benefit


def build_schedule_plan(
    tasks: list[TaskSpec],
    config: BudgetConfig,
    plan_id: str = "plan-0001",
) -> SchedulePlan:
    """按声明裁决顺序贪心构造调度计划。

    路径语义：
    * ``admit`` —— 直接准入；
    * ``degrade`` —— 计算预算不足时按最小样本比例降级后准入；
    * 拒绝码 —— :data:`E_COMPUTE_BUDGET` / :data:`E_MEMORY_BUDGET` /
      :data:`E_SLOT_BUDGET`，冲突类型与依据全部留痕。
    """
    ordered = sorted(tasks, key=lambda item: item.task_id)
    benefit = _shared_benefit(ordered)
    ranked = sorted(
        ordered,
        key=lambda item: _arbitration_key(item, benefit, config.arbitration_order),
    )
    shared_mem = 0
    shared_keys: tuple[str, ...] = ()
    max_queued = config.max_queued_tasks if config.max_queued_tasks is not None else len(ordered)

    admitted: list[TaskSpec] = []
    rejected: list[RejectedTask] = []
    estimates: list[CostEstimate] = []
    trail: list[str] = []
    paths: list[str] = []
    effective_samples: dict[str, int] = {}
    used_compute = 0

    for rank, task in enumerate(ranked):
        compute = estimate_compute(task, task.sample_count)
        memory = estimate_memory(task, task.sample_count)
        conflicts = []
        if len(admitted) >= max_queued:
            conflicts.append("slot")
        if used_compute + compute > config.compute_budget:
            conflicts.append("compute")
        group_peak = _peak_memory_with(admitted, task, task.sample_count, shared_mem, config)
        if group_peak > config.memory_budget_bytes:
            conflicts.append("memory")
        if not conflicts:
            admitted.append(task)
            effective_samples[task.task_id] = task.sample_count
            used_compute += compute
            estimates.append(
                CostEstimate(
                    task.task_id,
                    compute,
                    memory,
                    task.sample_count,
                    False,
                    _basis(task, task.sample_count, compute, memory),
                )
            )
            trail.append(
                f"rank={rank} {task.task_id} admit "
                f"(priority={task.priority}, shared_benefit={benefit[task.task_id]}, "
                f"samples={task.sample_count}, compute={compute})"
            )
            paths.append(f"{task.task_id}:admit")
            _, shared_keys = shared_statistics_memory(admitted)
            continue

        # 计算冲突优先尝试降级；内存/槽位冲突不降级。
        if config.degrade_on_compute and "compute" in conflicts and "memory" not in conflicts and "slot" not in conflicts:
            minimum = max(1, int(np.ceil(task.sample_count * config.min_sample_fraction)))
            reduced = _largest_fitting_samples(task, config, used_compute, shared_mem, admitted, minimum)
            if reduced is not None:
                reduced_compute = estimate_compute(task, reduced)
                reduced_memory = estimate_memory(task, reduced)
                admitted.append(task)
                effective_samples[task.task_id] = reduced
                used_compute += reduced_compute
                estimates.append(
                    CostEstimate(
                        task.task_id,
                        reduced_compute,
                        reduced_memory,
                        reduced,
                        True,
                        _basis(task, reduced, reduced_compute, reduced_memory)
                        + f" [degraded {task.sample_count}->{reduced}]",
                    )
                )
                trail.append(
                    f"rank={rank} {task.task_id} degrade samples {task.sample_count}->{reduced} "
                    f"compute {compute}->{reduced_compute}"
                )
                paths.append(f"{task.task_id}:degrade")
                continue

        code, conflict_type = _conflict_code(conflicts)
        reason = (
            f"约束不足: {','.join(conflicts)}; 已用计算={used_compute}/"
            f"{config.compute_budget}, 槽位上限={max_queued}, 共享统计键={list(shared_keys)}"
        )
        rejected.append(
            RejectedTask(
                task.task_id,
                code,
                conflict_type,
                reason,
                rank,
                compute,
                memory,
            )
        )
        trail.append(f"rank={rank} {task.task_id} reject {code} ({reason})")
        paths.append(f"{task.task_id}:reject:{code}")

    groups = _pack_groups(admitted, config.parallel_slots)
    # 共享只读统计量只覆盖准入任务引用到的 (target_domain, dim)，且只计一次。
    shared_mem, shared_keys = shared_statistics_memory(admitted)
    peak_memory = _schedule_peak_memory(admitted, effective_samples, groups, shared_mem)
    return SchedulePlan(
        plan_id=plan_id,
        admitted=tuple(task.task_id for task in admitted),
        rejected=tuple(rejected),
        groups=tuple(groups),
        estimates=tuple(estimates),
        arbitration_trail=tuple(trail),
        effective_samples=dict(sorted(effective_samples.items())),
        total_compute=used_compute,
        peak_memory=peak_memory,
        shared_stat_memory=shared_mem,
        budget={
            "parallel_slots": config.parallel_slots,
            "compute_budget": config.compute_budget,
            "memory_budget_bytes": config.memory_budget_bytes,
            "max_queued_tasks": max_queued,
            "arbitration_order": list(config.arbitration_order),
        },
        paths=tuple(paths),
    )


def _basis(task: TaskSpec, samples: int, compute: int, memory: int) -> str:
    hyper = ADAPTER_KIND_HYPERPARAMS[task.adapter_kind]
    return (
        f"compute=(1+{hyper['steps']})*{samples}*{task.input_dim}+eval; "
        f"memory=8*float64_buffers({samples}x{task.input_dim})={memory}B; "
        f"computed_compute={compute}"
    )


def _largest_fitting_samples(
    task: TaskSpec,
    config: BudgetConfig,
    used_compute: int,
    shared_mem: int,
    admitted: list[TaskSpec],
    minimum: int,
) -> int | None:
    """二分寻找能同时满足计算与内存约束的最大样本数（确定性）。"""
    remaining = config.compute_budget - used_compute
    low, high = minimum, task.sample_count
    best: int | None = None
    while low <= high:
        mid = (low + high) // 2
        compute_ok = estimate_compute(task, mid) <= remaining
        peak = _peak_memory_with(admitted, task, mid, shared_mem, config)
        memory_ok = peak <= config.memory_budget_bytes
        if compute_ok and memory_ok:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def _peak_memory_with(
    admitted: list[TaskSpec],
    candidate: TaskSpec,
    candidate_samples: int,
    shared_mem: int,
    config: BudgetConfig,
) -> int:
    """假设候选任务进入当前末位并行组后的峰值驻留。"""
    groups = _pack_groups(admitted, config.parallel_slots)
    if not groups:
        groups = [[]]
    last_group = list(groups[-1])
    if len(last_group) >= config.parallel_slots:
        last_group = []
    last_group.append(candidate.task_id)
    samples = {task.task_id: task.sample_count for task in admitted}
    samples[candidate.task_id] = candidate_samples
    by_id = {task.task_id: task for task in admitted}
    by_id[candidate.task_id] = candidate
    peak = shared_mem
    for task_id in last_group:
        peak += estimate_memory(by_id[task_id], samples[task_id])
    return peak


def _pack_groups(admitted: list[TaskSpec], slots: int) -> list[list[str]]:
    """按准入顺序确定性切分并行组，组大小不超过槽位数。"""
    groups: list[list[str]] = []
    current: list[str] = []
    for task in admitted:
        current.append(task.task_id)
        if len(current) == slots:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _schedule_peak_memory(
    admitted: list[TaskSpec],
    effective_samples: dict[str, int],
    groups: list[list[str]],
    shared_mem: int,
) -> int:
    by_id = {task.task_id: task for task in admitted}
    peak = shared_mem
    for group in groups:
        group_total = shared_mem
        for task_id in group:
            group_total += estimate_memory(by_id[task_id], effective_samples[task_id])
        peak = max(peak, group_total)
    return peak


def _conflict_code(conflicts: list[str]) -> tuple[str, str]:
    """冲突优先级：槽位 → 计算 → 内存，返回主业务码与冲突类型串。"""
    if "slot" in conflicts:
        return E_SLOT_BUDGET, ",".join(conflicts)
    if "compute" in conflicts:
        return E_COMPUTE_BUDGET, ",".join(conflicts)
    return E_MEMORY_BUDGET, ",".join(conflicts)
