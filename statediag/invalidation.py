"""定向依赖失效传播与增量失效闭包。

区分三类情形：仅 revision 提升、仅配置代数提升、block_size 变化。
按内容寻址缓存键决定受影响任务集合，复用未受影响任务的已提交产物，
仅重算受影响后缀，并保证重算集与复用集无交集且并集覆盖全部任务。
"""

from dataclasses import dataclass

from .cache import content_key


@dataclass
class ChangeSet:
    kind: str
    changed_task_ids: tuple
    affected_task_ids: tuple
    recompute_task_ids: tuple
    reuse_task_ids: tuple
    by_key: dict

    def to_dict(self):
        return {
            "kind": self.kind,
            "changed_task_ids": list(self.changed_task_ids),
            "affected_task_ids": list(self.affected_task_ids),
            "recompute_task_ids": list(self.recompute_task_ids),
            "reuse_task_ids": list(self.reuse_task_ids),
            "cache_keys": self.by_key,
        }


def classify_change(old_tasks, new_tasks, old_config, new_config):
    """判定变化类型。"""
    old_by_id = {t.task_id: t for t in old_tasks}
    new_by_id = {t.task_id: t for t in new_tasks}
    revision_only = all(
        new_by_id[i].block_size == old_by_id[i].block_size
        and new_by_id[i].quant_scale == old_by_id[i].quant_scale
        for i in old_by_id if i in new_by_id
    )
    block_changed = any(
        i in old_by_id
        and new_by_id[i].block_size != old_by_id[i].block_size
        for i in new_by_id
    )
    algebra_changed = new_config.algebra != old_config.algebra
    if block_changed:
        return "block_size_change"
    if algebra_changed:
        return "algebra_bump"
    if revision_only:
        return "revision_bump"
    return "revision_bump"


def plan_invalidation(ordered_tasks, old_tasks_by_id, old_config, new_config,
                      committed_keys):
    """计算失效闭包与重算/复用计划。

    ordered_tasks: 新清单按 (task_id, logical_clock_ms) 排序后的序列。
    committed_keys: 已提交内容键集合（来自检查点/缓存）。
    """
    kind = classify_change(
        list(old_tasks_by_id.values()), ordered_tasks, old_config, new_config
    )
    changed = []
    for task in ordered_tasks:
        old = old_tasks_by_id.get(task.task_id)
        if old is None:
            changed.append(task.task_id)
            continue
        if kind == "revision_bump" and task.revision != old.revision:
            changed.append(task.task_id)
        elif kind == "block_size_change" and (
            task.block_size != old.block_size
            or task.revision != old.revision
        ):
            changed.append(task.task_id)
        elif kind == "algebra_bump" and (
            task.revision != old.revision or task != old
        ):
            changed.append(task.task_id)

    changed_set = set(changed)
    # 依赖按摄入顺序构成后缀：首个变化点及其之后全部任务进入失效闭包。
    if changed_set:
        first_idx = min(
            idx for idx, t in enumerate(ordered_tasks)
            if t.task_id in changed_set
        )
    else:
        first_idx = len(ordered_tasks)

    # 失效闭包：
    # - algebra_bump：新代数下全部任务（缓存键随代数改变）。
    # - revision_bump / block_size_change：首个变化点及其之后的受影响后缀。
    if kind == "algebra_bump":
        affected_idx = set(range(len(ordered_tasks)))
    else:
        affected_idx = set(range(first_idx, len(ordered_tasks)))

    by_key = {}
    recompute = []
    reuse = []
    for idx, task in enumerate(ordered_tasks):
        key = content_key(task, new_config)
        by_key[task.task_id] = key
        if idx not in affected_idx:
            # 闭包之外的任务内容与配置均未变，直接复用已提交产物。
            reuse.append(task.task_id)
        elif key in committed_keys:
            # 闭包之内但内容寻址键已存在等价提交，仍可安全复用。
            reuse.append(task.task_id)
        else:
            recompute.append(task.task_id)

    affected = [ordered_tasks[i].task_id for i in sorted(affected_idx)]
    return ChangeSet(
        kind=kind,
        changed_task_ids=tuple(changed),
        affected_task_ids=tuple(affected),
        recompute_task_ids=tuple(recompute),
        reuse_task_ids=tuple(reuse),
        by_key=by_key,
    )
