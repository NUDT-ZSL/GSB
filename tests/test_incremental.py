"""业务要求5: 规则增量更新与依赖失效闭包; 未受影响结论逐字不变。"""

import copy

import pytest

from alertcorr import errors as E


def test_invalidation_closure_and_reuse(loaded_engine, patches):
    before = loaded_engine.dump()
    report = loaded_engine.apply_patch(patches[0])
    assert report["from_version"] == 1
    assert report["to_version"] == 2
    assert report["affected_services"] == {"api": 0, "gw": 0, "web": 1}
    assert report["recomputed_clusters"] > 0
    assert report["reused_clusters"] > 0

    after = loaded_engine.dump()
    affected = set(report["affected_services"])
    for cid, old in before["clusters"].items():
        if old["service"] in affected:
            continue
        assert cid in after["clusters"]
        assert after["clusters"][cid] == old
        assert after["clusters"][cid]["sha"] == old["sha"]

    invalidated_items = {e["item"] for e in report["invalidated"]}
    assert "service:gw" in invalidated_items
    layers = {e["item"]: e["layer"] for e in report["invalidated"]}
    assert layers["service:gw"] == 0
    assert layers["service:web"] == 1
    sources = {e["source"] for e in report["invalidated"]}
    assert any("set_dependencies:gw" in s for s in sources)
    assert any("set_weight:api-1" in s for s in sources)


def test_unaffected_candidates_byte_identical(loaded_engine, patches):
    before = loaded_engine.dump()
    report = loaded_engine.apply_patch(patches[0])
    after = loaded_engine.dump()
    recomputed = {e["item"].split(":", 1)[1]
                  for e in report["invalidated"]
                  if e["item"].startswith("candidates:")}
    reused = 0
    for cid, old_list in before["candidates"].items():
        if cid in recomputed:
            continue
        assert after["candidates"].get(cid) == old_list
        reused += 1
    assert reused == report["reused_candidates"]


def test_patch_idempotent(loaded_engine, patches):
    report1 = loaded_engine.apply_patch(patches[0])
    dump1 = loaded_engine.dump()
    report2 = loaded_engine.apply_patch(patches[0])
    dump2 = loaded_engine.dump()
    assert report2["idempotent_replay"] is True
    assert report2["invalidated"] == report1["invalidated"]
    assert dump1["sha"] == dump2["sha"]
    assert dump2["topology_version"] == 2


def test_patch_unknown_service(loaded_engine):
    patch = {"patch_id": "bad1", "base_version": 1,
             "changes": [{"type": "set_dependencies", "service": "ghost",
                          "upstream": []}]}
    with pytest.raises(E.BusinessError) as exc_info:
        loaded_engine.apply_patch(patch)
    assert exc_info.value.code == E.E3001_PATCH_UNKNOWN_SERVICE


def test_patch_invalid_window(loaded_engine):
    patch = {"patch_id": "bad2", "base_version": 1,
             "changes": [{"type": "add_window",
                          "window": {"id": "w", "kind": "silence",
                                     "service": "db", "instance_id": "",
                                     "start_ms": 100, "end_ms": 50}}]}
    with pytest.raises(E.BusinessError) as exc_info:
        loaded_engine.apply_patch(patch)
    assert exc_info.value.code == E.E3002_INVALID_WINDOW_RANGE


def test_patch_version_rollback(loaded_engine, patches):
    loaded_engine.apply_patch(patches[0])
    stale = copy.deepcopy(patches[0])
    stale["patch_id"] = "stale"
    stale["base_version"] = 1
    with pytest.raises(E.BusinessError) as exc_info:
        loaded_engine.apply_patch(stale)
    assert exc_info.value.code == E.E3003_TOPOLOGY_VERSION_ROLLBACK


def test_patch_cycle_rejected(loaded_engine):
    patch = {"patch_id": "bad3", "base_version": 1,
             "changes": [{"type": "set_dependencies", "service": "db",
                          "upstream": ["api"]}]}
    with pytest.raises(E.BusinessError) as exc_info:
        loaded_engine.apply_patch(patch)
    assert exc_info.value.code == E.E1005_DEPENDENCY_CYCLE
    assert loaded_engine.dump()["topology_version"] == 1
