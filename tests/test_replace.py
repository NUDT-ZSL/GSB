from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from graphstore.errors import (
    E_CLOCK_ROLLBACK,
    E_SHARD_HASH_MISMATCH,
    E_VERSION_ROLLBACK,
    GraphStoreError,
)
from graphstore.model import ClockConfig
from graphstore.package import load_package
from graphstore.storage import GraphStore

from conftest import ROOT


def test_merge_preview_does_not_create_persistent_state(
    tmp_path: Path, clock: ClockConfig, base_package
) -> None:
    db = tmp_path / "preview.sqlite3"
    store = GraphStore(db, clock)
    decision = store.classify_package(base_package)
    assert decision["classification"] == "initial_load"
    assert not db.exists()


def test_noop_append_and_rollback_paths(
    tmp_path: Path, clock: ClockConfig, base_package
) -> None:
    store = GraphStore(tmp_path / "store.sqlite3", clock)
    initial = store.replace(base_package)
    assert initial.classification == "initial_load"

    repeated = store.replace(base_package)
    assert repeated.classification == "no_op"
    assert repeated.idempotent is True
    assert store.current_index_version() == 1

    reformatted_manifest = replace(base_package, manifest_sha256="f" * 64)
    semantic_noop = store.replace(reformatted_manifest)
    assert semantic_noop.classification == "no_op"
    assert semantic_noop.idempotent is True
    assert store.current_index_version() == 1

    append_package = load_package(
        ROOT / "samples/packages/append/manifest.json", clock
    )
    appended = store.replace(append_package)
    assert appended.classification == "incremental_append"
    assert appended.node_count == 10
    assert store.current_index_version() == 2

    with pytest.raises(GraphStoreError) as error:
        store.replace(base_package)
    assert error.value.code == E_VERSION_ROLLBACK
    assert store.current_index_version() == 2

    rollback_package = load_package(
        ROOT / "samples/packages/rollback/manifest.json", clock
    )
    with pytest.raises(GraphStoreError) as error:
        store.replace(rollback_package)
    assert error.value.code == E_VERSION_ROLLBACK
    assert store.current_index_version() == 2

    clock_rollback_package = load_package(
        ROOT / "samples/packages/clock-rollback/manifest.json", clock
    )
    with pytest.raises(GraphStoreError) as error:
        store.replace(clock_rollback_package)
    assert error.value.code == E_CLOCK_ROLLBACK
    assert store.current_index_version() == 2

    verify = store.verify()
    assert verify["current_index"]["node_count"] == 10
    assert verify["current_index"]["edge_count"] == 10


def test_corrupt_and_missing_shards_are_all_or_nothing(
    tmp_path: Path, clock: ClockConfig, base_package
) -> None:
    store = GraphStore(tmp_path / "store.sqlite3", clock)
    store.replace(base_package)
    before = store.status()

    with pytest.raises(GraphStoreError) as error:
        load_package(ROOT / "samples/packages/partial/manifest.json", clock)
    assert error.value.code == E_SHARD_HASH_MISMATCH

    with pytest.raises(GraphStoreError) as error:
        load_package(ROOT / "samples/packages/missing/manifest.json", clock)
    assert error.value.code == "GS_MISSING_FILE"

    after = store.status()
    assert after["current_index"] == before["current_index"]
    assert after["pending_builds"] == []


def test_explicit_downgrade_policy(
    tmp_path: Path, clock: ClockConfig, base_package
) -> None:
    store = GraphStore(tmp_path / "store.sqlite3", clock)
    store.replace(base_package)
    rollback = load_package(ROOT / "samples/packages/rollback/manifest.json", clock)
    outcome = store.replace(rollback, downgrade_policy="allow")
    assert outcome.classification == "full_replace"
    assert outcome.decision_reason == "explicit_downgrade_version_rollback_accepted"
    assert store.status()["current_index"]["package_version"] == 1


def test_explicit_policy_can_restore_historical_committed_versions(
    tmp_path: Path, clock: ClockConfig, base_package
) -> None:
    store = GraphStore(tmp_path / "store.sqlite3", clock)
    store.replace(base_package)
    append_package = load_package(
        ROOT / "samples/packages/append/manifest.json", clock
    )
    store.replace(append_package)
    assert store.current_index_version() == 2

    restored = store.replace(base_package, downgrade_policy="allow")
    assert restored.classification == "downgrade_restore"
    assert restored.index_version == 1
    assert store.current_index_version() == 1
    assert store.status()["current_index"]["node_count"] == 9

    forward = store.replace(append_package, downgrade_policy="allow")
    assert forward.classification == "downgrade_restore"
    assert forward.index_version == 2
    assert store.current_index_version() == 2
    assert store.status()["current_index"]["node_count"] == 10

