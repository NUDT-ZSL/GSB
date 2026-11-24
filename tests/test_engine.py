from __future__ import annotations

import json
import signal
from dataclasses import replace

import numpy as np
import pytest

from relief_workbench.engine import WORKING_BYTES_PER_TILE_PIXEL, WorkbenchEngine
from relief_workbench.errors import WorkbenchError
from relief_workbench.renderer import ReliefRenderer


def test_preview_then_render_exact_and_cache_reuse(small_config, tmp_path) -> None:
    state = tmp_path / "state"
    output = tmp_path / "out"
    engine = WorkbenchEngine(small_config, state, output)
    preview = engine.preview_tile(1, 2)
    assert preview.spec.row == 1 and preview.spec.col == 2
    assert not preview.cache_hit
    assert preview.output_path.exists()
    assert len(engine.store.cached_tile_names()) == 1

    restarted = WorkbenchEngine(small_config, state, output)
    preview_again = restarted.preview_tile(1, 2)
    assert preview_again.cache_hit
    assert np.array_equal(preview.pixels, preview_again.pixels)

    report = restarted.render_all()
    assert report["ok"] is True
    assert report["comparison"]["max_abs_error"] == 0
    assert report["comparison"]["mismatch"] is None
    assert (
        report["comparison"]["assembled_sha256"]
        == report["comparison"]["reference_sha256"]
    )
    assert report["cache"]["reused_tiles"] == 1
    assert report["cache"]["rendered_tiles"] == 11
    assert len(list((output / "tiles").glob("*.png"))) == 12
    assert (output / "full.png").exists()
    assert (output / "report.json").exists()

    cached_engine = WorkbenchEngine(small_config, state, tmp_path / "fresh-out")
    cached_report = cached_engine.render_all()
    assert cached_report["cache"]["reused_tiles"] == 12
    assert cached_report["cache"]["rendered_tiles"] == 0
    assert cached_report["comparison"]["max_abs_error"] == 0
    assert len(list((tmp_path / "fresh-out" / "tiles").glob("*.png"))) == 12


def test_concurrency_one_and_four_are_pixel_identical(small_config, tmp_path) -> None:
    hashes = []
    for concurrency in (1, 4):
        config = replace(small_config, concurrency=concurrency)
        engine = WorkbenchEngine(
            config, tmp_path / f"state-{concurrency}", tmp_path / f"out-{concurrency}"
        )
        report = engine.render_all()
        hashes.append(report["comparison"]["assembled_sha256"])
        assert report["comparison"]["max_abs_error"] == 0
    assert hashes[0] == hashes[1]


def test_memory_budget_waits_in_row_major_order(small_config, tmp_path) -> None:
    spec = ReliefRenderer(small_config).spec(0, 0)
    task_cost = spec.pixel_count * WORKING_BYTES_PER_TILE_PIXEL
    config = replace(
        small_config,
        concurrency=4,
        memory_budget_bytes=task_cost * 2,
    )
    engine = WorkbenchEngine(config, tmp_path / "state", tmp_path / "out")
    report = engine.render_all()
    assert report["comparison"]["max_abs_error"] == 0
    assert report["cache"]["peak_in_flight_bytes"] <= config.memory_budget_bytes
    assert report["cache"]["max_observed_workers"] <= 2


def test_memory_budget_cannot_hold_a_tile(small_config, tmp_path) -> None:
    config = replace(small_config, memory_budget_bytes=1)
    engine = WorkbenchEngine(config, tmp_path / "state", tmp_path / "out")
    with pytest.raises(WorkbenchError) as caught:
        engine.render_all()
    assert caught.value.code == "E_MEMORY_BUDGET"
    assert not (tmp_path / "out" / "full.png").exists()


def test_render_exception_cancels_pending(small_config, tmp_path) -> None:
    config = replace(small_config, concurrency=2)
    engine = WorkbenchEngine(config, tmp_path / "state", tmp_path / "out")
    original = engine._render_and_store

    def failing(spec, stats):
        if spec.row == 0 and spec.col == 1:
            raise RuntimeError("synthetic tile failure")
        return original(spec, stats)

    engine._render_and_store = failing  # type: ignore[method-assign]
    with pytest.raises(WorkbenchError) as caught:
        engine.render_all()
    assert caught.value.code == "E_RENDER"
    assert "synthetic tile failure" in caught.value.details["reason"]
    assert len(engine.store.cached_tile_names()) < len(engine.renderer.specs)
    assert not (tmp_path / "out" / "report.json").exists()


def test_digest_tamper_is_reported_and_repaired(small_config, tmp_path) -> None:
    state = tmp_path / "state"
    output = tmp_path / "out"
    engine = WorkbenchEngine(small_config, state, output)
    first = engine.render_all()
    assert first["comparison"]["max_abs_error"] == 0

    spec = engine.renderer.spec(2, 3)
    tile_path = engine.store.tile_path(spec)
    payload = bytearray(tile_path.read_bytes())
    payload[-1] ^= 0xFF
    tile_path.write_bytes(payload)

    restarted = WorkbenchEngine(small_config, state, output)
    repaired = restarted.render_all()
    assert repaired["comparison"]["max_abs_error"] == 0
    assert repaired["digest_repairs"]
    assert repaired["digest_repairs"][0]["code"] == "E_DIGEST"
    assert repaired["digest_repairs"][0]["reason"] == "digest_mismatch"


def test_truncated_pixel_count_is_reported_and_repaired(small_config, tmp_path) -> None:
    state = tmp_path / "state"
    output = tmp_path / "out"
    engine = WorkbenchEngine(small_config, state, output)
    engine.render_all()
    spec = engine.renderer.spec(0, 2)
    tile_path = engine.store.tile_path(spec)
    tile_path.write_bytes(tile_path.read_bytes()[:-1])

    restarted = WorkbenchEngine(small_config, state, output)
    report = restarted.render_all()
    assert report["comparison"]["max_abs_error"] == 0
    reasons = {item["reason"] for item in report["digest_repairs"]}
    assert "pixel_count_mismatch" in reasons


def test_version_change_purges_cache_and_outputs(small_config, tmp_path) -> None:
    state = tmp_path / "state"
    output = tmp_path / "out"
    first = WorkbenchEngine(small_config, state, output)
    first_report = first.render_all()
    assert first_report["ok"] is True
    assert (output / "full.png").exists()

    changed = replace(small_config, seed=small_config.seed + 1)
    second = WorkbenchEngine(changed, state, output)
    second.prepare()
    assert second.store.purged
    assert second.store.purge_reason == "version_mismatch"
    assert not list((state / "tiles").glob("*.tile"))
    assert not (output / "full.png").exists()
    assert not (output / "report.json").exists()

    second_report = second.render_all()
    assert second_report["cache"]["reused_tiles"] == 0
    assert second_report["comparison"]["max_abs_error"] == 0
    assert (
        second_report["comparison"]["assembled_sha256"]
        != first_report["comparison"]["assembled_sha256"]
    )


def test_handled_termination_writes_marker_and_resumes(small_config, tmp_path) -> None:
    state = tmp_path / "state"
    output = tmp_path / "out"
    engine = WorkbenchEngine(small_config, state, output)
    engine.preview_tile(0, 0)
    engine.request_termination(signal.SIGTERM)
    with pytest.raises(WorkbenchError) as caught:
        engine.render_all()
    assert caught.value.code == "E_INTERRUPTED"
    assert caught.value.exit_code == 143
    marker = json.loads((state / "interrupted.json").read_text(encoding="utf-8"))
    assert marker["exit_code"] == 143
    assert marker["signal"] == int(signal.SIGTERM)
    assert len(marker["completed"]) == 1
    assert not (output / "full.png").exists()
    assert not (output / "report.json").exists()

    resumed = WorkbenchEngine(small_config, state, output)
    report = resumed.render_all()
    assert report["ok"] is True
    assert report["previous_interruption"] is not None
    assert report["cache"]["reused_tiles"] == 1
    assert report["comparison"]["max_abs_error"] == 0


def test_consistency_error_reports_first_pixel_and_writes_report(
    small_config, tmp_path
) -> None:
    output = tmp_path / "out"
    engine = WorkbenchEngine(small_config, tmp_path / "state", output)
    original_render_full = engine.renderer.render_full

    def shifted_reference(stats):
        reference = original_render_full(stats)
        reference[0, 0] = (int(reference[0, 0]) + 37) % 256
        return reference

    engine.renderer.render_full = shifted_reference  # type: ignore[method-assign]
    with pytest.raises(WorkbenchError) as caught:
        engine.render_all()
    assert caught.value.code == "E_CONSISTENCY"
    mismatch = caught.value.details
    assert mismatch["x"] == 0
    assert mismatch["y"] == 0
    assert mismatch["tile"]["row"] == 0
    assert mismatch["tile"]["col"] == 0
    assert mismatch["assembled"] != mismatch["reference"]
    assert mismatch["abs_error"] > 0
    assert mismatch["cause"]
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["comparison"]["mismatch"]["x"] == 0
