"""跨模块一致性自检。"""

from .errors import EInconsistent
from .invalidation import ChangeSet
from .ledger import build_ledger
from .transforms import generate_state


def verify_partition(change_set, all_task_ids):
    """重算集与复用集无交集，且并集覆盖全部受影响任务。"""
    if not isinstance(change_set, ChangeSet):
        raise EInconsistent("失效计划类型错误")
    recompute = set(change_set.recompute_task_ids)
    reuse = set(change_set.reuse_task_ids)
    if recompute & reuse:
        raise EInconsistent(
            "重算集与复用集存在交集",
            details={"intersection": sorted(recompute & reuse)},
        )
    union = recompute | reuse
    if union != set(all_task_ids):
        first = next(
            tid for tid in all_task_ids if tid not in union
        ) if any(tid not in union for tid in all_task_ids) else None
        raise EInconsistent(
            "重算集与复用集的并集未覆盖全部任务",
            details={"first_missing_task": first},
        )
    affected = set(change_set.affected_task_ids)
    if not affected.issubset(union):
        raise EInconsistent(
            "受影响任务未全部纳入重算/复用计划",
            details={"first_missing_task": next(iter(affected - union))},
        )
    return True


def independently_recompute_ledger(task, state_dim, config):
    """仅依据记录参数独立复算误差账本，用于自检。"""
    initial = generate_state(task.state_seed, state_dim)
    ledger, _ = build_ledger(task, initial, config)
    return ledger


def verify_ledger_reproducible(task, state_dim, config, recorded_ledger):
    """用记录参数独立复算并与已提交账本逐项比对，定位首个偏差任务。"""
    recomputed = independently_recompute_ledger(task, state_dim, config)
    for stage_a, stage_b in zip(recomputed["entries"], recorded_ledger["entries"]):
        if stage_a["stage"] != stage_b["stage"]:
            raise EInconsistent(
                "账本阶段顺序不一致",
                details={"first_deviation_task": task.task_id},
            )
        for metric in ("max_abs_diff", "max_rel_diff", "amplification"):
            if abs(stage_a[metric] - stage_b[metric]) > 0.0:
                raise EInconsistent(
                    "误差账本无法由记录参数独立复算",
                    details={
                        "first_deviation_task": task.task_id,
                        "stage": stage_a["stage"],
                        "metric": metric,
                    },
                )
    if recomputed["final_checksum"] != recorded_ledger["final_checksum"]:
        raise EInconsistent(
            "复算最终校验和不一致",
            details={"first_deviation_task": task.task_id},
        )
    return True


def run_self_check(change_set, all_task_ids, task_by_id, state_dim,
                   config_by_precision, recorded_ledgers):
    """完整自检入口：分区校验 + 账本独立复算。"""
    verify_partition(change_set, all_task_ids)
    for task_id, recorded in recorded_ledgers.items():
        task = task_by_id[task_id]
        config = config_by_precision[recorded["precision"]]
        verify_ledger_reproducible(task, state_dim, config, recorded)
    return {"ok": True, "tasks_checked": sorted(recorded_ledgers.keys())}
