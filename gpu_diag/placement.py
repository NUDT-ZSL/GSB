"""带拓扑约束的连续卡位放置。

放置规则完全确定，不依赖字典/哈希遍历顺序：

* 卡位按索引升序扫描，先找到的可行窗口胜出；
* ``same_group``   : 窗口内全部卡位属于同一拓扑分组；
* ``distinct_group``: 窗口内每个卡位属于不同拓扑分组；
* ``any``          : 任意连续空闲窗口；
* 已放置与已下线 (drained) 卡位均视为不可用；
* 单卡估算显存超过池单卡容量时直接登记 E_PLACEMENT。
"""

from .errors import E_PLACEMENT, code_name
from .models import RejectReason


def _window_labels(pool, window):
    labels = pool.card_labels
    return [labels[index] for index in window]


def _satisfies(constraint, labels):
    if constraint == "any":
        return True
    if constraint == "same_group":
        return len(set(labels)) == 1
    if constraint == "distinct_group":
        return len(set(labels)) == len(labels)
    return False


def try_place(pool, job, estimate, occupied_cards):
    """返回 (slots, topology_group, reason)。成功时 reason 为 None。"""
    unavailable = set(pool.drained_cards) | set(occupied_cards)
    if estimate.per_card_mib > pool.card_memory_mib:
        return [], "", RejectReason(
            code=E_PLACEMENT,
            code_name=code_name(E_PLACEMENT),
            dimension="card_memory_mib",
            message=("per-card estimate {} MiB exceeds pool {} card capacity {} MiB").format(
                estimate.per_card_mib, pool.name, pool.card_memory_mib),
            current_usage=pool.card_memory_mib,
            requested=estimate.per_card_mib,
            limit=pool.card_memory_mib,
            field_path="pools.{}.card_memory_mib".format(pool.name),
        )

    for start in range(0, pool.card_count - job.card_count + 1):
        window = list(range(start, start + job.card_count))
        if any(index in unavailable for index in window):
            continue
        labels = _window_labels(pool, window)
        if _satisfies(job.topology_constraint, labels):
            group = labels[0] if job.topology_constraint == "same_group" else ""
            return window, group, None

    return [], "", RejectReason(
        code=E_PLACEMENT,
        code_name=code_name(E_PLACEMENT),
        dimension="topology",
        message=("no contiguous free window of {} cards satisfies constraint {} in pool {}").format(
            job.card_count, job.topology_constraint, pool.name),
        current_usage=len(unavailable),
        requested=job.card_count,
        limit=pool.card_count,
        field_path="jobs.{}.topology_constraint".format(job.job_id),
    )
