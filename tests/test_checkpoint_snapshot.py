from __future__ import annotations

import json

import pytest

from motion_temporal.checkpoint import CheckpointStore
from motion_temporal.codes import E_CHECKPOINT_CORRUPT, E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK
from motion_temporal.errors import BusinessError
from motion_temporal.snapshots import SNAPSHOT_CATEGORIES, SnapshotStore


def test_checkpoint_versions_increase_and_checksum_detects_tamper(tmp_path):
    store = CheckpointStore(tmp_path, "run-a")
    first = store.commit("sampling", {"value": 1}, [0, 1])
    second = store.commit("encoding", {"value": 2}, [0, 1])
    assert first["checkpoint_version"] == 1
    assert second["checkpoint_version"] == 2
    loaded = store.load_prefix()
    assert set(loaded) == {"sampling", "encoding"}
    path = tmp_path / "run-a" / "checkpoint_v000001_sampling.json"
    envelope = json.loads(path.read_text())
    envelope["payload"]["value"] = 999
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(BusinessError) as caught:
        store.load_prefix()
    assert caught.value.code == E_CHECKPOINT_CORRUPT


def test_snapshot_whole_replacement_rollback_and_corruption(tmp_path):
    for category in SNAPSHOT_CATEGORIES:
        store = SnapshotStore(tmp_path / category)
        old = store.put(category, 2, {"parameters": [1, 2, 3]})
        changed = store.put(category, 2, {"parameters": [1, 2, 4]})
        assert old["content_checksum"] != changed["content_checksum"]
        with pytest.raises(BusinessError) as caught:
            store.put(category, 1, {"parameters": [0]})
        assert caught.value.code == E_SNAPSHOT_ROLLBACK
        assert caught.value.details["snapshot_category"] == category

        path = next((tmp_path / category).glob("*.json"))
        path.write_text("{not-json", encoding="utf-8")
        with pytest.raises(BusinessError) as caught:
            store.get(category)
        assert caught.value.code == E_SNAPSHOT_CORRUPT
        assert caught.value.details["snapshot_category"] == category
        assert caught.value.details["snapshot_version"] == 2

