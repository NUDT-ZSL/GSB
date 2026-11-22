from __future__ import annotations

from pathlib import Path

import pytest

from blockstore.canonical import sha256_bytes
from blockstore.errors import BlockStoreError, ErrorCode
from blockstore.engine import SnapshotEngine
from blockstore.models import BlockData, SnapshotSpec

from .conftest import query_db


def _snapshot_from_data(
    *,
    snapshot_id: str,
    seq: int,
    parent: str,
    clock: int,
    block_no: int,
    data: bytes,
) -> SnapshotSpec:
    digest = sha256_bytes(data)
    return SnapshotSpec(
        snapshot_id=snapshot_id,
        seq=seq,
        volume_id="vol-demo",
        parent_snapshot_id=parent,
        created_clock=clock,
        covered_start=block_no,
        covered_end=block_no,
        blocks={
            block_no: BlockData(
                block_no=block_no,
                data=data,
                digest=digest,
                encoding="utf8",
                encoded_data=data.decode("utf-8"),
            )
        },
    )


def test_cow_inheritance_and_content_addressing_dedup(
    loaded_engine: SnapshotEngine,
) -> None:
    before_files = set(loaded_engine.blocks.iter_digests())
    repeated = _snapshot_from_data(
        snapshot_id="snap-a5",
        seq=7,
        parent="snap-a2",
        clock=6000,
        block_no=1,
        data=b"A1000001",
    )
    result = loaded_engine.create_snapshot(repeated)
    assert result["created"]
    after_files = set(loaded_engine.blocks.iter_digests())
    assert before_files == after_files
    rows = query_db(
        loaded_engine,
        "SELECT block_no, digest, source FROM snapshot_blocks "
        "WHERE snapshot_id = ? ORDER BY block_no",
        ("snap-a5",),
    )
    assert len(rows) == 8
    assert rows[1]["source"] == "cow"
    assert rows[0]["source"] == "inherit"
    assert rows[0]["digest"] == "8c05d20f0c2b72b285bdffd4e100468417a2059ca9ed2cd63ea799a45f55c3a8"
    loaded_engine.verify()


def test_same_snapshot_is_idempotent_but_changed_content_conflicts(
    loaded_engine: SnapshotEngine,
) -> None:
    spec = _snapshot_from_data(
        snapshot_id="snap-a5",
        seq=7,
        parent="snap-a2",
        clock=6000,
        block_no=1,
        data=b"A1000001",
    )
    first = loaded_engine.create_snapshot(spec)
    second = loaded_engine.create_snapshot(spec)
    assert first["created"]
    assert second["idempotent"]
    assert second["view_version"] == first["view_version"]
    changed = _snapshot_from_data(
        snapshot_id="snap-a5",
        seq=7,
        parent="snap-a2",
        clock=6000,
        block_no=1,
        data=b"CHANGED1",
    )
    with pytest.raises(BlockStoreError) as caught:
        loaded_engine.create_snapshot(changed)
    assert caught.value.code is ErrorCode.SNAPSHOT_CONFLICT
    loaded_engine.verify()


def test_tampered_block_is_rejected(loaded_engine: SnapshotEngine) -> None:
    digest = "8c05d20f0c2b72b285bdffd4e100468417a2059ca9ed2cd63ea799a45f55c3a8"
    path = loaded_engine.blocks.path_for(digest)
    path.write_bytes(b"TAMPERED")
    with pytest.raises(BlockStoreError) as caught:
        loaded_engine.verify()
    assert caught.value.code is ErrorCode.VERIFY_FAILED
    errors = caught.value.details["errors"]
    file_check = next(
        item for item in errors if item["name"] == "referenced_block_files_valid"
    )
    assert any(
        detail.get("code") == ErrorCode.BLOCK_CORRUPT.value
        for detail in file_check["details"]
    )
    new_snapshot = _snapshot_from_data(
        snapshot_id="snap-a5",
        seq=7,
        parent="snap-a2",
        clock=6000,
        block_no=3,
        data=b"NEWBLOCK",
    )
    with pytest.raises(BlockStoreError) as rejected:
        loaded_engine.create_snapshot(new_snapshot)
    assert rejected.value.code is ErrorCode.BLOCK_CORRUPT


def test_block_files_have_one_copy_per_digest(loaded_engine: SnapshotEngine) -> None:
    files = list(loaded_engine.blocks.blocks_dir.glob("*.blk"))
    digests = [path.name[:-4] for path in files]
    assert len(digests) == len(set(digests))
    assert all(path.name == f"{digest}.blk" for path, digest in zip(files, digests))
