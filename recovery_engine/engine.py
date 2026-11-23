"""引擎编排层：加载合并、裁决执行、强制终止后的恢复。"""

from .adjudicator import Adjudicator, PHASE_FAILED_OVER
from .clock import LogicalClock
from .errors import BizError, ErrCode
from .merge import merge
from .migrate import migrate_plan
from .v1_loader import load_v1
from .v2_loader import load_v2


class Engine:
    def __init__(self, store, clock, executor, health=None):
        self.store = store
        self.clock = clock
        self.executor = executor
        self.adjudicator = Adjudicator(store, clock, executor, health)

    def build_plan(self, v1_path=None, v2_dir=None, v2_meta=None, clock_ms=None):
        """加载两代配置、合并为统一计划并执行时间迁移。

        校验失败时抛业务错误码，且不写入任何持久化状态
        （加载与校验全部在迁移持久化之前完成）。
        """
        v1_cfg = load_v1(v1_path) if v1_path else None
        v2_cfg = load_v2(v2_dir, v2_meta) if (v2_dir and v2_meta) else None
        plan = merge(v1_cfg, v2_cfg)
        migrate_plan(plan, clock_ms if clock_ms is not None else self.clock.now(),
                     self.store)
        return plan

    def check_clock_against_waterline(self):
        """启动时校验注入时钟相对持久化水位未回拨超容差；违规则状态不变。"""
        waterline = self.store.get_clock_waterline()
        if waterline is None:
            return
        observed = self.clock.now()
        tolerance = self.clock.tolerance_ms
        if observed < waterline - tolerance:
            raise BizError(ErrCode.E_CLOCK_REGRESS,
                           "注入逻辑时钟低于持久化水位且超过容差",
                           waterline=waterline, observed=observed,
                           tolerance_ms=tolerance)

    def failover(self, plan, services=None):
        self.check_clock_against_waterline()
        return self.adjudicator.failover(plan, services)

    def failback(self, plan, services=None):
        self.check_clock_against_waterline()
        return self.adjudicator.failback(plan, services)

    def recover(self, plan):
        """强制终止后的恢复裁决。

        依据持久化状态：
        - pending_actions 中残留的记录为半提交动作，清除并按未提交处理；
        - 每个服务只重放已提交水位（action_waterline）之后的动作；
        - 已提交动作绝不重复执行。
        返回恢复报告。
        """
        self.check_clock_against_waterline()
        half_committed = self.store.pending_actions()
        with self.store.transaction() as conn:
            self.store.clear_pending(conn)
        report = {"half_committed_discarded": [
            {"service": p["service"], "action_id": p["action_id"]}
            for p in half_committed], "resumed": {}}
        for name in plan.topo_order():
            svc = plan.services[name]
            state = self.store.get_service_state(name)
            if state is None:
                continue
            if state["phase"] in ("failing_over", "waiting_upstream"):
                waterline = state["action_waterline"]
                remaining = [a.action_id for a in svc.actions if a.seq > waterline]
                if remaining or state["phase"] == "failing_over":
                    result = self.adjudicator._failover_one(plan, svc)
                    report["resumed"][name] = {
                        "from_waterline": waterline,
                        "replayed": remaining,
                        "status": result["status"],
                    }
        return report
