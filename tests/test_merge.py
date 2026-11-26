from coord_workbench.merge import merge_topologies
from coord_workbench.models import TopoEntry


def _v1(node, rel, scope="domain-alpha", deps=()):
    return TopoEntry(node=node, scope=scope, depends_on=list(deps), source_version="v1",
                     raw_time={"rel_publish_s": rel}, abs_ms=-1)


def _v2(node, publish_ms, scope="domain-alpha", tombstone=False, deps=()):
    return TopoEntry(node=node, scope=scope, depends_on=list(deps), source_version="v2",
                     raw_time={"publish_ms": publish_ms}, abs_ms=publish_ms, tombstone=tombstone)


def test_v1_conversion_recorded() -> None:
    idx = merge_topologies([_v1("n1", 5)], [], injected_clock_ms=100_000)
    view = idx["views"]["n1"]
    assert view["abs_ms"] == 95_000
    assert view["conversion"] == {"abs_ms": 95_000, "scale": 1000, "rounding": "floor",
                                  "injected_clock_ms": 100_000}
    assert view["raw_time"] == {"rel_publish_s": 5}
    assert view["source_version"] == "v1"


def test_newer_logical_clock_wins() -> None:
    # v1 -> abs 95_000 ; v2 -> 90_000 : v1 is newer and must win
    idx = merge_topologies([_v1("n1", 5)], [_v2("n1", 90_000)], injected_clock_ms=100_000)
    view = idx["views"]["n1"]
    assert view["source_version"] == "v1"
    assert view["decided_by"] == "newer-logical-clock"


def test_clock_tie_broken_by_source_priority() -> None:
    # v1 -> abs 90_000 equals v2 publish 90_000 : v2 wins on source priority
    idx = merge_topologies([_v1("n1", 10)], [_v2("n1", 90_000)], injected_clock_ms=100_000)
    view = idx["views"]["n1"]
    assert view["source_version"] == "v2"
    assert "clock-tie" in view["decided_by"]


def test_tombstone_excluded_from_views() -> None:
    idx = merge_topologies([], [_v2("n1", 1_000, tombstone=True)], injected_clock_ms=100_000)
    assert idx["views"] == {}
    assert idx["tombstones"][0]["node"] == "n1"


def test_views_keep_decision_metadata(samples) -> None:
    views = samples.topo_index["views"]
    for view in views.values():
        assert view["decided_by"]
        assert view["source_version"] in ("v1", "v2")
        assert view["raw_time"]
    # node-d / node-e exist in both snapshots -> arbitration happened
    overlapped = [v for v in views.values() if v["decided_by"] != f"single-source:{v['source_version']}"]
    assert overlapped, "expected at least one cross-version arbitration"
