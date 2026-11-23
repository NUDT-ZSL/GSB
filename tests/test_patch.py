import pytest

from recovery_engine.errors import BizError, ErrCode
from recovery_engine.patch import apply_patch, invalidate


def test_closure_leaf_change_affects_only_itself(plan):
    new_plan, info = apply_patch(plan, {"patches": [
        {"op": "set_ladder", "name": "cache", "ladder": [20, 40, 60, 80, 100]}]})
    assert info["affected_services"] == ["cache"]
    assert "db" in info["reusable_services"]
    assert "web" in info["reusable_services"]
    assert new_plan.services["cache"].ladder == [20, 40, 60, 80, 100]
    # 原计划不被修改
    assert plan.services["cache"].ladder == [25, 50, 100]


def test_closure_root_change_propagates_downstream(plan):
    _, info = apply_patch(plan, {"patches": [
        {"op": "set_actions", "name": "db",
         "actions": [{"id": "db-stop", "kind": "stop", "at_ms": 9010},
                     {"id": "db-switch", "kind": "switch", "at_ms": 9020},
                     {"id": "db-verify", "kind": "start", "at_ms": 9030}]}]})
    # db 的下游：api、cache；api 的下游：web
    assert info["affected_services"] == ["api", "cache", "db", "web"]
    assert "db-verify" in info["affected_actions"]
    assert set(info["reusable_services"]) == {"legacy", "oldsvc", "tie"}
    recompute = {(c["service"], c["kind"]) for c in info["recompute_conclusions"]}
    assert ("api", "failover") in recompute and ("web", "failback") in recompute


def test_upstream_change_marks_service_and_dependents(plan):
    _, info = apply_patch(plan, {"patches": [
        {"op": "set_upstreams", "name": "cache", "upstreams": ["db", "legacy"]}]})
    assert info["affected_services"] == ["cache"]


def test_tombstone_patch(plan):
    new_plan, info = apply_patch(plan, {"patches": [
        {"op": "tombstone", "name": "legacy", "value": True}]})
    assert new_plan.services["legacy"].tombstoned is True
    assert info["affected_services"] == ["legacy"]


def test_upsert_and_remove_service(plan):
    new_plan, info = apply_patch(plan, {"patches": [
        {"op": "upsert_service", "service": {
            "name": "search", "effective_ms": 9500, "generation": 3,
            "upstreams": ["api"], "ladder": [50, 100],
            "actions": [{"id": "search-switch", "kind": "switch", "at_ms": 9510}]}}]})
    assert "search" in new_plan.services
    assert "search" in info["affected_services"]
    newer, info2 = apply_patch(new_plan, {"patches": [
        {"op": "remove_service", "name": "search"}]})
    assert "search" not in newer.services


def test_patch_cycle_rejected(plan):
    with pytest.raises(BizError) as exc:
        apply_patch(plan, {"patches": [
            {"op": "set_upstreams", "name": "db", "upstreams": ["web"]}]})
    assert exc.value.code == ErrCode.E_CYCLE


def test_patch_missing_upstream_rejected(plan):
    with pytest.raises(BizError) as exc:
        apply_patch(plan, {"patches": [
            {"op": "set_upstreams", "name": "cache", "upstreams": ["ghost"]}]})
    assert exc.value.code == ErrCode.E_BAD_CONFIG


def test_unaffected_conclusions_byte_identical(engine, plan, store):
    engine.failover(plan)
    engine.failback(plan)
    before = {(c["service"], c["kind"]): c["payload_json"]
              for c in store.all_conclusions()}
    _, info = apply_patch(plan, {"patches": [
        {"op": "set_ladder", "name": "cache", "ladder": [10, 100]}]})
    invalidate(store, info)
    after = {(c["service"], c["kind"]): c["payload_json"]
             for c in store.all_conclusions()}
    # cache 的结论被精确失效
    assert ("cache", "failover") not in after
    assert ("cache", "failback") not in after
    # 未受影响服务的结论逐字保持不变
    for key in before:
        if key[0] != "cache":
            assert after[key] == before[key]
