"""切换与回切的裁决执行链。

切换（failover）：
- 按依赖拓扑序逐服务裁决；上游未就绪时下游等待并记录触发来源；
- 每个动作按 seq 顺序执行，提交水位持久化；单服务异常不中断其他服务。
回切（failback）：
- 按流量比例阶梯逐级推进；每步校验目标健康结论与持久化状态；
- 中途失败停留在上一稳定阶梯并输出失败证据，不跳级、不回退到未验证状态。
"""

from .errors import BizError, ErrCode

PHASE_READY = "ready"
PHASE_FAILING_OVER = "failing_over"
PHASE_FAILED_OVER = "failed_over"
PHASE_FAILING_BACK = "failing_back"
PHASE_FAILED_BACK = "failed_back"
PHASE_FAILED = "failed"
PHASE_WAITING = "waiting_upstream"


class Adjudicator:
    def __init__(self, store, clock, executor, health=None):
        self.store = store
        self.clock = clock
        self.executor = executor
        self.health = health

    # ---------------- 切换 ----------------
    def failover(self, plan, services=None):
        """按拓扑序执行切换；返回 {service: conclusion_dict}。"""
        order = plan.topo_order()
        if services is not None:
            wanted = set(services)
            order = [n for n in order if n in wanted]
        results = {}
        for name in order:
            svc = plan.services[name]
            try:
                results[name] = self._failover_one(plan, svc)
            except BizError as exc:
                # 异常链路隔离：记录失败证据，继续其他服务
                results[name] = {
                    "service": name, "kind": "failover", "status": "failed",
                    "error": exc.to_dict(),
                }
                self._save_conclusion(name, "failover", results[name])
        return results

    def _failover_one(self, plan, svc):
        clock_ms = self.clock.now()
        if svc.tombstoned:
            raise BizError(ErrCode.E_TOMBSTONED, "服务已打失效墓碑，拒绝切换",
                           service=svc.name)
        # 上游就绪检查：未就绪则等待并记录触发来源
        not_ready = []
        for up in sorted(svc.upstreams):
            if up not in plan.services:
                continue
            state = self.store.get_service_state(up)
            if state is None or state["phase"] != PHASE_FAILED_OVER:
                not_ready.append(up)
        if not_ready:
            conclusion = {
                "service": svc.name, "kind": "failover", "status": "waiting",
                "triggered_by": not_ready,
                "error": {"code": ErrCode.E_UPSTREAM_NOT_READY,
                          "message": "上游未就绪，下游等待",
                          "details": {"upstreams": not_ready}},
            }
            with self.store.transaction() as conn:
                self.store.upsert_service_state(
                    conn, svc.name, PHASE_WAITING,
                    self._waterline(svc.name), -1, clock_ms)
                self.store.save_conclusion(conn, svc.name, "failover", conclusion)
                self.store.set_clock_waterline(conn, clock_ms)
            return conclusion

        executed, reused = [], []
        for action in svc.actions:
            state = self.store.get_service_state(svc.name)
            waterline = -1 if state is None else state["action_waterline"]
            if action.seq <= waterline:
                reused.append(action.action_id)  # 水位之前：已提交，安全复用
                continue
            self.clock.advance()
            clock_ms = self.clock.now()
            with self.store.transaction() as conn:
                self.store.mark_pending(conn, svc.name, action.action_id, action.seq)
                self.store.upsert_service_state(
                    conn, svc.name, PHASE_FAILING_OVER, waterline, -1, clock_ms)
                self.store.set_clock_waterline(conn, clock_ms)
            # 半提交窗口：pending 已记录但 committed 未写入，崩溃后由恢复路径识别
            result = self.executor.execute(svc.name, action, clock_ms)
            with self.store.transaction() as conn:
                self.store.commit_action(
                    conn, svc.name, action.action_id, svc.source_version,
                    action.seq, clock_ms, result["status"])
                self.store.upsert_service_state(
                    conn, svc.name, PHASE_FAILING_OVER, action.seq, -1, clock_ms)
                self.store.set_clock_waterline(conn, clock_ms)
            executed.append(action.action_id)

        clock_ms = self.clock.now()
        conclusion = {
            "service": svc.name, "kind": "failover", "status": "done",
            "source_version": svc.source_version,
            "executed": executed, "reused": reused,
            "evidence": {"clock_ms": clock_ms,
                         "waterline": (svc.actions[-1].seq if svc.actions else -1)},
        }
        with self.store.transaction() as conn:
            self.store.upsert_service_state(
                conn, svc.name, PHASE_FAILED_OVER,
                svc.actions[-1].seq if svc.actions else -1, -1, clock_ms)
            self.store.save_conclusion(conn, svc.name, "failover", conclusion)
            self.store.set_clock_waterline(conn, clock_ms)
        return conclusion

    # ---------------- 回切 ----------------
    def failback(self, plan, services=None):
        """按流量阶梯回切；失败停留在上一稳定阶梯并输出证据。"""
        order = plan.topo_order()
        if services is not None:
            wanted = set(services)
            order = [n for n in order if n in wanted]
        results = {}
        for name in order:
            svc = plan.services[name]
            try:
                results[name] = self._failback_one(svc)
            except BizError as exc:
                results[name] = {
                    "service": name, "kind": "failback", "status": "failed",
                    "error": exc.to_dict(),
                }
                self._save_conclusion(name, "failback", results[name])
        return results

    def _failback_one(self, svc):
        if svc.tombstoned:
            raise BizError(ErrCode.E_TOMBSTONED, "服务已打失效墓碑，拒绝回切",
                           service=svc.name)
        state = self.store.get_service_state(svc.name)
        # 允许从“已切换”或“回切中途（停在上次稳定阶梯）”状态进入/续推
        if state is None or state["phase"] not in (PHASE_FAILED_OVER, PHASE_FAILING_BACK):
            raise BizError(ErrCode.E_BAD_CONFIG, "服务未处于已切换状态，拒绝回切",
                           service=svc.name,
                           phase=None if state is None else state["phase"])
        current_rung = state["ladder_rung"]
        ladder = svc.ladder
        last_stable = current_rung
        for rung in range(current_rung + 1, len(ladder)):
            self.clock.advance()
            clock_ms = self.clock.now()
            # 每步校验目标健康结论与持久化状态
            persisted = self.store.get_service_state(svc.name)
            if persisted is None or persisted["ladder_rung"] != last_stable:
                raise BizError(ErrCode.E_BAD_CONFIG, "持久化状态与裁决状态不一致",
                               service=svc.name)
            if not self.health.is_healthy(svc.name, rung):
                # 停留在上一稳定阶梯，输出失败证据
                conclusion = {
                    "service": svc.name, "kind": "failback", "status": "stalled",
                    "stable_rung": last_stable,
                    "stable_percent": ladder[last_stable] if last_stable >= 0 else 0,
                    "failed_rung": rung,
                    "failed_percent": ladder[rung],
                    "error": {"code": "E_HEALTH_CHECK",
                              "message": "目标服务健康结论未通过",
                              "details": {"rung": rung, "percent": ladder[rung]}},
                }
                self._save_conclusion(svc.name, "failback", conclusion)
                return conclusion
            with self.store.transaction() as conn:
                self.store.upsert_service_state(
                    conn, svc.name, PHASE_FAILING_BACK,
                    persisted["action_waterline"], rung, clock_ms)
                self.store.set_clock_waterline(conn, clock_ms)
            last_stable = rung
        clock_ms = self.clock.now()
        conclusion = {
            "service": svc.name, "kind": "failback", "status": "done",
            "stable_rung": last_stable,
            "stable_percent": ladder[last_stable] if last_stable >= 0 else 0,
            "evidence": {"clock_ms": clock_ms, "ladder": list(ladder)},
        }
        with self.store.transaction() as conn:
            self.store.upsert_service_state(
                conn, svc.name, PHASE_FAILED_BACK,
                state["action_waterline"], last_stable, clock_ms)
            self.store.save_conclusion(conn, svc.name, "failback", conclusion)
            self.store.set_clock_waterline(conn, clock_ms)
        return conclusion

    # ---------------- 内部 ----------------
    def _waterline(self, service):
        state = self.store.get_service_state(service)
        return -1 if state is None else state["action_waterline"]

    def _save_conclusion(self, service, kind, payload):
        with self.store.transaction() as conn:
            self.store.save_conclusion(conn, service, kind, payload)
