import json
import os

import pytest

from pano_workbench.errors import E_SNAPSHOT_CORRUPT, PanoError
from pano_workbench.snapshot import Snapshot, SnapshotManager, SnapshotStore


def test_commit_increments_version_and_replaces_wholesale(tmp_path):
    store = SnapshotStore(str(tmp_path))
    s1 = store.commit(b"index-a", b"metrics-a", {"k": 1})
    s2 = store.commit(b"index-b", b"metrics-b", {"k": 2})
    assert (s1.version, s2.version) == (1, 2)
    assert s1.checksum != s2.checksum
    current = store.load_current()
    assert current.version == 2
    assert current.index_bytes == b"index-b"       # whole-sale replacement
    assert current.metrics_bytes == b"metrics-b"
    old = store.load(1)                            # old version stays intact
    assert old.index_bytes == b"index-a"


def test_load_detects_checksum_corruption(tmp_path):
    store = SnapshotStore(str(tmp_path))
    store.commit(b"index-a", b"metrics-a", {"k": 1})
    path = os.path.join(str(tmp_path), "snapshot_v1.json")
    payload = json.loads(open(path).read())
    payload["index_hex"] = "00" * len(payload["index_hex"])  # tamper content
    with open(path, "w") as fh:
        json.dump(payload, fh)
    with pytest.raises(PanoError) as exc:
        store.load_current()
    assert exc.value.code == E_SNAPSHOT_CORRUPT


def test_load_detects_unreadable_file(tmp_path):
    store = SnapshotStore(str(tmp_path))
    store.commit(b"a", b"b", {})
    path = os.path.join(str(tmp_path), "snapshot_v1.json")
    with open(path, "w") as fh:
        fh.write("{not json")
    with pytest.raises(PanoError) as exc:
        store.load_current()
    assert exc.value.code == E_SNAPSHOT_CORRUPT


def test_manager_rejects_non_monotonic_version():
    mgr = SnapshotManager()
    s2 = Snapshot.build(2, b"i2", b"m2", {"version": 2})
    s1 = Snapshot.build(1, b"i1", b"m1", {"version": 1})
    mgr.publish(s1)
    mgr.publish(s2)
    with pytest.raises(PanoError) as exc:
        mgr.publish(s2)                            # same/lower version
    assert exc.value.code == E_SNAPSHOT_CORRUPT
    with pytest.raises(PanoError):
        mgr.publish(s1)


def test_atomic_swap_readers_never_see_mixed_state():
    mgr = SnapshotManager()
    s1 = Snapshot.build(1, b"index-1", b"metrics-1", {"version": 1})
    mgr.publish(s1)
    held = mgr.current()                           # reader pins old snapshot
    s2 = Snapshot.build(2, b"index-2", b"metrics-2", {"version": 2})
    mgr.publish(s2)
    # the pinned reference is still a fully consistent old snapshot
    assert held.version == 1
    assert held.index_bytes == b"index-1"
    assert held.metrics_bytes == b"metrics-1"
    held.verify()
    # new readers see the fully consistent new snapshot
    cur = mgr.current()
    assert cur.version == 2
    assert cur.index_bytes == b"index-2"
    cur.verify()


def test_publish_verifies_checksum():
    mgr = SnapshotManager()
    bad = Snapshot(version=1, checksum="0" * 64,
                   index_bytes=b"i", metrics_bytes=b"m", meta={})
    with pytest.raises(PanoError) as exc:
        mgr.publish(bad)
    assert exc.value.code == E_SNAPSHOT_CORRUPT
