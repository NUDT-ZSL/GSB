"""K/V 修订与 block_size 变更的增量执行入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from .cache import ContentCache
from .config import RunConfig, Task
from .engine import EngineContext, compute_layer
from .errors import InconsistentPlanError
from .nodes import (
    InvalidationPlan,
    case_node_ids,
    plan_block_size_change,
    plan_kv_change,
)


@dataclass
class InvalidationResult:
    plan: dict
    reused_verified: List[str]
    recomputed: List[str]
    layer_reports: list

    def to_dict(self) -> dict:
        return {
            "plan": self.plan,
            "reused_verified": self.reused_verified,
            "recomputed": self.recomputed,
            "layer_reports": self.layer_reports,
            "partition_ok": True,
        }


def run_kv_change(
    old_task: Task,
    new_task: Task,
    layers: Sequence[int],
    config: RunConfig,
) -> InvalidationResult:
    return _run(
        old_task, new_task, layers, config,
        plan_factory=plan_kv_change,
    )


def run_block_size_change(
    old_task: Task,
    new_task: Task,
    layers: Sequence[int],
    config: RunConfig,
) -> InvalidationResult:
    return _run(
        old_task, new_task, layers, config,
        plan_factory=plan_block_size_change,
    )


def _run(
    old_task: Task,
    new_task: Task,
    layers: Sequence[int],
    config: RunConfig,
    plan_factory,
) -> InvalidationResult:
    config.validate()
    cache = ContentCache(config.cache_dir)
    merged_plan: InvalidationPlan | None = None
    reused_verified: List[str] = []
    recomputed: List[str] = []
    layer_reports = []

    for layer in layers:
        plan = plan_factory(old_task, new_task, layer)
        if merged_plan is None:
            merged_plan = InvalidationPlan(
                change_type=plan.change_type,
                affected=list(plan.affected),
                recompute=list(plan.recompute),
                reused=list(plan.reused),
                reason=plan.reason,
            )
        else:
            merged_plan.affected.extend(plan.affected)
            merged_plan.recompute.extend(plan.recompute)
            merged_plan.reused.extend(plan.reused)

        # 复用节点必须在缓存中真实存在，否则分区声明不成立。
        entries = cache.all_node_entries()
        for node in plan.reused:
            if node not in entries:
                raise InconsistentPlanError(
                    f"声明复用的节点 {node} 在缓存中不存在，复用不成立",
                    case_id=new_task.case_id,
                    layer=layer,
                )
            reused_verified.append(node)

        # 重算后缀：以新任务配置重建该层 DAG，未失效节点自然缓存命中。
        ctx = EngineContext(task=new_task, config=config, cache=cache)
        report = compute_layer(ctx, layer)
        recomputed.extend(
            node for node in ctx.misses if node in set(plan.recompute)
        )
        # 重算集合内若出现缓存命中，说明键未变化，分区同样不成立。
        unexpected_hits = set(ctx.hits) & set(plan.recompute)
        if unexpected_hits:
            raise InconsistentPlanError(
                f"重算节点出现意外缓存命中：{sorted(unexpected_hits)[:5]}",
                case_id=new_task.case_id,
                layer=layer,
            )
        layer_reports.append(report)

    assert merged_plan is not None
    merged_plan.verify()
    if merged_plan.change_type == "block_size":
        _validate_block_size_coverage(old_task, new_task, layers, merged_plan)
    else:
        _validate_full_coverage(new_task, layers, merged_plan)
    return InvalidationResult(
        plan=merged_plan.to_dict(),
        reused_verified=sorted(set(reused_verified)),
        recomputed=sorted(set(recomputed)),
        layer_reports=layer_reports,
    )


def _validate_full_coverage(
    task: Task, layers: Sequence[int], plan: InvalidationPlan
) -> None:
    universe = set(case_node_ids(task, layers=list(layers)))
    union = set(plan.recompute) | set(plan.reused)
    if union != universe:
        missing = universe - union
        extra = union - universe
        raise InconsistentPlanError(
            "重算/复用并集未覆盖该 case 全部节点",
            case_id=task.case_id,
            details={"missing": sorted(missing), "extra": sorted(extra)},
        )


def _validate_block_size_coverage(
    old_task: Task,
    new_task: Task,
    layers: Sequence[int],
    plan: InvalidationPlan,
) -> None:
    # 块节点按“种类后缀”归并（score_b/* 与 stats_b/* 的块数随 block_size
    # 变化），其余节点 ID 在新旧任务间保持稳定。
    def suffixes(task: Task) -> set:
        result = set()
        for node in case_node_ids(task, layers=list(layers)):
            parts = node.split("/")
            if parts[2].endswith("_b"):
                result.add("/".join(parts[:3]))
            else:
                result.add(node)
        return result

    old_suffix = suffixes(old_task)
    new_suffix = suffixes(new_task)
    partition_suffix = set()
    for node in set(plan.recompute) | set(plan.reused):
        parts = node.split("/")
        if parts[2].endswith("_b"):
            partition_suffix.add("/".join(parts[:3]))
        else:
            partition_suffix.add(node)
    if old_suffix != new_suffix:
        raise InconsistentPlanError(
            "block_size 变更前后非块节点集合不一致",
            case_id=new_task.case_id,
            details={
                "missing": sorted(new_suffix - old_suffix),
                "extra": sorted(old_suffix - new_suffix),
            },
        )
    if partition_suffix != new_suffix:
        raise InconsistentPlanError(
            "重算/复用未覆盖全部节点种类后缀",
            case_id=new_task.case_id,
            details={"missing": sorted(new_suffix - partition_suffix)},
        )
