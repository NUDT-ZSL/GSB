"""格式版本升级与增量重算：仅重算新增/变化项，推导受影响回滚步骤，并与全量重算逐项核对。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import E_INCR, OpsError
from .graph import dependents_closure, topo_order
from .models import NormalizedTicket, RollbackPlan
from .risk import ItemContribution, RiskReport, compute_risk, item_contribution, item_error_bound


@dataclass(frozen=True)
class TicketDiff:
    added: tuple
    removed: tuple
    changed: tuple

    @property
    def touched(self):
        return tuple(sorted(set(self.added) | set(self.removed) | set(self.changed)))


def diff_tickets(old: NormalizedTicket, new: NormalizedTicket) -> TicketDiff:
    old_map, new_map = old.item_map(), new.item_map()
    added = tuple(sorted(k for k in new_map if k not in old_map))
    removed = tuple(sorted(k for k in old_map if k not in new_map))
    changed = tuple(sorted(
        k for k in old_map.keys() & new_map.keys()
        if old_map[k].fingerprint() != new_map[k].fingerprint()
    ))
    return TicketDiff(added=added, removed=removed, changed=changed)


class ContributionCache:
    """按变更项内容指纹缓存风险贡献；指纹失配即失效重算。"""

    def __init__(self):
        self._entries = {}  # key -> ItemContribution

    def get(self, item) -> ItemContribution | None:
        entry = self._entries.get(item.key)
        if entry is not None and entry.fingerprint == item.fingerprint():
            return entry
        return None

    def put(self, item) -> ItemContribution:
        entry = ItemContribution(
            key=item.key,
            contribution=item_contribution(item),
            error_bound=item_error_bound(item),
            fingerprint=item.fingerprint(),
        )
        self._entries[item.key] = entry
        return entry

    def drop(self, keys):
        for key in keys:
            self._entries.pop(key, None)

    def clear(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        return count

    def __len__(self):
        return len(self._entries)


@dataclass(frozen=True)
class IncrementalResult:
    report: RiskReport
    diff: TicketDiff
    recomputed_keys: tuple      # 实际重算贡献的变更项
    reused_keys: tuple          # 复用缓存的变更项（贡献逐位保持不变）
    affected_steps: tuple       # 受影响回滚步骤（含传递依赖者）
    new_order: tuple            # 重新生成的回滚顺序
    cache_entries_cleared: int


def incremental_recompute(old_ticket: NormalizedTicket,
                          new_ticket: NormalizedTicket,
                          plan: RollbackPlan,
                          cache: ContributionCache) -> IncrementalResult:
    """增量重算风险贡献与回滚顺序，并与全量重算逐项核对（不一致抛 E_INCR）。"""
    diff = diff_tickets(old_ticket, new_ticket)

    recomputed, reused = [], []
    contributions = []
    for item in new_ticket.items:  # 已按 key 排序
        cached = cache.get(item)
        if cached is not None and item.key not in diff.touched:
            contributions.append(cached)
            reused.append(item.key)
        else:
            contributions.append(cache.put(item))
            recomputed.append(item.key)
    cache.drop(diff.removed)

    report = RiskReport(
        ticket_id=new_ticket.ticket_id,
        revision=new_ticket.revision,
        contributions=tuple(contributions),
        total_score=sum(c.contribution for c in contributions),
        total_error_bound=sum(c.error_bound for c in contributions),
    )

    # 受影响步骤：变更键对应步骤 + 传递依赖者
    step_by_key = {}
    for step in plan.steps:
        step_by_key.setdefault(step.change_key, []).append(step.step_id)
    direct = sorted({sid for key in diff.touched for sid in step_by_key.get(key, ())})
    affected = tuple(dependents_closure(plan, direct)) if direct else ()
    new_order = tuple(topo_order(plan))

    # 与全量重算逐项核对：顺序一致 + 每项贡献一致
    full_report = compute_risk(new_ticket)
    full_map = full_report.contribution_map()
    for contrib in report.contributions:
        full = full_map[contrib.key]
        if (contrib.contribution, contrib.error_bound) != (full.contribution, full.error_bound):
            raise OpsError(E_INCR, "incremental contribution diverges from full recompute",
                           {"first_diff_key": contrib.key,
                            "incremental": contrib.contribution,
                            "full": full.contribution})
    if report.total_score != full_report.total_score:
        raise OpsError(E_INCR, "incremental total diverges from full recompute",
                       {"first_diff_key": "<total>",
                        "incremental": report.total_score,
                        "full": full_report.total_score})

    return IncrementalResult(
        report=report,
        diff=diff,
        recomputed_keys=tuple(recomputed),
        reused_keys=tuple(reused),
        affected_steps=affected,
        new_order=new_order,
        cache_entries_cleared=len(diff.removed),
    )
