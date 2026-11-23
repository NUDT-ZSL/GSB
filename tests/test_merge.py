from recovery_engine.merge import merge
from recovery_engine.v1_loader import load_v1
from recovery_engine.v2_loader import load_v2
from tests.conftest import V1_PATH, V2_DIR, V2_META


def build_plan():
    return merge(load_v1(V1_PATH), load_v2(V2_DIR, V2_META))


def test_merge_deterministic_byte_identical():
    outputs = {build_plan().to_stable_json() for _ in range(5)}
    assert len(outputs) == 1


def test_conflict_resolution_by_clock():
    plan = build_plan()
    # api: v2 effective_ms=9000 > v1 clock 5000 → v2 胜出
    assert plan.services["api"].source_version == "v2"
    # web: v2 effective_ms=4000 < v1 clock 5000 → v1 胜出
    assert plan.services["web"].source_version == "v1"
    reasons = {r["service"]: r["reason"] for r in plan.rationale}
    assert reasons["api"] == "newer_logical_clock"
    assert reasons["web"] == "newer_logical_clock"


def test_conflict_resolution_by_format_priority_on_tie():
    plan = build_plan()
    # tie: v2 effective_ms=5000 == v1 clock 5000 → 格式优先级 v2 胜出
    assert plan.services["tie"].source_version == "v2"
    reason = {r["service"]: r for r in plan.rationale}["tie"]
    assert reason["reason"] == "format_priority"
    assert reason["discarded"] == "v1"


def test_provenance_preserved():
    plan = build_plan()
    api = plan.services["api"]
    assert all(a.source_version == "v2" for a in api.actions)
    assert api.actions[0].priority == "P1"           # v2 优先级标签保留
    assert api.effective_ms == 9000                  # v2 原始绝对时刻保留
    web = plan.services["web"]
    assert web.fallback_seconds == 30                # v1 原始相对回退值保留
    assert [a.at_tick for a in web.actions] == [10, 20]  # v1 原始时间表示保留
    assert web.actions[0].source_version == "v1"


def test_rationale_stable_sorted():
    plan = build_plan()
    services = [r["service"] for r in plan.to_dict()["rationale"]]
    assert services == sorted(services)
    assert set(services) == {"web", "api", "legacy", "tie", "db", "cache", "oldsvc"}
