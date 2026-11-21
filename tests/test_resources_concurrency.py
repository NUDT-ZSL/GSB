from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import multiprocessing
from pathlib import Path

import pytest

from driftmon.cache import CacheStore
from driftmon.config import default_config
from driftmon.engine import MonitorEngine
from driftmon.errors import E_RESOURCE_LIMIT, AppError
from driftmon.models import ResourceLimits


def _process_cache_worker(arguments):
    root, key_components, sequence = arguments
    cache = CacheStore(root, max_entries=20, resource_policy="strict")
    result = cache.get_or_compute(
        key_components,
        "process_test",
        lambda: {
            "sequence": sequence,
            "payload": ["x" * 1000, {"nested": list(range(50))}],
        },
    )
    return {
        "write_owner": result.write_owner,
        "status": result.status,
        "content": result.content,
        "content_sha256": result.content_sha256,
    }


def _process_distinct_key_worker(arguments):
    root, sequence = arguments
    cache = CacheStore(root, max_entries=3, resource_policy="strict")
    try:
        cache.get_or_compute(
            {"distinct": sequence},
            "process_limit_test",
            lambda: {"sequence": sequence},
        )
        return "written"
    except AppError as error:
        return error.code


def test_batch_size_resource_limit() -> None:
    with pytest.raises(AppError) as captured:
        replace(
            default_config(),
            limits=ResourceLimits(max_batch_samples=32),
        )
    assert captured.value.code == E_RESOURCE_LIMIT
    assert captured.value.field == "max_batch_samples"


def test_cache_entry_limit_strict_and_degrade(tmp_path: Path) -> None:
    strict = CacheStore(tmp_path / "strict", max_entries=1, resource_policy="strict")

    def write(cache: CacheStore, key: str, value: int):
        return cache.get_or_compute(
            {"key": key}, "test", lambda: {"value": value}
        )

    first = write(strict, "a", 1)
    assert first.status == "miss" and first.write_owner is True
    with pytest.raises(AppError) as captured:
        write(strict, "b", 2)
    assert captured.value.code == E_RESOURCE_LIMIT
    assert captured.value.field == "max_cache_entries"

    degraded = CacheStore(
        tmp_path / "degraded", max_entries=1, resource_policy="degrade"
    )
    write(degraded, "a", 1)
    bypass = write(degraded, "b", 2)
    assert bypass.status == "bypass"
    assert bypass.degradation == "CACHE_ENTRY_LIMIT"
    assert degraded.count_entries() == 1


def test_stats_memory_limit_returns_resource_error(runtime) -> None:
    config, cache, alerts = runtime
    constrained = config.updated(
        limits=ResourceLimits(max_stats_memory_bytes=128)
    )
    with pytest.raises(AppError) as captured:
        MonitorEngine(constrained, cache, alerts).run(3)
    assert captured.value.code == E_RESOURCE_LIMIT
    assert captured.value.field == "max_stats_memory_bytes"


def test_parallel_feature_limit_strict_and_degrade(runtime) -> None:
    config, cache, alerts = runtime
    strict_config = config.updated(
        limits=ResourceLimits(max_parallel_features=2, resource_policy="strict")
    )
    with pytest.raises(AppError) as captured:
        MonitorEngine(strict_config, cache, alerts).run(
            3, requested_parallel=8
        )
    assert captured.value.code == E_RESOURCE_LIMIT
    assert captured.value.field == "max_parallel_features"

    degraded_config = config.updated(
        limits=ResourceLimits(max_parallel_features=2, resource_policy="degrade")
    )
    result = MonitorEngine(degraded_config, cache, alerts).run(
        3, requested_parallel=8
    )
    assert result.parallelism["workers"] == 2
    assert "PARALLELISM_CAPPED" in result.degradations


def test_concurrent_same_key_has_one_writer_and_all_read_complete(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path / "cache", max_entries=20, resource_policy="strict")
    key_components = {"feature": "amount", "window": "b1-b3"}

    def worker(_: int):
        return cache.get_or_compute(
            key_components,
            "test",
            lambda: {"value": 42, "nested": {"values": list(range(20))}},
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(worker, range(24)))

    assert all(result.content == {"value": 42, "nested": {"values": list(range(20))}} for result in results)
    assert sum(1 for result in results if result.write_owner) == 1
    assert cache.count_entries() == 1
    key = cache.key_for(key_components)
    assert cache.get(key) is not None


def test_cross_process_same_key_is_atomic(tmp_path: Path) -> None:
    root = tmp_path / "process-cache"
    key_components = {"feature": "amount", "reference": "b0-b3"}
    context = multiprocessing.get_context("fork")
    with context.Pool(processes=8) as pool:
        results = pool.map(
            _process_cache_worker,
            [(str(root), key_components, index) for index in range(16)],
        )
    assert sum(1 for result in results if result["write_owner"]) == 1
    assert len({result["content_sha256"] for result in results}) == 1
    assert all(len(result["content"]["payload"][0]) == 1000 for result in results)
    cache = CacheStore(root, max_entries=20, resource_policy="strict")
    assert cache.count_entries() == 1
    assert cache.get(cache.key_for(key_components)) is not None


def test_cross_process_entry_limit_is_globally_enforced(tmp_path: Path) -> None:
    root = tmp_path / "limit-cache"
    context = multiprocessing.get_context("fork")
    with context.Pool(processes=6) as pool:
        outcomes = pool.map(
            _process_distinct_key_worker,
            [(str(root), index) for index in range(12)],
        )
    cache = CacheStore(root, max_entries=3, resource_policy="strict")
    assert cache.count_entries() == 3
    assert outcomes.count("written") <= 3
    assert "E_RESOURCE_LIMIT" in outcomes

