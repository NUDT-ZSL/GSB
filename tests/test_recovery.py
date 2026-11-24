from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np

from surface_stitch.pipeline import PipelineConfig, run_pipeline
from surface_stitch.storage import INTERRUPT_MARKER


def test_preset_stop_writes_marker_without_complete_tiles(
    sample_dir: Path, tmp_path: Path
) -> None:
    stop = threading.Event()
    stop.set()
    state = tmp_path / "state"
    report = run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "out",
            state_dir=state,
            stop_event=stop,
        )
    )
    assert report["interrupted"] is True
    assert report["exit_code"] == 130
    marker = json.loads((state / INTERRUPT_MARKER).read_text(encoding="utf-8"))
    assert marker["exit_code"] == 130
    assert not list((state / "tiles").glob("*.manifest.json"))
    assert not (tmp_path / "out" / "mosaic.png").exists()


def test_interrupt_after_one_then_resume(sample_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    first = run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "first-out",
            state_dir=state,
            workers=1,
            interrupt_after=1,
        )
    )
    assert first["interrupted"] is True
    assert first["completed_tiles"] == ["tile_00"]
    manifests = sorted(path.name for path in (state / "tiles").glob("*.manifest.json"))
    assert manifests == ["tile_00.manifest.json"]

    resumed = run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "resume-out",
            state_dir=state,
            workers=2,
        )
    )
    assert resumed["ok"] is True
    statuses = {tile["tile_id"]: tile["persistence_status"] for tile in resumed["tiles"]}
    assert statuses["tile_00"] == "reused"
    assert sum(status == "processed" for status in statuses.values()) == 3


def test_digest_tamper_is_reported_and_recomputed(
    sample_dir: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "out1",
            state_dir=state,
            workers=1,
        )
    )
    manifest_path = state / "tiles" / "tile_02.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["array_digest"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "out2",
            state_dir=state,
            workers=1,
        )
    )
    tile02 = next(tile for tile in report["tiles"] if tile["tile_id"] == "tile_02")
    assert "E_DIGEST" in tile02["business_codes"]
    assert tile02["persistence_status"] == "processed"
    events = [
        event
        for event in report["digest_checks"]
        if event["tile_id"] == "tile_02"
    ]
    assert events[0]["status"] == "invalid"
    assert events[0]["code"] == "E_DIGEST"
    assert events[-1]["status"] == "valid"


def test_hard_kill_orphan_pixel_file_is_not_complete(
    sample_dir: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    first = run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "first",
            state_dir=state,
            workers=1,
            interrupt_after=1,
        )
    )
    assert first["interrupted"] is True

    tiles_dir = state / "tiles"
    np.savez(tiles_dir / "tile_01.npz", aligned=np.zeros((224, 224), dtype=np.float32))
    (tiles_dir / "tile_02.manifest.json.tmp").write_text("partial", encoding="utf-8")

    report = run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "recovered",
            state_dir=state,
            workers=1,
        )
    )
    assert report["ok"] is True
    statuses = {tile["tile_id"]: tile["persistence_status"] for tile in report["tiles"]}
    assert statuses["tile_00"] == "reused"
    assert statuses["tile_01"] == "processed"
    assert not (tiles_dir / "tile_02.manifest.json.tmp").exists()
