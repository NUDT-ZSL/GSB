import pytest

from graymesh.errors import BusinessError, ErrorCode
from graymesh.invalidation import apply_patch, compute_invalidation
from graymesh.models import objects_from_list, validate_all

from tests.conftest import bootstrap


def base_objects(f):
    return objects_from_list([
        f["member"]("m1", 9001, 3),
        f["member"]("m2", 9002, 1),
        f["member"]("s1", 9003, 1),
        f["group"]("g1", ["m1", "m2"]),
        f["group"]("g-shadow", ["s1"]),
        f["mirror_policy"]("mp1", "g-shadow", percent=100),
        f["route"]("r1", "g1", gray_percent=100, mirror="mp1"),
        f["virtual_service"]("vs1", ["r1"]),
    ])


def test_member_change_invalidates_pool_and_snapshot_only(object_factory):
    old = base_objects(object_factory)
    new = dict(old)
    new[("upstream_member", "m1")] = object_factory["member"]("m1", 9001, 5)
    validate_all(new)
    event = compute_invalidation(old, new, "p1", 2)

    assert event["invalidated"]["connection_pool"] == ["upstream_member/m1"]
    assert event["invalidated"]["member_snapshot"] == ["upstream_group/g1"]
    assert event["invalidated"]["parse_cache"] == ["upstream_member/m1"]
    assert event["invalidated"]["mirror_sampling"] == []
    # everything else is reused
    assert "upstream_member/m2" in event["reused"]["connection_pool"]
    assert "upstream_group/g-shadow" in event["reused"]["member_snapshot"]
    assert "gray_route/r1" in event["reused"]["mirror_sampling"]
    # propagation path reaches the virtual service
    paths = [p["path"] for p in event["propagation"]]
    assert ["upstream_member/m1", "upstream_group/g1", "gray_route/r1",
            "virtual_service/vs1"] in paths


def test_mirror_policy_change_invalidates_sampling(object_factory):
    old = base_objects(object_factory)
    new = dict(old)
    new[("mirror_policy", "mp1")] = object_factory["mirror_policy"](
        "mp1", "g-shadow", percent=30)
    validate_all(new)
    event = compute_invalidation(old, new, "p2", 2)

    assert event["invalidated"]["mirror_sampling"] == ["gray_route/r1", "mirror_policy/mp1"]
    assert event["invalidated"]["connection_pool"] == []
    assert event["invalidated"]["member_snapshot"] == []
    assert "upstream_member/m1" in event["reused"]["parse_cache"]


def test_group_membership_change_invalidates_snapshot(object_factory):
    old = base_objects(object_factory)
    new = dict(old)
    new[("upstream_member", "m3")] = object_factory["member"]("m3", 9004, 2)
    new[("upstream_group", "g1")] = object_factory["group"]("g1", ["m1", "m2", "m3"])
    validate_all(new)
    event = compute_invalidation(old, new, "p3", 2)

    assert "upstream_member/m3" in event["invalidated"]["connection_pool"]
    assert event["invalidated"]["member_snapshot"] == ["upstream_group/g1"]
    assert "upstream_member/m1" in event["reused"]["connection_pool"]


def test_malformed_patch_rejected(object_factory):
    old = base_objects(object_factory)
    with pytest.raises(BusinessError) as err:
        apply_patch(old, {"ops": []})
    assert err.value.code == ErrorCode.PATCH_FORMAT
    with pytest.raises(BusinessError) as err:
        apply_patch(old, {"patch_id": "x", "ops": [{"op": "frobnicate",
                                                    "kind": "gray_route", "id": "r1"}]})
    assert err.value.code == ErrorCode.PATCH_FORMAT
    with pytest.raises(BusinessError) as err:
        apply_patch(old, {"patch_id": "x", "ops": [
            {"op": "replace", "kind": "gray_route", "id": "r1",
             "object": {"kind": "gray_route", "id": "OTHER",
                        "spec": {"upstream_group_id": "g1", "weight": 1,
                                 "gray_percent": 1, "matches": []}}}]})
    assert err.value.code == ErrorCode.PATCH_FORMAT


def test_unaffected_route_decisions_bit_identical(store, clock, object_factory):
    """A patch touching g1 must not change decisions of routes on other groups."""
    from graymesh.controlplane import ControlPlane
    from graymesh.router import DecisionEngine

    f = object_factory
    objs = [
        f["member"]("m1", 9001, 3), f["member"]("m2", 9002, 1),
        f["member"]("m3", 9003, 1),
        f["group"]("g1", ["m1", "m2"]), f["group"]("g2", ["m3"]),
        f["route"]("r1", "g1", gray_percent=50),
        f["route"]("r2", "g2", gray_percent=50,
                   matches=[{"headers": {"x-api": "extra"}}]),
        f["virtual_service"]("vs1", ["r1", "r2"]),
    ]
    control = ControlPlane(store, clock)
    bootstrap(control, objs)

    request = {"seq": 1, "key": "u-x", "path": "/ok",
               "headers": {"x-api": "extra"}}
    engine1 = DecisionEngine(store.load_snapshot(), clock)
    before = engine1.decide(request)
    assert before["route_id"] == "r2"

    control.submit_patch({"patch_id": "p-touch-g1", "expected_version": 1, "ops": [
        {"op": "replace", "kind": "upstream_member", "id": "m1",
         "object": f["member"]("m1", 9001, 9)}]})

    engine2 = DecisionEngine(store.load_snapshot(), clock)
    after = engine2.decide(request)
    # every decision field except version/timestamps/invalidation is identical
    for field in ("route_id", "route_selected_by", "route_pick", "route_gates",
                  "upstream_group_id", "member_id", "member_pick", "mirror",
                  "gray_bucket", "request_key"):
        assert before[field] == after[field], field
