from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from rope_attention.data import sample_configs
from rope_attention.errors import (
    E_CACHE_CORRUPT,
    E_SNAPSHOT_CONFLICT,
    E_VERSION_STALE,
    RopeError,
)
from rope_attention.process_demo import snapshot_commit_worker
from rope_attention.snapshot import ConfigSnapshot, SnapshotStore


def test_snapshot_commit_is_monotonic_and_idempotent(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    first, changed = store.replace_from_text(
        sample_configs()["v1"],
        source_name="first",
        version=1,
    )
    assert changed
    again, changed_again = store.replace_from_text(
        sample_configs()["v1"],
        source_name="first",
        version=1,
    )
    assert not changed_again
    assert again.checksum == first.checksum
    second, changed_second = store.replace_from_text(
        sample_configs()["v3"],
        source_name="second",
        version=2,
    )
    assert changed_second
    assert store.active() == second
    with pytest.raises(RopeError) as captured:
        store.replace_from_text(
            sample_configs()["v2"],
            source_name="stale",
            version=1,
        )
    assert captured.value.code == E_VERSION_STALE


def test_same_version_different_checksum_is_rejected(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.replace_from_text(sample_configs()["v1"], source_name="first", version=1)
    with pytest.raises(RopeError) as captured:
        store.replace_from_text(
            sample_configs()["v2"],
            source_name="conflict",
            version=1,
        )
    assert captured.value.code == E_SNAPSHOT_CONFLICT


def test_corrupt_snapshot_file_is_detected(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    snapshot, _ = store.replace_from_text(
        sample_configs()["v1"],
        source_name="first",
        version=1,
    )
    path = store._snapshot_path(snapshot.version)
    document = path.read_text(encoding="utf-8")
    path.write_text(document.replace('"version": 1', '"version": 2'), encoding="utf-8")
    with pytest.raises(RopeError) as captured:
        store.active()
    assert captured.value.code == E_CACHE_CORRUPT


def test_concurrent_same_version_commits_have_one_winner(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    arguments = [
        (str(tmp_path), 1, sample_configs()["v1"], "worker-a"),
        (str(tmp_path), 1, sample_configs()["v2"], "worker-b"),
    ]
    with context.Pool(processes=2) as pool:
        results = pool.starmap(snapshot_commit_worker, arguments)
    codes = sorted(item["business_code"] for item in results)
    assert codes == ["E_SNAPSHOT_CONFLICT", "OK"]
    active = SnapshotStore(tmp_path).active()
    assert active is not None
    assert active.version == 1
    assert active.checksum == next(
        item["checksum"] for item in results if item["business_code"] == "OK"
    )

