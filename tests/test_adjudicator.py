from recovery_engine.engine import Engine
from recovery_engine.errors import ErrCode
from recovery_engine.executor import SimulatedExecutor, SimulatedHealth
from tests.conftest import make_clock


def test_failover_topo_order_and_commit(engine, plan, store):
    results = engine.failover(plan)
    assert results["db"]["status"] == "done"
    assert results["api"]["status"] == "done"
    assert results["web"]["status"] == "done"
    # 拓扑序：db 的动作先于 api，api 先于 web
    executed = engine.executor.executed
    order = [a for _, a, _ in executed]
    assert order.index("db-switch") < order.index("api-stop-v2")
    assert order.index("api-switch-v2") < order.index("web-stop")
    # 全部动作提交且水位持久化
    assert len(store.committed_actions()) == 9
    assert store.get_service_state("web")["phase"] == "failed_over"


def test_tombstoned_service_rejected_but_others_proceed(engine, plan):
    results = engine.failover(plan)
    assert results["oldsvc"]["status"] == "failed"
    assert results["oldsvc"]["error"]["code"] == ErrCode.E_TOMBSTONED
    assert results["legacy"]["status"] == "done"
    assert results["tie"]["status"] == "done"


def test_upstream_not_ready_waits_with_trigger(store, plan):
    # 只对 web 裁决：上游 api 未就绪 → 等待并记录触发来源
    engine = Engine(store, make_clock(), SimulatedExecutor(), SimulatedHealth())
    results = engine.failover(plan, services=["web"])
    assert results["web"]["status"] == "waiting"
    assert results["web"]["triggered_by"] == ["api"]
    assert results["web"]["error"]["code"] == ErrCode.E_UPSTREAM_NOT_READY
    assert store.get_service_state("web")["phase"] == "waiting_upstream"


def test_conn_refused_isolated_per_service(store, plan):
    executor = SimulatedExecutor(failures={"db-stop": "conn_refused"})
    engine = Engine(store, make_clock(), executor, SimulatedHealth())
    results = engine.failover(plan)
    assert results["db"]["status"] == "failed"
    assert results["db"]["error"]["code"] == ErrCode.E_CONN_REFUSED
    assert results["db"]["error"]["details"]["action_id"] == "db-stop"
    # 下游等待，独立服务不受中断
    assert results["api"]["status"] == "waiting"
    assert results["legacy"]["status"] == "done"
    assert results["tie"]["status"] == "done"


def test_budget_exceeded_mapped(store, plan):
    executor = SimulatedExecutor(budget_ms=10, action_costs={"legacy-switch": 20})
    engine = Engine(store, make_clock(), executor, SimulatedHealth())
    results = engine.failover(plan)
    assert results["legacy"]["status"] == "failed"
    err = results["legacy"]["error"]
    assert err["code"] == ErrCode.E_BUDGET_EXCEEDED
    assert err["details"]["cost_ms"] == 20 and err["details"]["budget_ms"] == 10
    assert results["db"]["status"] == "done"


def test_failback_ladder_full(engine, plan, store):
    engine.failover(plan)
    results = engine.failback(plan)
    assert results["db"]["status"] == "done"
    assert results["db"]["stable_percent"] == 100
    assert store.get_service_state("db")["phase"] == "failed_back"
    assert store.get_service_state("db")["ladder_rung"] == 3


def test_failback_stalls_at_last_stable_rung(store, plan):
    # db 仅前两级（10%、25%）健康 → 停在 25%，输出失败证据
    engine = Engine(store, make_clock(), SimulatedExecutor(),
                    SimulatedHealth(healthy_rungs={"db": 1}))
    engine.failover(plan)
    results = engine.failback(plan, services=["db"])
    res = results["db"]
    assert res["status"] == "stalled"
    assert res["stable_rung"] == 1 and res["stable_percent"] == 25
    assert res["failed_rung"] == 2 and res["failed_percent"] == 50
    assert res["error"]["code"] == "E_HEALTH_CHECK"
    # 持久化状态停留在已验证阶梯，不回退到未验证状态
    assert store.get_service_state("db")["ladder_rung"] == 1


def test_failback_resumes_without_skipping(store, plan):
    unhealthy = Engine(store, make_clock(), SimulatedExecutor(),
                       SimulatedHealth(healthy_rungs={"db": 1}))
    unhealthy.failover(plan)
    unhealthy.failback(plan, services=["db"])
    # 恢复健康后继续：从第 2 级续推，不跳过阶梯
    healthy = Engine(store, make_clock(start=50000), SimulatedExecutor(),
                     SimulatedHealth())
    results = healthy.failback(plan, services=["db"])
    assert results["db"]["status"] == "done"
    assert store.get_service_state("db")["ladder_rung"] == 3


def test_failback_requires_failed_over_state(store, plan):
    engine = Engine(store, make_clock(), SimulatedExecutor(), SimulatedHealth())
    results = engine.failback(plan, services=["db"])
    assert results["db"]["status"] == "failed"
    assert results["db"]["error"]["code"] == "E_BAD_CONFIG"
