"""Versioning, whole-snapshot replacement, commit locks and corruption."""
from __future__ import annotations

import json

import numpy as np
import pytest

from scene_pipeline.errors import (
    E_SNAPSHOT_CORRUPT,
    E_STAGE_BUSY,
    SnapshotCorruptError,
    StageBusyError,
)
from scene_pipeline.snapshot import SnapshotStore


def test_versions_are_monotonic_and_replaced_whole(tmp_path):
    store = SnapshotStore(str(tmp_path / "store"))
    manifest_a = store.try_commit("scene", "geometry", {"faces": 10})
    manifest_b = store.try_commit("scene", "geometry", {"faces": 8})
    assert manifest_a["version"] == 1
    assert manifest_b["version"] == 2
    assert store.current_version("scene", "geometry") == 2
    loaded = store.load_manifest("scene", "geometry")
    assert loaded["payload"] == {"faces": 8}
    # Older version remains immutable and independently verifiable.
    assert store.load_manifest("scene", "geometry", 1)["payload"] == {"faces": 10}


def test_array_checksum_detects_corruption(tmp_path):
    store = SnapshotStore(str(tmp_path / "store"))
    store.try_commit(
        "scene", "texture", {"ok": True},
        arrays={"texture.npy": np.zeros((4, 4, 3), dtype=np.uint8)},
    )
    version_dir = store.version_dir("scene", "texture", 1)
    with open(f"{version_dir}/texture.npy", "r+b") as handle:
        handle.seek(-4, 2)
        handle.write(b"\xff\xff\xff\xff")
    with pytest.raises(SnapshotCorruptError) as exc_info:
        store.verify("scene", "texture")
    assert exc_info.value.code == E_SNAPSHOT_CORRUPT


def test_manifest_checksum_detects_tampering(tmp_path):
    store = SnapshotStore(str(tmp_path / "store"))
    store.try_commit("scene", "ingest", {"camera_ids": ["cam00"]})
    manifest_path = f"{store.version_dir('scene', 'ingest', 1)}/snapshot.json"
    with open(manifest_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data["payload"]["camera_ids"] = ["tampered"]
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    with pytest.raises(SnapshotCorruptError):
        store.verify("scene", "ingest")


def test_commit_lock_rejects_concurrent_late_writer(tmp_path):
    store = SnapshotStore(str(tmp_path / "store"))
    first = store.lock().acquire()
    with pytest.raises(StageBusyError) as exc_info:
        store.try_commit("scene", "ingest", {"x": 1})
    assert exc_info.value.code == E_STAGE_BUSY
    first.release()
    # After release, commits succeed again.
    assert store.try_commit("scene", "ingest", {"x": 1})["version"] == 1


def test_stage_scene_id_sets(tmp_path):
    store = SnapshotStore(str(tmp_path / "store"))
    for scene_id in ("a", "b"):
        store.try_commit(scene_id, "ingest", {})
    store.try_commit("a", "texture", {})
    assert store.stage_scene_ids("ingest") == ["a", "b"]
    assert store.stage_scene_ids("texture") == ["a"]
