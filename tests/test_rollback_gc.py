from __future__ import annotations

from pathlib import Path

import pytest

from blockstore.canonical import sha256_bytes
from blockstore.engine import SnapshotEngine
from blockstore.errors import BlockStoreError, ErrorCode
from blockstore.models import BlockData, RollbackRequest, SnapshotSpec

from .conftest import query_db


MAIN_ROLLBACK = RollbackRequest(
    request_id="rb-main",
    target_snapshot_id="snap-a2",
    rollback_clock=10000,
    keep_after_target=False,
)


def test_rollback_orphan_closure_and_volume_switch(
    loaded_engine: SnapshotEngine,
) -> None:
    result = loaded_engine.rollback(MAIN_ROLLBACK)
    assert result["applied"]
    assert result["view_version"] == 7
    assert {item["snapshot_id"] for item in result["orphaned_snapshots"]} == {
        "snap-b1",
        "snap-a3",
        "snap-c1",
    }
    for item in result["orphaned_snapshots"]:
        path = item["reference_path"]
        assert path["lowest_common_ancestor"] in path["target_to_lca"]
        assert path["lowest_common_ancestor"] in path["orphan_to_lca"]
    status = loaded_engine.status()
    states = {item["snapshot_id"]: item["status"] for item in status["snapshots"]}
    assert states == {
        "vol-demo@base": "live",
        "snap-a1": "live",
        "snap-a2": "live",
        "snap-b1": "orphan",
        "snap-a3": "orphan",
        "snap-c1": "orphan",
    }
    orphan_refs = query_db(
        loaded_engine,
        "SELECT COUNT(*) AS n FROM block_refs WHERE source_kind = 'snapshot' "
        "AND source_id IN ('snap-b1','snap-a3','snap-c1')",
    )
    assert orphan_refs[0]["n"] == 0
    target_rows = query_db(
        loaded_engine,
        "SELECT digest FROM snapshot_blocks WHERE snapshot_id = 'snap-a2' "
        "ORDER BY block_no",
    )
    volume_rows = query_db(
        loaded_engine,
        "SELECT digest FROM volume_blocks ORDER BY block_no",
    )
    assert [row["digest"] for row in target_rows] == [
        row["digest"] for row in volume_rows
    ]
    loaded_engine.verify()


def test_rollback_is_idempotent_and_stale_requests_are_rejected(
    loaded_engine: SnapshotEngine,
) -> None:
    first = loaded_engine.rollback(MAIN_ROLLBACK)
    second = loaded_engine.rollback(MAIN_ROLLBACK)
    assert first["view_version"] == second["view_version"] == 7
    assert second["idempotent"]
    with pytest.raises(BlockStoreError) as caught:
        loaded_engine.rollback(
            RollbackRequest(
                request_id="rb-stale",
                target_snapshot_id="snap-a1",
                rollback_clock=9000,
                keep_after_target=True,
            )
        )
    assert caught.value.code is ErrorCode.ROLLBACK_STALE
    assert loaded_engine.status()["view_version"] == 7

    with pytest.raises(BlockStoreError) as conflict:
        loaded_engine.rollback(
            RollbackRequest(
                request_id="rb-equal",
                target_snapshot_id="snap-a1",
                rollback_clock=10000,
                keep_after_target=False,
            )
        )
    assert conflict.value.code is ErrorCode.ROLLBACK_CONFLICT
    assert loaded_engine.status()["view_version"] == 7
    loaded_engine.verify()


def test_gc_deletes_only_orphan_exclusive_blocks_and_is_deterministic(
    loaded_engine: SnapshotEngine,
) -> None:
    loaded_engine.rollback(MAIN_ROLLBACK)
    first = loaded_engine.gc()
    assert first["deleted_count"] == 5
    assert first["retained_count"] == 11
    deleted = {
        item["digest"] for item in first["actions"] if item["action"] == "delete"
    }
    assert deleted == {
        "2ad16fb49d4329ca4c4c098f2fb4a23ea9b38eb75a9969ee3c48b2a2c4ec267a",
        "f9fe5c7c7e5d596bccc001b13c5b7864d77d060bf770dfbc7fdac9beadc019f2",
        "1b8494136e7b60d030f1a73a31903e1df921d63dda46cd42b88619e651dae4b7",
        "1870e325cbd77b4d3c711770c1f25f98c773706eddbaa75299afea76fdc98f21",
        "65c61bc6686509bd74bf714300c33a017f8e6d054a2ae3e58e74376554f45880",
    }
    for digest in deleted:
        assert not loaded_engine.blocks.path_for(digest).exists()
    second = loaded_engine.gc()
    assert second["idempotent"]
    assert second["actions"] == first["actions"]
    assert second["deleted_count"] == first["deleted_count"]
    loaded_engine.verify()


def test_new_snapshot_after_rollback_and_orphan_parent_rejection(
    loaded_engine: SnapshotEngine,
) -> None:
    loaded_engine.rollback(MAIN_ROLLBACK)
    data = b"A4000000"
    spec = SnapshotSpec(
        snapshot_id="snap-a4",
        seq=6,
        volume_id="vol-demo",
        parent_snapshot_id="snap-a2",
        created_clock=20000,
        covered_start=0,
        covered_end=0,
        blocks={
            0: BlockData(
                block_no=0,
                data=data,
                digest=sha256_bytes(data),
                encoding="utf8",
                encoded_data=data.decode("utf-8"),
            )
        },
    )
    result = loaded_engine.create_snapshot(spec)
    assert result["created"]
    status = loaded_engine.status()
    assert status["view_version"] == 8
    assert status["anchor_snapshot_id"] == "snap-a2"
    states = {item["snapshot_id"]: item["status"] for item in status["snapshots"]}
    assert states["snap-a4"] == "live"
    orphan_child = SnapshotSpec(
        snapshot_id="snap-invalid",
        seq=8,
        volume_id="vol-demo",
        parent_snapshot_id="snap-a3",
        created_clock=21000,
        covered_start=0,
        covered_end=0,
        blocks={0: spec.blocks[0]},
    )
    with pytest.raises(BlockStoreError) as caught:
        loaded_engine.create_snapshot(orphan_child)
    assert caught.value.code is ErrorCode.PARENT_ORPHANED
    loaded_engine.verify()
