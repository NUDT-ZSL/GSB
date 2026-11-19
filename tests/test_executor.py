"""Requirements 4, 5, 8: adjudication chain, error mapping, determinism."""

from __future__ import annotations

import json

import pytest

from conftest import build_plan, make_clock
from recovery_pipeline.errors import BizError, Err
from recovery_pipeline.executor import Orchestrator, topo_order
from recovery_pipeline.models import ServicePlan, UnifiedPlan

PLAN_VERSION = 2


def _closed_port_url() -> str:
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def _setup(store, samples, target_url, **kw):
    plan, _, _ = build_plan(samples)
    store.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)
    orch = Orchestrator(store, make_clock(), target_url, **kw)
    return plan, orch


def test_failover_topo_order_and_chain(store, samples, target):
    plan, orch = _setup(store, samples, target.url)
    chain = orch.run_failover(plan, PLAN_VERSION)
    by_service = {c["service"]: c for c in chain}
    assert set(by_service) == {"svc-alpha", "svc-beta", "svc-gamma",
                               "svc-delta"}
    assert by_service["svc-beta"]["first_trigger"] == "initial_fault"
    assert by_service["svc-gamma"]["first_trigger"] == \
        "upstream_ready:svc-beta"
    decisions = [d["decision"] for d in by_service["svc-delta"]["decisions"]]
    assert decisions == ["FAILOVER_START", "FAILED_OVER"]
    view = orch.persisted_view(PLAN_VERSION)
    assert all(s["stage"] == "failed_over" for s in view["stages"])
    assert len(view["watermarks"]) == 6  # 3 alpha + 1 beta + 1 gamma + 1 delta


def test_failover_upstream_not_ready_waits(store, samples):
    # Closed port -> every dispatch is refused; upstreams FAIL, downstreams WAIT.
    plan, orch = _setup(store, samples, _closed_port_url())
    chain = orch.run_failover(plan, PLAN_VERSION)
    by_service = {c["service"]: c for c in chain}
    alpha_dec = [d["decision"] for d in by_service["svc-alpha"]["decisions"]]
    assert alpha_dec == ["FAILOVER_START", "FAIL"]
    gamma = by_service["svc-gamma"]["decisions"][0]
    assert gamma["decision"] == "WAIT"
    assert gamma["trigger_source"] == "upstream_not_ready:svc-beta"
    delta = by_service["svc-delta"]["decisions"][0]
    assert delta["trigger_source"] == "upstream_not_ready:svc-beta,svc-gamma"


def test_failback_ladder_advances(store, samples, target):
    plan, orch = _setup(store, samples, target.url)
    orch.run_failover(plan, PLAN_VERSION)
    chain = orch.run_failback(plan, PLAN_VERSION)
    by_service = {c["service"]: c for c in chain}
    alpha_dec = [d["decision"] for d in by_service["svc-alpha"]["decisions"]]
    assert alpha_dec == ["FAILOVER_START", "FAILED_OVER", "FAILBACK_START",
                         "ADVANCE", "ADVANCE", "ADVANCE", "ADVANCE",
                         "FAILED_BACK"]
    view = orch.persisted_view(PLAN_VERSION)
    assert all(s["stage"] == "failed_back" for s in view["stages"])
    assert all(s["traffic_rung"] == 4 for s in view["stages"])


def test_failback_holds_on_unhealthy(store, samples, target):
    plan, orch = _setup(store, samples, target.url)
    orch.run_failover(plan, PLAN_VERSION)
    calls = {"n": 0}
    real_health = orch._get_health

    def flaky_health():
        calls["n"] += 1
        return calls["n"] <= 3 and real_health()

    orch._get_health = flaky_health
    chain = orch.run_failback(plan, PLAN_VERSION)
    by_service = {c["service"]: c for c in chain}
    delta_dec = [d["decision"] for d in by_service["svc-delta"]["decisions"]]
    assert "HOLD" in delta_dec
    view = orch.persisted_view(PLAN_VERSION)
    delta = next(s for s in view["stages"] if s["service"] == "svc-delta")
    assert delta["traffic_rung"] == 3  # stopped at last stable rung
    assert delta["stage"] == "failed_over"


def test_error_mapping_conn_refused(store, samples):
    plan, orch = _setup(store, samples, _closed_port_url())
    with pytest.raises(BizError) as ei:
        orch._dispatch_action("svc-alpha", "alpha-stop", "v1:alpha-stop",
                              "stop")
    assert ei.value.code == Err.TARGET_CONN_REFUSED


def test_error_mapping_timeout(store, samples, target):
    plan, orch = _setup(store, samples, target.url, http_timeout=0.3)
    target.httpd.state.timeout_seconds = 5.0
    target.httpd.state.mode = "timeout"
    with pytest.raises(BizError) as ei:
        orch._dispatch_action("svc-alpha", "alpha-stop", "v1:alpha-stop",
                              "stop")
    assert ei.value.code == Err.TARGET_TIMEOUT


def test_error_mapping_version_echo_mismatch(store, samples, target):
    plan, orch = _setup(store, samples, target.url)
    target.httpd.state.mode = "bad_version"
    with pytest.raises(BizError) as ei:
        orch._dispatch_action("svc-alpha", "alpha-stop", "v1:alpha-stop",
                              "stop")
    assert ei.value.code == Err.TARGET_VERSION_ECHO_MISMATCH
    assert ei.value.fields["echoed"].endswith("-STALE")


def test_error_mapping_digest_mismatch(store, samples, target):
    plan, orch = _setup(store, samples, target.url)
    action = {"action_id": "alpha-stop", "op": "stop", "generation": 1}
    assert orch._commit_action(PLAN_VERSION, "svc-alpha", action, 1000) \
        == "committed"
    assert orch._commit_action(PLAN_VERSION, "svc-alpha", action, 1000) \
        == "already_committed"  # idempotent resubmit
    tampered = {"action_id": "alpha-stop", "op": "start", "generation": 9}
    with pytest.raises(BizError) as ei:
        orch._commit_action(PLAN_VERSION, "svc-alpha", tampered, 1000)
    assert ei.value.code == Err.DIGEST_MISMATCH


def test_dependency_cycle_detected():
    plan = UnifiedPlan()
    plan.services["a"] = ServicePlan(service="a", fault_domain="f",
                                     upstream=("b",))
    plan.services["b"] = ServicePlan(service="b", fault_domain="f",
                                     upstream=("a",))
    with pytest.raises(BizError) as ei:
        topo_order(plan)
    assert ei.value.code == Err.DEPENDENCY_CYCLE
    assert set(ei.value.fields["cycle"]) == {"a", "b"}


def test_deterministic_replay_same_clock(store, samples, target, tmp_path):
    from recovery_pipeline.store import Store
    plan, orch = _setup(store, samples, target.url)
    orch.run_failover(plan, PLAN_VERSION)
    orch.run_failback(plan, PLAN_VERSION)
    first = orch.persisted_view(PLAN_VERSION)

    store2 = Store(tmp_path / "second" / "pipeline.sqlite3")
    plan2, orch2 = _setup(store2, samples, target.url)
    orch2.run_failover(plan2, PLAN_VERSION)
    orch2.run_failback(plan2, PLAN_VERSION)
    second = orch2.persisted_view(PLAN_VERSION)

    assert json.dumps(first, sort_keys=True) == json.dumps(second,
                                                           sort_keys=True)


def test_clock_rollback_keeps_state(store, samples, target):
    from recovery_pipeline.clock import LogicalClock
    plan, _, _ = build_plan(samples)
    store.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)
    clock = LogicalClock([10_000, 1_000], rollback_tolerance_ms=100)
    orch = Orchestrator(store, clock, target.url)
    with pytest.raises(BizError) as ei:
        orch.run_failover(plan, PLAN_VERSION)
    assert ei.value.code == Err.CLOCK_ROLLBACK_EXCEEDED
    view = orch.persisted_view(PLAN_VERSION)
    # first service committed before the rollback reading; second never ran
    waiting = [s for s in view["stages"] if s["stage"] == "pending"]
    assert waiting, "plan state must not advance after clock rollback"
    assert not any(s["service"] == "svc-beta" and s["stage"] != "pending"
                   for s in view["stages"])
