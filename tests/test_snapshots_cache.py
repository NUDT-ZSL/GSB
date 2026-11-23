from __future__ import annotations

import json

import pytest

from ctr_cvr_joint.cache import CacheKey, ContentAddressedCache, make_cache_key
from ctr_cvr_joint.errors import E_CACHE_CORRUPT, E_SNAPSHOT_ROLLBACK, BusinessError
from ctr_cvr_joint.snapshots import SnapshotRegistry


def test_snapshot_whole_replacement_and_rollback(tmp_path):
    registry = SnapshotRegistry(tmp_path / "snapshots")
    first = registry.commit("statistics", 1, {"value": 1})
    second = registry.commit("statistics", 2, {"value": 2})
    assert registry.load("statistics", 1) == {"value": 1}
    assert registry.load("statistics", 2) == {"value": 2}
    assert second.version == 2
    with pytest.raises(BusinessError) as excinfo:
        registry.commit("statistics", 1, {"value": 0})
    assert excinfo.value.code == E_SNAPSHOT_ROLLBACK
    with pytest.raises(BusinessError) as excinfo:
        registry.commit("statistics", 2, {"value": 3})
    assert excinfo.value.code == E_CACHE_CORRUPT
    assert first.checksum != second.checksum


def test_cache_put_get_and_corruption(tmp_path):
    cache = ContentAddressedCache(tmp_path / "cache")
    key = make_cache_key({"node": "assembly", "sample": "a"})
    envelope = cache.put(
        "assembly",
        key,
        product={"value": 1},
        request_id="req-a",
        node_path=("assembly",),
    )
    assert cache.get("assembly", key) == envelope
    matches = cache.find_by_request("req-a")
    assert len(matches) == 1
    assert matches[0]["key"] == key.key

    path = cache._path("assembly", key.key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["product"]["value"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BusinessError) as excinfo:
        cache.get("assembly", key)
    assert excinfo.value.code == E_CACHE_CORRUPT


def test_cache_different_components_have_different_keys():
    first = make_cache_key({"sample": "a", "version": 1})
    second = make_cache_key({"sample": "a", "version": 2})
    assert first.key != second.key
    assert isinstance(first, CacheKey)

