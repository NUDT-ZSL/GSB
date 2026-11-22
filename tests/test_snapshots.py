from __future__ import annotations

import json

import pytest

from panoweave.errors import BusinessError, ErrorCode
from panoweave.snapshots import SnapshotStore


def test_snapshot_reuse_replacement_and_rollback(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    first = store.commit("parameters", {"value": 1, "nested": {"a": 2}}, version=1)
    assert first.reused is False
    path = tmp_path / "snapshots" / "parameters.snapshot.json"
    before = path.read_bytes()
    reuse = store.commit("parameters", {"nested": {"a": 2}, "value": 1}, version=1)
    assert reuse.reused is True
    assert path.read_bytes() == before
    replacement = store.commit(
        "parameters", {"value": 2, "nested": {"a": 3}}, version=2
    )
    assert replacement.replaced is True
    loaded = store.load("parameters")
    assert loaded.version == 2
    assert loaded.payload["value"] == 2
    with pytest.raises(BusinessError) as caught:
        store.commit("parameters", {"value": 1}, version=1)
    assert caught.value.code == ErrorCode.SNAPSHOT_ROLLBACK
    assert caught.value.details["current_version"] == 2


def test_same_version_different_checksum_is_corrupt(tmp_path):
    store = SnapshotStore(tmp_path)
    store.commit("parameters", {"value": 1}, version=1)
    with pytest.raises(BusinessError) as caught:
        store.commit("parameters", {"value": 2}, version=1)
    assert caught.value.code == ErrorCode.SNAPSHOT_CORRUPT
    assert caught.value.details["version"] == 1
    assert "value" in caught.value.details["inconsistent_fields"]


def test_corrupted_payload_reports_version_and_field(tmp_path):
    store = SnapshotStore(tmp_path)
    store.commit("render", {"panorama": "abc", "mask": "def"}, version=4)
    path = tmp_path / "render.snapshot.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["panorama"] = "tampered"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(BusinessError) as caught:
        store.load("render")
    assert caught.value.code == ErrorCode.SNAPSHOT_CORRUPT
    assert caught.value.details["version"] == 4
    assert "panorama" in caught.value.details["inconsistent_fields"]


def test_concurrent_commit_returns_stage_busy_without_waiting(tmp_path):
    store = SnapshotStore(tmp_path)
    store._commit_lock.acquire()
    try:
        with pytest.raises(BusinessError) as caught:
            store.commit("parameters", {"value": 1}, version=1)
    finally:
        store._commit_lock.release()
    assert caught.value.code == ErrorCode.STAGE_BUSY
    assert caught.value.details["wait_policy"] == "non_blocking"

