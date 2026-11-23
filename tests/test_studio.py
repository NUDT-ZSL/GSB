from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from matforge.errors import MatForgeError
from matforge.params import MaterialParams
from matforge.pipeline import generate_full
from matforge.plan import BlockPlan
from matforge.snapshot import SnapshotStore
from matforge.studio import MaterialStudio, ResourceLimits

PLAN = BlockPlan(width=96, height=64, tile_width=32, tile_height=32)


def make_studio(tmp_path: Path, **limit_overrides: float) -> MaterialStudio:
    limits = ResourceLimits(
        max_material_pixels=limit_overrides.get("max_material_pixels", 1_000_000),
        max_block_resident_bytes=limit_overrides.get("max_block_resident_bytes", 8_000_000),
        max_parallel_slices=limit_overrides.get("max_parallel_slices", 4),
        preview_latency_budget_ms=limit_overrides.get("preview_latency_budget_ms", 500.0),
    )
    return MaterialStudio(cache_root=tmp_path / "cache", snapshot_path=tmp_path / "snapshot.json", plan=PLAN, limits=limits)


def test_preview_is_partial_fast_and_has_actual_observation(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    result = studio.preview(0, budget_ms=1000)
    assert result["source"] == "generated"
    assert result["progress"]["ready"] == 1
    assert result["progress"]["total"] == PLAN.block_count
    observed = result["observed"]
    assert observed["elapsed_ms"] >= 0
    assert observed["source"] == "perf_counter_around_local_generation_loop"
    assert observed["estimate"]["pixel_count"] == 32 * 32
    assert observed["estimate"]["octave_count"] == MaterialParams().noise.octaves
    assert "seam_neighborhood_pixels" in observed["estimate"]
    again = studio.preview(0, budget_ms=1000)
    assert again["source"] == "state_reuse"
    assert again["slice"]["channel_digest"] == result["slice"]["channel_digest"]


def test_latency_budget_error_contains_completed_blocks(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    with pytest.raises(MatForgeError) as caught:
        studio.preview(0, budget_ms=0.0)
    error = caught.value
    assert error.code == "E_LATENCY_BUDGET"
    assert error.details["completed_blocks"] == [0]
    assert error.details["observed"]["observed_over_budget"] is True
    assert studio.progress()["ready"] == 1


def test_request_remaining_and_stitch_equal_full(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    studio.preview(0)
    remaining = studio.request_remaining(parallelism=3)
    assert remaining["reused"] == [0]
    assert remaining["recomputed"] == list(range(1, PLAN.block_count))
    verification = studio.verify()
    assert verification["equal"] is True
    assert verification["mismatch_pixels"] == 0
    full = generate_full(studio.params, studio.plan)
    assert verification["stitched_digest"] == full.digest


def test_parallel_preview_many(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    result = studio.preview_many([3, 0, 2, 1], parallelism=4)
    assert result["ok"] is True
    assert sorted(item["slice"]["block"] for item in result["results"]) == [0, 1, 2, 3]
    assert result["parallelism"] == 4
    assert studio.progress()["ready"] == 4


def test_metadata_revision_reuses_all_and_global_revision_recomputes_suffix(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    studio.request_remaining(parallelism=4)
    metadata = studio.params.with_revision(2, name="Only a label")
    report = studio.commit_plan(metadata, studio.plan, generate_suffix=False)
    assert report["affected"] == []
    assert report["reused"] == list(range(PLAN.block_count))
    assert report["recomputed"] == []
    assert report["coverage"]["covers_all"] is True
    parameter = studio.params.with_revision(3, base_color=(120, 80, 50), roughness=0.7)
    report2 = studio.commit_plan(parameter, studio.plan, generate_suffix=True, parallelism=2)
    assert report2["affected"] == list(range(PLAN.block_count))
    assert report2["affected_suffix"] == list(range(PLAN.block_count))
    assert report2["recomputed"] == list(range(PLAN.block_count))
    assert report2["reused"] == []
    assert report2["coverage"]["disjoint"] is True
    assert report2["coverage"]["covers_all"] is True
    assert studio.verify()["equal"] is True


def test_stale_slice_is_rejected_with_trace_fields(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    studio.request_remaining()
    old_record = studio.ready[0]
    studio.apply_revision(studio.params.with_revision(2, base_color=(100, 120, 80)))
    with pytest.raises(MatForgeError) as caught:
        studio.inspect_stale(0)
    error = caught.value
    assert error.code == "E_STALE_SLICE"
    assert error.details["material_id"] == studio.params.material_id
    assert error.details["block"] == 0
    assert error.details["stale_version"] == 1
    assert error.details["current_version"] == 2
    assert error.details["cache_key"] == old_record.cache_key
    assert studio.ready[0].params_revision == 2
    assert studio.verify()["equal"] is True


def test_replay_regenerates_identical_pixels_and_digest(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    studio.preview(0)
    replay = studio.replay_slice(0)
    record = studio.ready[0]
    assert replay["replay_mode"] == "regenerated_and_compared_digest"
    assert replay["replayed_channel_digest"] == record.channel_digest
    assert replay["replayed_cache_key"] == record.cache_key


def test_snapshot_restart_replaces_state_whole(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    studio.preview(0)
    versions = studio.snapshot_versions()
    snapshot_file = tmp_path / "snapshot.json"
    document = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert document["ready"]["checksum"] == studio.snapshot_store.current.ready["checksum"]
    restarted = make_studio(tmp_path)
    assert restarted.params.revision == studio.params.revision
    assert restarted.progress()["ready"] == 1
    assert restarted.snapshot_versions()["ready_version"] == versions["ready_version"]
    restarted.preview(1)
    restarted.request_remaining()
    assert restarted.verify()["equal"] is True


def test_plan_only_commit_is_persisted_before_generation(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    studio.request_remaining()
    new_plan = BlockPlan(width=128, height=64, tile_width=32, tile_height=32)
    params = studio.params.with_revision(2)
    report = studio.commit_plan(params, new_plan, generate_suffix=False)
    on_disk = SnapshotStore(tmp_path / "snapshot.json").load(replace=False)
    assert on_disk.params["version"] == report["versions"]["params_version"]
    assert on_disk.plan["version"] == report["versions"]["plan_version"]
    assert on_disk.ready["version"] == report["versions"]["ready_version"]
    assert on_disk.ready["items"] == []
    restarted = make_studio(tmp_path)
    assert restarted.plan.width == 128
    assert restarted.progress()["ready"] == 0


def test_snapshot_rollback_and_corruption_codes(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    studio.request_remaining()
    old_snapshot = studio.snapshot_store.current
    assert old_snapshot is not None
    studio.apply_revision(studio.params.with_revision(2, base_color=(1, 2, 3)))
    store = studio.snapshot_store
    with pytest.raises(MatForgeError) as rollback:
        store.save(old_snapshot)
    assert rollback.value.code == "E_SNAPSHOT_ROLLBACK"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with pytest.raises(MatForgeError) as damaged:
        SnapshotStore(corrupt).load()
    assert damaged.value.code == "E_SNAPSHOT_CORRUPT"
    snapshot = store.current
    assert snapshot is not None
    tampered = snapshot.to_dict()
    tampered["ready"]["items"][0]["channel_digest"] = "00" * 32
    corrupt.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(MatForgeError) as damaged2:
        SnapshotStore(corrupt).load()
    assert damaged2.value.code == "E_SNAPSHOT_CORRUPT"


def test_commit_lock_is_non_blocking(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        assert studio._commit_lock.acquire()
        entered.set()
        release.wait(timeout=5)
        studio._commit_lock.release()

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(MatForgeError) as caught:
            studio.commit_plan(studio.params.with_revision(2), studio.plan)
        assert caught.value.code == "E_STAGE_BUSY"
    finally:
        release.set()
        thread.join(timeout=2)


def test_resource_limits_and_streaming_degradation(tmp_path: Path) -> None:
    with pytest.raises(MatForgeError) as material:
        make_studio(tmp_path, max_material_pixels=100)
    assert material.value.code == "E_RESOURCE_LIMIT"
    with pytest.raises(MatForgeError) as block:
        make_studio(tmp_path, max_block_resident_bytes=1000)
    assert block.value.code == "E_RESOURCE_LIMIT"
    studio = make_studio(tmp_path, max_parallel_slices=2)
    with pytest.raises(MatForgeError) as parallel:
        studio.request_remaining(parallelism=8, allow_degrade=False)
    assert parallel.value.code == "E_RESOURCE_LIMIT"
    degraded = studio.request_remaining(parallelism=8, allow_degrade=True)
    assert degraded["degraded"] == ["streaming_serial_degraded"]
    assert degraded["parallelism"] == 1


def test_mismatch_adjudication_has_first_pixel_block_and_spectrum(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    full = generate_full(studio.params, studio.plan)
    stitched = full.channels.copy()
    stitched[40, 70, 3] = (int(stitched[40, 70, 3]) + 9) % 256
    with pytest.raises(MatForgeError) as caught:
        studio.adjudicate(stitched, full)
    error = caught.value
    assert error.code == "E_BLOCK_MISMATCH"
    assert error.details["first_deviation_pixel"]["x"] == 70
    assert error.details["first_deviation_pixel"]["y"] == 40
    assert error.details["first_deviation_pixel"]["channel"] == 3
    assert error.details["block"] == 5
    assert len(error.details["deviation_spectrum"]) == 7


def test_cache_tamper_is_detected(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    studio.preview(0)
    record = studio.ready[0]
    obj = studio.cache.get(record.cache_key)
    channel_file = Path(obj.path) / "channels.npy"
    payload = bytearray(channel_file.read_bytes())
    payload[-1] ^= 0xFF
    channel_file.write_bytes(payload)
    with pytest.raises(MatForgeError) as caught:
        studio.cache.get(record.cache_key)
    assert caught.value.code == "E_CACHE_CORRUPT"


def test_selfcheck_reports_partition_and_crosscheck(tmp_path: Path) -> None:
    studio = make_studio(tmp_path)
    studio.request_remaining()
    studio.apply_revision(studio.params.with_revision(2, base_color=(90, 110, 130)))
    report = studio.selfcheck()
    assert report["ok"] is True
    names = {check["name"] for check in report["checks"]}
    assert names == {"stitched_equals_full", "cache_key_and_digest_crosscheck", "recompute_reuse_partition", "snapshot_checksums"}

