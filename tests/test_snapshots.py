from __future__ import annotations

import json

import pytest

from moe_router.errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    MoeError,
)
from moe_router.snapshots import SnapshotStore


def _payload(revision: int) -> dict:
    return {
        "policy": {"top_k": 2, "capacity_factor": 0.75, "block_size": 8},
        "experts": {
            "revisions": {"0": revision, "1": revision},
            "bank_digest": f"digest-{revision}",
        },
        "schedule": {
            "max_case_resident_bytes": 1024,
            "max_parallel_recompute_blocks": 2,
        },
    }


def test_whole_snapshot_replacement_and_history(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    v1 = store.commit(version=1, **_payload(1))
    assert v1["version"] == 1
    assert store.load_current()["checksum"] == v1["checksum"]

    v2_payload = _payload(2)
    v2_payload["policy"]["top_k"] = 3
    v2 = store.commit(version=2, expected_version=1, **v2_payload)
    current = store.load_current()
    assert current["version"] == 2
    assert current["policy"]["top_k"] == 3
    assert current["experts"]["revisions"]["0"] == 2
    assert current["checksum"] == v2["checksum"]
    assert store.load_history(1)["checksum"] == v1["checksum"]
    assert store.load_history(2)["checksum"] == v2["checksum"]


def test_old_snapshot_cannot_overwrite_new(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit(version=2, **_payload(2))
    with pytest.raises(MoeError) as exc_info:
        store.commit(version=1, **_payload(1))
    assert exc_info.value.code == E_SNAPSHOT_ROLLBACK
    assert store.current_version() == 2


def test_same_version_with_changed_checksum_is_busy(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit(version=1, **_payload(1))
    changed = _payload(9)
    with pytest.raises(MoeError) as exc_info:
        store.commit(version=1, **changed)
    assert exc_info.value.code == E_STAGE_BUSY
    assert store.load_current()["experts"]["revisions"]["0"] == 1


def test_checksum_tampering_is_rejected(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit(version=1, **_payload(1))
    snapshot = json.loads(store.current_path.read_text(encoding="utf-8"))
    snapshot["policy"]["top_k"] = 99
    store.current_path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(MoeError) as exc_info:
        store.load_current()
    assert exc_info.value.code == E_SNAPSHOT_CORRUPT


def test_concurrent_commit_is_rejected_immediately(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    with store.hold_commit_guard():
        with pytest.raises(MoeError) as exc_info:
            store.commit(version=1, **_payload(1))
    assert exc_info.value.code == E_STAGE_BUSY
    assert store.load_current() is None


def test_expected_version_conflict_is_busy(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit(version=1, **_payload(1))
    with pytest.raises(MoeError) as exc_info:
        store.commit(version=2, expected_version=0, **_payload(2))
    assert exc_info.value.code == E_STAGE_BUSY
    assert store.current_version() == 1
