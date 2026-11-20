from __future__ import annotations

import json
import threading
import time

import pytest

from dialog_emotion.cache import (
    ConfigSnapshotStore,
    ContentAddressedCache,
    make_cache_key,
)
from dialog_emotion.config import PipelineConfig
from dialog_emotion.errors import BusinessError


def test_cache_key_depends_on_identity_not_audit() -> None:
    first, components = make_cache_key(
        "frame_features",
        {"audio": ["a"], "hop": 10},
        {"registration_version": "r1", "config_version": 1},
    )
    second, _ = make_cache_key(
        "frame_features",
        {"audio": ["a"], "hop": 10},
        {"registration_version": "r2", "config_version": 2},
    )
    third, _ = make_cache_key(
        "frame_features",
        {"audio": ["a"], "hop": 15},
        {"registration_version": "r1", "config_version": 1},
    )
    assert first == second
    assert first != third
    assert components["identity"]["stage"] == "frame_features"
    assert components["audit_context"]["registration_version"] == "r1"


def test_cache_roundtrip_version_and_export(tmp_path) -> None:
    cache = ContentAddressedCache(tmp_path / "cache")
    key, components = make_cache_key("x", {"a": 1}, {"registration_version": "r"})
    first = cache.put(
        key,
        {"value": 1},
        node="x:d",
        key_components=components,
    )
    assert first["version"] == 1
    reused = cache.put(
        key,
        {"value": 1},
        node="x:d",
        key_components=components,
    )
    assert reused["version"] == 1
    replaced = cache.put(
        key,
        {"value": 2},
        node="x:d",
        key_components=components,
        replace=True,
    )
    assert replaced["version"] == 2
    exported = cache.export_key_components(key)
    assert exported["identity"]["a"] == 1
    assert cache.list_entries()[0]["version"] == 2


def test_corrupt_cache_entry_returns_code(tmp_path) -> None:
    cache = ContentAddressedCache(tmp_path / "cache")
    key, components = make_cache_key("x", {"a": 1})
    cache.put(key, {"value": 1}, node="x", key_components=components)
    path = cache.path_for(key)
    payload = json.loads(path.read_bytes())
    payload["data"]["value"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BusinessError) as captured:
        cache.get(key)
    assert captured.value.code == "E_CACHE_CORRUPT"


def test_cache_entry_limit(tmp_path) -> None:
    cache = ContentAddressedCache(tmp_path / "cache")
    key1, components = make_cache_key("x", {"a": 1})
    key2, _ = make_cache_key("x", {"a": 2})
    cache.put(key1, {"value": 1}, node="x", key_components=components, max_entries=1)
    with pytest.raises(BusinessError) as captured:
        cache.put(
            key2,
            {"value": 2},
            node="x",
            key_components=components,
            max_entries=1,
        )
    assert captured.value.code == "E_RESOURCE_LIMIT"


def test_concurrent_commit_returns_busy_without_blocking(tmp_path) -> None:
    cache = ContentAddressedCache(tmp_path / "cache")
    key, components = make_cache_key("x", {"a": 1})
    captured: list[BusinessError] = []

    with cache.stage_lock(key):
        def writer() -> None:
            try:
                cache.put(
                    key,
                    {"value": 1},
                    node="x",
                    key_components=components,
                )
            except BusinessError as exc:
                captured.append(exc)

        thread = threading.Thread(target=writer)
        started = time.monotonic()
        thread.start()
        thread.join(timeout=2.0)
        elapsed = time.monotonic() - started
    assert not thread.is_alive()
    assert elapsed < 1.0
    assert captured and captured[0].code == "E_STAGE_BUSY"


def test_config_snapshots_are_whole_monotonic_replacements(tmp_path) -> None:
    store = ConfigSnapshotStore(tmp_path / "config")
    first = store.commit(PipelineConfig())
    same = store.commit(PipelineConfig())
    second = store.commit(PipelineConfig(frame_shift_ms=15.0))
    assert first["version"] == 1
    assert same["version"] == 1
    assert second["version"] == 2
    assert second["replacement"] == "whole-snapshot"
    archived = store.load_version(1)
    assert archived["config"]["frame_shift_ms"] == 10.0
    assert store.load_current()["version"] == 2


def test_corrupt_config_snapshot_code(tmp_path) -> None:
    store = ConfigSnapshotStore(tmp_path / "config")
    store.commit(PipelineConfig())
    payload = json.loads(store.current_path.read_bytes())
    payload["config"]["frame_shift_ms"] = 5.0
    store.current_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BusinessError) as captured:
        store.load_current()
    assert captured.value.code == "E_CACHE_CORRUPT"
