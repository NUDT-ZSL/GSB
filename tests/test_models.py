import pytest

from graymesh.errors import BusinessError, ErrorCode
from graymesh.models import objects_from_list, validate_all, validate_object


def objects(factory):
    f = factory
    return objects_from_list([
        f["member"]("m1", 9001, 2),
        f["group"]("g1", ["m1"]),
        f["group"]("g-shadow", ["m1"]),
        f["mirror_policy"]("mp1", "g-shadow", percent=50),
        f["route"]("r1", "g1", gray_percent=60, mirror="mp1"),
        f["route"]("r2", "g1", gray_percent=40),
        f["virtual_service"]("vs1", ["r1", "r2"]),
    ])


def test_valid_object_set_loads(object_factory):
    validate_all(objects(object_factory))


def test_missing_reference_rejected(object_factory):
    objs = objects(object_factory)
    objs[("gray_route", "r1")]["spec"]["upstream_group_id"] = "g-missing"
    with pytest.raises(BusinessError) as err:
        validate_all(objs)
    assert err.value.code == ErrorCode.REF_MISSING
    assert "g-missing" in str(err.value.details)


def test_cycle_rejected(object_factory):
    objs = objects(object_factory)
    objs[("upstream_group", "g-loop-a")] = object_factory["group"](
        "g-loop-a", ["m1"], fallback_group_id="g-loop-b")
    objs[("upstream_group", "g-loop-b")] = object_factory["group"](
        "g-loop-b", ["m1"], fallback_group_id="g-loop-a")
    with pytest.raises(BusinessError) as err:
        validate_all(objs)
    assert err.value.code == ErrorCode.CYCLE
    assert "g-loop-a" in str(err.value.details["cycle"])


def test_invalid_weight_rejected(object_factory):
    with pytest.raises(BusinessError) as err:
        validate_object(object_factory["member"]("m-bad", 9001, weight=0))
    assert err.value.code == ErrorCode.FIELD_INVALID
    with pytest.raises(BusinessError):
        validate_object(object_factory["member"]("m-bad2", 9001, weight=1.5))


def test_gray_percent_sum_rejected(object_factory):
    objs = objects(object_factory)
    objs[("gray_route", "r2")]["spec"]["gray_percent"] = 41  # 60 + 41 > 100
    with pytest.raises(BusinessError) as err:
        validate_all(objs)
    assert err.value.code == ErrorCode.FIELD_INVALID


def test_mirror_percent_range(object_factory):
    with pytest.raises(BusinessError):
        validate_object(object_factory["mirror_policy"]("mp-bad", "g-shadow", percent=101))
    with pytest.raises(BusinessError):
        validate_object(object_factory["mirror_policy"]("mp-bad2", "g-shadow", percent=-1))
