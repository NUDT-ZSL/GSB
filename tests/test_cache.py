"""Requirement 5: content-addressed cache and key construction."""

import pytest

from meeting_align.cache import ArtifactCache
from meeting_align.errors import ResourceLimitError


def test_same_key_reuses_snapshot(tmp_path):
    cache = ArtifactCache(tmp_path)
    components = {"pcm_hash": "abc", "frame_hop": 160, "frame_len": 400}
    key = cache.put(components, {"value": 1})
    assert cache.get(key) == {"value": 1}
    # identical components -> identical key
    assert cache.make_key(dict(reversed(list(components.items())))) == key


def test_different_components_force_new_key(tmp_path):
    cache = ArtifactCache(tmp_path)
    k1 = cache.put({"pcm_hash": "a", "frame_hop": 160}, {"v": 1})
    k2 = cache.make_key({"pcm_hash": "a", "frame_hop": 320})
    k3 = cache.make_key({"pcm_hash": "b", "frame_hop": 160})
    assert k1 != k2 and k1 != k3 and k2 != k3
    assert cache.get(k2) is None  # not computed yet


def test_describe_returns_key_composition_and_content_hash(tmp_path):
    cache = ArtifactCache(tmp_path)
    components = {"ns": "seams", "pcm_hash": "xyz", "seam_threshold": 0.02}
    key = cache.put(components, {"candidates": []})
    desc = cache.describe(key)
    assert desc["key_components"] == components
    assert len(desc["content_hash"]) == 64


def test_entry_limit_raises_resource_limit(tmp_path):
    cache = ArtifactCache(tmp_path, max_entries=2)
    cache.put({"i": 0}, {"v": 0})
    cache.put({"i": 1}, {"v": 1})
    with pytest.raises(ResourceLimitError) as exc:
        cache.put({"i": 2}, {"v": 2})
    assert exc.value.code == "E_RESOURCE_LIMIT"
    # rewriting an existing key does not count as a new entry
    cache.put({"i": 0}, {"v": 9})
    assert cache.get(cache.make_key({"i": 0})) == {"v": 9}
