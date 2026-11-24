from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from surface_stitch.fusion import fusion_digest
from surface_stitch.pipeline import PipelineConfig, run_pipeline
from surface_stitch.registration import Registration


def test_pipeline_registers_arbitrates_and_fuses(
    sample_dir: Path, tmp_path: Path
) -> None:
    report = run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "out",
            state_dir=tmp_path / "state",
            workers=2,
        )
    )

    assert report["ok"] is True
    registration = report["registration_summary"]
    assert registration["max_integer_error_px"] == 0
    assert registration["max_subpixel_error_px"] <= registration["subpixel_tolerance_px"]
    assert registration["passed"] is True
    assert report["conflict_count"] == 12
    assert len(report["conflicts"]) == 12
    conflict = report["conflicts"][0]
    assert conflict["source"] if "source" in conflict else True
    assert conflict["v1"]["source"].endswith(".json#/records/1")
    assert "pixel_xy" in conflict
    assert report["fusion_consistency"]["weighted_and_row_serial_pixels_identical"] is True
    assert report["fusion_consistency"]["uncovered_pixels"] == 0
    assert report["radiometry_summary"]["mean_overlap_mae_after"] < report[
        "radiometry_summary"
    ]["mean_overlap_mae_before"]

    image_path = tmp_path / "out" / "mosaic.png"
    report_path = tmp_path / "out" / "registration_verification_report.json"
    assert image_path.exists() and report_path.exists()
    with Image.open(image_path) as image:
        assert image.size == (320, 320)
        assert image.text["surface_stitch_version"] == report["version_id"]


def test_worker_counts_produce_identical_pixels(
    sample_dir: Path, tmp_path: Path
) -> None:
    serial = run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "serial-out",
            state_dir=tmp_path / "serial-state",
            workers=1,
        )
    )
    parallel = run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "parallel-out",
            state_dir=tmp_path / "parallel-state",
            workers=3,
        )
    )
    assert serial["fusion_consistency"]["fused_sha256"] == parallel[
        "fusion_consistency"
    ]["fused_sha256"]

    def pixels(path: Path) -> np.ndarray:
        return np.asarray(Image.open(path / "mosaic.png"))

    assert np.array_equal(
        pixels(tmp_path / "serial-out"), pixels(tmp_path / "parallel-out")
    )
    assert fusion_digest(pixels(tmp_path / "serial-out")) == serial[
        "fusion_consistency"
    ]["fused_sha256"]


def test_low_confidence_is_marked_for_manual_review(
    sample_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    import surface_stitch.pipeline as pipeline_mod

    original = pipeline_mod.phase_correlate

    def low_confidence(*args, **kwargs):
        result = original(*args, **kwargs)
        return Registration(
            integer_dx=result.integer_dx,
            integer_dy=result.integer_dy,
            subpixel_dx=result.subpixel_dx,
            subpixel_dy=result.subpixel_dy,
            confidence=0.10,
            fft_peak_norm=result.fft_peak_norm,
            response_peak=result.response_peak,
        )

    monkeypatch.setattr(pipeline_mod, "phase_correlate", low_confidence)
    report = run_pipeline(
        PipelineConfig(
            sample_dir=sample_dir,
            output_dir=tmp_path / "out",
            state_dir=tmp_path / "state",
            workers=1,
            check_serial=False,
        )
    )
    review = set(report["registration_summary"]["needs_manual_review_tiles"])
    assert {"tile_01", "tile_02", "tile_03"}.issubset(review)
    assert all(
        tile["needs_manual_review"]
        and tile["adoption_status"] == "provisional_requires_manual_confirmation"
        for tile in report["tiles"]
        if tile["tile_id"] != "tile_00"
    )
