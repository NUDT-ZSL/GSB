from __future__ import annotations

from pathlib import Path

import pytest

from dialog_emotion.cache import CacheStore, compute_cache_key
from dialog_emotion.config import ConfigSnapshotStore, PipelineConfig
from dialog_emotion.errors import (
    DomainError,
    E_CACHE_BUSY,
    E_CACHE_CORRUPT,
    E_CACHE_KEY_MISMATCH,
    E_RESOURCE_LIMIT,
)
from dialog_emotion.util import atomic_write_json, read_json


def _assert_code(code: str, fn) -> DomainError:
    with pytest.raises(DomainError) as captured:
        fn()
    assert captured.value.code == code
    return captured.value


def test_cache_snapshot_versions_and_key_composition(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path / "cache")
    for index in range(3):
        parts = [{"name": "node", "value": "test"}, {"name": "index", "value": index}]
        key = compute_cache_key(parts)
        entry = cache.put(key, "test", {"index": index}, parts)
        assert entry["key"] == key
        assert entry["key_parts"] == parts
        assert cache.get(key)["payload"]["index"] == index
    snapshot = cache.snapshot()
    assert snapshot["version"] == 3
    assert snapshot["entry_count"] == 3
    assert snapshot["entries"] == sorted(snapshot["entries"], key=lambda item: item["key"])
    assert len(cache.snapshot_summary()["checksum"]) == len("sha256:") + 64
    assert cache.put(
        compute_cache_key([{"name": "node", "value": "test"}, {"name": "index", "value": 0}]),
        "test",
        {"index": 0},
        [{"name": "node", "value": "test"}, {"name": "index", "value": 0}],
    )["version"] == 1


def test_cache_corruption_and_key_mismatch(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path / "cache")
    parts = [{"name": "node", "value": "test"}]
    key = compute_cache_key(parts)
    cache.put(key, "test", {"value": 1}, parts)
    path = cache.entry_path(key)
    document = read_json(path)
    document["payload"]["value"] = 2
    atomic_write_json(path, document)
    corrupt = _assert_code(E_CACHE_CORRUPT, lambda: cache.get(key))
    assert corrupt.details["entry"] == key
    assert corrupt.details["version"] == 1

    cache = CacheStore(tmp_path / "cache2")
    parts = [{"name": "node", "value": "test"}]
    key = compute_cache_key(parts)
    cache.put(key, "test", {"value": 1}, parts)
    path = cache.entry_path(key)
    document = read_json(path)
    document["key_parts"].append({"name": "tampered", "value": True})
    atomic_write_json(path, document)
    mismatch = _assert_code(E_CACHE_KEY_MISMATCH, lambda: cache.get(key))
    assert mismatch.details["entry"] == key


def test_cache_busy_does_not_overwrite(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path / "cache")
    parts = [{"name": "node", "value": "test"}]
    key = compute_cache_key(parts)
    with cache.write_guard(key):
        _assert_code(
            E_CACHE_BUSY,
            lambda: cache.put(key, "test", {"value": 1}, parts),
        )
    assert cache.get(key) is None
    cache.put(key, "test", {"value": 1}, parts)
    assert cache.get(key)["payload"]["value"] == 1


def test_config_snapshot_full_replacement_and_checksum(tmp_path: Path) -> None:
    store = ConfigSnapshotStore(tmp_path / "config")
    first = store.save(PipelineConfig())
    assert first["version"] == 1
    second = store.save(PipelineConfig(frame_shift_ms=10.0))
    assert second["version"] == 2
    assert (store.root / "v00000001.json").exists()
    assert (store.root / "v00000002.json").exists()
    current = read_json(store.current_path)
    assert current["payload"]["frame_shift_ms"] == 10.0
    current["payload"]["frame_ms"] = 30.0
    atomic_write_json(store.current_path, current)
    corrupt = _assert_code(E_CACHE_CORRUPT, store.load_current)
    assert corrupt.details["entry"] == "config_snapshot"
    assert corrupt.details["version"] == 2


def test_cache_read_only_does_not_create_directories(tmp_path: Path) -> None:
    missing = tmp_path / "missing_cache"
    cache = CacheStore(missing, read_only=True)
    assert cache.get("sha256:" + "0" * 64) is None
    assert not missing.exists()


def test_cache_entry_limit_reports_resource_code(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path / "cache", max_entries=1)
    first_parts = [{"name": "node", "value": "test"}, {"name": "index", "value": 1}]
    second_parts = [{"name": "node", "value": "test"}, {"name": "index", "value": 2}]
    cache.put(compute_cache_key(first_parts), "test", {"index": 1}, first_parts)
    error = _assert_code(
        E_RESOURCE_LIMIT,
        lambda: cache.put(compute_cache_key(second_parts), "test", {"index": 2}, second_parts),
    )
    assert error.details["category"] == "cache_entries"
    assert error.details["limit"] == 1
