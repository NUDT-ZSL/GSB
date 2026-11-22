from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from blockstore.canonical import sha256_text
from blockstore.errors import BlockStoreError, ErrorCode
from blockstore.engine import SnapshotEngine
from blockstore.models import load_bundle

from .conftest import SAMPLES, run_cli, write_json


def _volume_document() -> dict:
    return json.loads((SAMPLES / "volume.json").read_text(encoding="utf-8"))


def test_bad_digest_returns_business_error_and_creates_no_state(tmp_path: Path) -> None:
    volume = _volume_document()
    volume["blocks"][0]["sha256"] = "0" * 64
    volume_path = write_json(tmp_path / "bad-volume.json", volume)
    state_root = tmp_path / "state"
    result = run_cli(
        state_root,
        "load",
        "--volume",
        str(volume_path),
        "--snapshots",
        str(SAMPLES / "snapshots.json"),
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == ErrorCode.DIGEST_MISMATCH.value
    assert not state_root.exists()


def test_duplicate_snapshot_seq_is_rejected(tmp_path: Path) -> None:
    snapshots = json.loads((SAMPLES / "snapshots.json").read_text(encoding="utf-8"))
    snapshots["snapshots"][1]["seq"] = 1
    snapshots_path = write_json(tmp_path / "bad-snapshots.json", snapshots)
    with pytest.raises(BlockStoreError) as caught:
        load_bundle(
            SAMPLES / "volume.json",
            snapshots_path,
            SAMPLES / "rollbacks.json",
        )
    assert caught.value.code is ErrorCode.DUPLICATE_SNAPSHOT_SEQ


def test_parent_clock_must_be_strictly_earlier(tmp_path: Path) -> None:
    snapshots = json.loads((SAMPLES / "snapshots.json").read_text(encoding="utf-8"))
    snapshots["snapshots"][1]["created_clock"] = 1000
    snapshots_path = write_json(tmp_path / "bad-clock.json", snapshots)
    with pytest.raises(BlockStoreError) as caught:
        load_bundle(SAMPLES / "volume.json", snapshots_path)
    assert caught.value.code is ErrorCode.CLOCK_NOT_MONOTONIC


def test_snapshot_coverage_must_match_block_payloads(tmp_path: Path) -> None:
    snapshots = json.loads((SAMPLES / "snapshots.json").read_text(encoding="utf-8"))
    snapshots["snapshots"][0]["blocks"].pop()
    snapshots_path = write_json(tmp_path / "bad-coverage.json", snapshots)
    with pytest.raises(BlockStoreError) as caught:
        load_bundle(SAMPLES / "volume.json", snapshots_path)
    assert caught.value.code is ErrorCode.COVERAGE_MISMATCH


def test_parent_may_appear_later_in_json_but_must_exist(tmp_path: Path) -> None:
    snapshots = json.loads((SAMPLES / "snapshots.json").read_text(encoding="utf-8"))
    snapshots["snapshots"][1], snapshots["snapshots"][0] = (
        snapshots["snapshots"][0],
        snapshots["snapshots"][1],
    )
    reordered = write_json(tmp_path / "reordered.json", snapshots)
    bundle = load_bundle(SAMPLES / "volume.json", reordered)
    assert {item.snapshot_id for item in bundle.snapshots} == {
        "snap-a1",
        "snap-a2",
        "snap-b1",
        "snap-a3",
        "snap-c1",
    }
    snapshots["snapshots"][1]["parent_snapshot_id"] = "missing-parent"
    missing = write_json(tmp_path / "missing-parent.json", snapshots)
    with pytest.raises(BlockStoreError) as caught:
        load_bundle(SAMPLES / "volume.json", missing)
    assert caught.value.code in {
        ErrorCode.PARENT_NOT_FOUND,
        ErrorCode.CLOCK_NOT_MONOTONIC,
    }


def test_load_and_verify_sample(tmp_path: Path, sample_bundle) -> None:
    engine = SnapshotEngine(tmp_path / "store")
    result = engine.load_plan(sample_bundle)
    assert result["ok"]
    status = result["status"]
    assert status["view_version"] == 6
    assert status["logical_clock"] == 5000
    assert status["anchor_snapshot_id"] == "vol-demo@base"
    assert len(status["snapshots"]) == 6
    assert all(item["status"] == "live" for item in status["snapshots"])
    report = engine.verify()
    assert report["ok"]
    assert report["live_snapshot_count"] == 6
    assert report["orphan_snapshot_count"] == 0


def test_load_is_idempotent(loaded_engine: SnapshotEngine, sample_bundle) -> None:
    before = loaded_engine.status()
    second = loaded_engine.load_plan(sample_bundle)
    assert second["ok"]
    after = second["status"]
    assert after["view_version"] == before["view_version"]
    assert all(item["idempotent"] for item in second["applied_snapshots"])
    loaded_engine.verify()


def test_status_json_is_stable(loaded_engine: SnapshotEngine) -> None:
    first = json.dumps(loaded_engine.status(), sort_keys=True, separators=(",", ":"))
    second = json.dumps(loaded_engine.status(), sort_keys=True, separators=(",", ":"))
    assert sha256_text(first) == sha256_text(second)


def test_mismatched_volume_does_not_mutate_existing_state(
    loaded_engine: SnapshotEngine,
    tmp_path: Path,
    sample_bundle,
) -> None:
    before = loaded_engine.status()
    volume = _volume_document()
    volume["blocks"][0]["data"] = "CHANGED0"
    volume["blocks"][0]["sha256"] = hashlib.sha256(b"CHANGED0").hexdigest()
    changed_path = write_json(tmp_path / "changed-volume.json", volume)
    changed_bundle = load_bundle(changed_path, SAMPLES / "snapshots.json")
    with pytest.raises(BlockStoreError) as caught:
        loaded_engine.load_plan(changed_bundle)
    assert caught.value.code is ErrorCode.VOLUME_MISMATCH
    after = loaded_engine.status()
    assert after == before
    loaded_engine.verify()
