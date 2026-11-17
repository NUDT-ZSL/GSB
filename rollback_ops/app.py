"""应用编排层：加载归一、风险计算、版本演进整体替换、执行与恢复。"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import E_VERSION, OpsError
from .graph import topo_order, validate_plan
from .incremental import ContributionCache, IncrementalResult, incremental_recompute
from .ledger import (RecoveryReport, RollbackExecutor, fetch_entries, recover,
                     verify_ledger)
from .loader import load_plan, load_ticket
from .models import NormalizedTicket, RollbackPlan
from .risk import RiskReport, compute_risk


@dataclass(frozen=True)
class ViewSnapshot:
    """某一变更单版本下的完整结论视图（风险、计划、顺序）。历史快照只读。"""
    ticket: NormalizedTicket
    plan: RollbackPlan
    report: RiskReport
    order: tuple

    def version_label(self):
        return (f"ticket_rev={self.ticket.revision} "
                f"format_v{self.ticket.source_format_version} "
                f"plan_v{self.plan.plan_version}")


@dataclass(frozen=True)
class ReplacementReport:
    """整体替换报告：变化项、受影响步骤、被清空的重算缓存条目数。"""
    old_label: str
    new_label: str
    changed_items: tuple        # added+removed+changed，排序
    added_items: tuple
    removed_items: tuple
    affected_steps: tuple
    cache_entries_cleared: int


class VersionStore:
    """维护当前视图与只读历史；变更单演进时整体替换而非增量合并。"""

    def __init__(self):
        self._history: list[ViewSnapshot] = []
        self._current: ViewSnapshot | None = None
        self.cache = ContributionCache()

    @property
    def current(self) -> ViewSnapshot | None:
        return self._current

    @property
    def history(self) -> tuple:
        return tuple(self._history)

    def version_labels(self) -> list:
        labels = [s.version_label() for s in self._history]
        if self._current is not None:
            labels.append(self._current.version_label() + " (current)")
        return labels

    def load_initial(self, ticket: NormalizedTicket, plan: RollbackPlan) -> ViewSnapshot:
        validate_plan(plan)
        report = compute_risk(ticket)
        for item in ticket.items:
            self.cache.put(item)
        self._current = ViewSnapshot(ticket=ticket, plan=plan, report=report,
                                     order=tuple(topo_order(plan)))
        return self._current

    def replace_ticket(self, new_ticket: NormalizedTicket,
                       new_plan: RollbackPlan | None = None) -> tuple:
        """变更单演进：版本号须单调递增；当前视图整体替换，旧视图转入只读历史。"""
        if self._current is None:
            raise OpsError(E_VERSION, "no current view to replace", {})
        old = self._current
        if new_ticket.revision <= old.ticket.revision:
            raise OpsError(E_VERSION, "ticket revision must increase monotonically",
                           {"current_revision": old.ticket.revision,
                            "attempted_revision": new_ticket.revision})
        plan = new_plan or old.plan
        if plan.plan_version < old.plan.plan_version:
            raise OpsError(E_VERSION, "plan version must not regress",
                           {"current_plan_version": old.plan.plan_version,
                            "attempted_plan_version": plan.plan_version})
        validate_plan(plan)

        incr: IncrementalResult = incremental_recompute(
            old.ticket, new_ticket, plan, self.cache)
        cleared = incr.cache_entries_cleared
        if plan is not old.plan:
            cleared += 0  # 计划替换不影响按内容指纹索引的贡献缓存

        self._history.append(old)  # 旧版本结论保留为只读历史
        self._current = ViewSnapshot(ticket=new_ticket, plan=plan,
                                     report=incr.report, order=incr.new_order)
        replacement = ReplacementReport(
            old_label=old.version_label(),
            new_label=self._current.version_label(),
            changed_items=incr.diff.touched,
            added_items=incr.diff.added,
            removed_items=incr.diff.removed,
            affected_steps=incr.affected_steps,
            cache_entries_cleared=cleared,
        )
        return self._current, replacement, incr


class OpsApp:
    """面向演示与 UI 的门面：串起加载、风险、计划、执行、恢复。"""

    def __init__(self, ledger_conn=None):
        self.store = VersionStore()
        self.conn = ledger_conn

    def load(self, ticket_path, plan_path) -> ViewSnapshot:
        ticket = load_ticket(ticket_path)
        plan = load_plan(plan_path)
        return self.store.load_initial(ticket, plan)

    def evolve(self, ticket_path, plan_path=None) -> tuple:
        ticket = load_ticket(ticket_path)
        plan = load_plan(plan_path) if plan_path else None
        return self.store.replace_ticket(ticket, plan)

    def execute(self, clock=None, crash_hook=None) -> list:
        current = self._require_current()
        executor = RollbackExecutor(self.conn, clock=clock, crash_hook=crash_hook)
        return executor.execute_plan(current.plan, current.ticket, current.report)

    def verify_ledger(self) -> None:
        current = self._require_current()
        verify_ledger(self.conn, current.plan, current.ticket.revision)

    def recover(self) -> RecoveryReport:
        current = self._require_current()
        return recover(self.conn, current.plan, current.ticket)

    def ledger_entries(self) -> list:
        return fetch_entries(self.conn)

    def _require_current(self) -> ViewSnapshot:
        if self.store.current is None:
            raise OpsError(E_VERSION, "no ticket loaded", {})
        return self.store.current
