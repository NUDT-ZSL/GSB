"""资源上限与降级策略。

三类可配置上限：潜帧驻留内存估算（活动+参考两份 float64）、
单次分块数（gh*gw）、并行任务数（超出则降级为顺序分批）。
超限处理：内存超限 -> E_RESOURCE_LIMIT；分块数超限 -> 降级到更小
分块网格并标记 BLOCK_GRID_REDUCED；无法整除缩减 -> E_RESOURCE_LIMIT。
"""

from __future__ import annotations

from .errors import StageError, E_RESOURCE_LIMIT

_STAGE = "resource_guard"

DEGRADE_BLOCK_GRID_REDUCED = "BLOCK_GRID_REDUCED"
DEGRADE_PARALLEL_BATCHED = "PARALLEL_BATCHED"


def estimate_latent_bytes(latent_shape) -> int:
    """潜帧驻留内存估算：活动 + 参考两份 float64。"""
    c, h, w = latent_shape
    return int(c) * int(h) * int(w) * 8 * 2


def _divisors(n: int):
    return [d for d in range(1, n + 1) if n % d == 0]


def _reduced_grid(h: int, w: int, gh: int, gw: int, max_blocks: int):
    """在整除约束下寻找不超过 max_blocks 的最大网格（确定性搜索）。"""
    best = None
    for g2 in sorted(_divisors(h), reverse=True):
        if g2 > gh:
            continue
        for g1 in sorted(_divisors(w), reverse=True):
            if g1 > gw:
                continue
            blocks = g1 * g2
            if blocks <= max_blocks and (best is None or blocks > best[0] * best[1]):
                best = (g2, g1)
    return list(best) if best else None


def apply_resource_limits(task, limits):
    """对单个任务应用资源上限；可能原地降级 task.block_grid。"""
    records = []
    need = estimate_latent_bytes(task.latent_shape)
    if need > limits.max_latent_bytes:
        raise StageError(
            _STAGE, E_RESOURCE_LIMIT, task.task_id,
            f"latent frame estimate {need} bytes exceeds limit "
            f"{limits.max_latent_bytes}",
            {"estimated_bytes": need, "max_latent_bytes": limits.max_latent_bytes})

    gh, gw = task.block_grid
    blocks = gh * gw
    if blocks > limits.max_blocks_per_task:
        c, h, w = task.latent_shape
        new_grid = _reduced_grid(h, w, gh, gw, limits.max_blocks_per_task)
        if new_grid is None:
            raise StageError(
                _STAGE, E_RESOURCE_LIMIT, task.task_id,
                f"cannot reduce block_grid {task.block_grid} to fit "
                f"max_blocks_per_task={limits.max_blocks_per_task}",
                {"block_grid": list(task.block_grid),
                 "max_blocks_per_task": limits.max_blocks_per_task})
        task.original_block_grid = list(task.block_grid)
        task.block_grid = new_grid
        task.degraded = True
        task.degradation_category = DEGRADE_BLOCK_GRID_REDUCED
        records.append({
            "task_id": task.task_id,
            "category": DEGRADE_BLOCK_GRID_REDUCED,
            "original_block_grid": task.original_block_grid,
            "degraded_block_grid": list(new_grid),
            "reason": f"blocks {blocks} > max_blocks_per_task "
                      f"{limits.max_blocks_per_task}",
        })
    return records


def plan_parallel_batches(n_tasks: int, limits) -> dict:
    """并行任务数上限：超出则降级为顺序分批执行。"""
    max_par = max(1, limits.max_parallel_tasks)
    batches = [list(range(i, min(i + max_par, n_tasks)))
               for i in range(0, n_tasks, max_par)]
    return {
        "batch_count": len(batches),
        "batch_size": max_par,
        "degraded": n_tasks > max_par,
        "category": DEGRADE_PARALLEL_BATCHED if n_tasks > max_par else None,
        "batches": batches,
    }
