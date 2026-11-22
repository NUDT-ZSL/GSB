from __future__ import annotations

import threading

import pytest

from flightfusion.errors import BusinessError, ErrorCode
from flightfusion.snapshot import SnapshotStore


def _payload(value: str):
    return {"description": {"gyro": value}, "fusion": {"gain": 0.1}}


def test_whole_replacement_not_merge():
    store = SnapshotStore()
    first = store.commit(_payload("v1"), version=1)
    second = store.commit(_payload("v2"), version=2)
    assert first.status == "created"
    assert second.status == "replaced"
    assert store.current.payload == _payload("v2")
    assert "v1" not in repr(store.current.payload)
    assert [item.version for item in store.history] == [1, 2]


def test_rollback_and_mutation_of_existing_version_are_rejected():
    store = SnapshotStore()
    store.commit(_payload("one"), version=2)
    with pytest.raises(BusinessError) as rollback:
        store.commit(_payload("older"), version=1)
    assert rollback.value.code == ErrorCode.SNAPSHOT_ROLLBACK
    with pytest.raises(BusinessError) as mutation:
        store.commit(_payload("changed"), version=2)
    assert mutation.value.code == ErrorCode.SNAPSHOT_ROLLBACK
    unchanged = store.commit(_payload("one"), version=2)
    assert unchanged.status == "unchanged"


def test_checksum_mismatch_is_corrupt():
    store = SnapshotStore()
    with pytest.raises(BusinessError) as caught:
        store.commit(_payload("one"), version=1, provided_checksum="bad")
    assert caught.value.code == ErrorCode.SNAPSHOT_CORRUPT
    assert store.current is None


def test_verify_detects_corrupt_snapshot():
    store = SnapshotStore()
    snapshot = store.commit(_payload("one"), version=1).snapshot.as_dict()
    snapshot["payload"]["description"]["gyro"] = "tampered"
    with pytest.raises(BusinessError) as caught:
        store.verify(snapshot)
    assert caught.value.code == ErrorCode.SNAPSHOT_CORRUPT


def test_busy_commit_fails_immediately():
    store = SnapshotStore()
    with store.hold_commit_lock():
        with pytest.raises(BusinessError) as caught:
            store.commit(_payload("busy"), version=1)
    assert caught.value.code == ErrorCode.STAGE_BUSY


def test_concurrent_commit_one_wins_and_one_is_busy():
    store = SnapshotStore(commit_delay_s=0.15)
    barrier = threading.Barrier(2)
    results = []

    def worker(version: int):
        barrier.wait()
        try:
            result = store.commit(_payload(f"v{version}"), version=version)
            results.append((version, result.status))
        except BusinessError as exc:
            results.append((version, exc.code))

    threads = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    statuses = sorted(status for _version, status in results)
    assert statuses in (
        ["E_STAGE_BUSY", "created"],
        ["E_STAGE_BUSY", "replaced"],
    )

