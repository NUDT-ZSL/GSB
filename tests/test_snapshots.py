"""Requirement 8 (part): versioned snapshot replacement."""

import json

import pytest

from meeting_align.errors import (SnapshotCorruptError,
                                  SnapshotRollbackError)
from meeting_align.snapshots import SnapshotStore


def test_upsert_replaces_whole_snapshot_and_bumps_version(tmp_path):
    store = SnapshotStore(tmp_path)
    snap1, changed = store.upsert("align_config", {"frame_hop": 160})
    assert changed and snap1["version"] == 1
    snap2, changed = store.upsert("align_config", {"frame_hop": 160})
    assert not changed and snap2["version"] == 1  # identical content: no-op
    snap3, changed = store.upsert("align_config", {"frame_hop": 320})
    assert changed and snap3["version"] == 2
    # whole-file replacement: the on-disk document holds only the new payload
    on_disk = json.loads((tmp_path / "align_config.snapshot.json").read_text())
    assert on_disk["payload"] == {"frame_hop": 320}
    assert on_disk["version"] == 2


def test_rollback_rejected(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put("seam_set", {"v": 1}, version=2)
    with pytest.raises(SnapshotRollbackError) as exc:
        store.put("seam_set", {"v": 0}, version=1)
    assert exc.value.code == "E_SNAPSHOT_ROLLBACK"
    with pytest.raises(SnapshotRollbackError):
        store.put("seam_set", {"v": 1}, version=2)  # equal version is rollback


def test_corrupt_snapshot_rejected(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put("digests", {"a": 1}, version=1)
    path = tmp_path / "digests.snapshot.json"
    data = json.loads(path.read_text())
    data["payload"]["a"] = 999  # tamper without fixing the checksum
    path.write_text(json.dumps(data))
    with pytest.raises(SnapshotCorruptError) as exc:
        store.load("digests")
    assert exc.value.code == "E_SNAPSHOT_CORRUPT"


def test_unparseable_snapshot_rejected(tmp_path):
    store = SnapshotStore(tmp_path)
    (tmp_path / "broken.snapshot.json").write_text("{not json")
    with pytest.raises(SnapshotCorruptError):
        store.load("broken")


def test_snapshots_survive_reload(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put("align_config", {"frame_hop": 160}, version=1)
    reloaded = SnapshotStore(tmp_path)
    snap = reloaded.load("align_config")
    assert snap["version"] == 1
    assert snap["payload"] == {"frame_hop": 160}
