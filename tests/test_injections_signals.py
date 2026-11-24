"""Business rules 7 and 8: exception chains, signals, restart recovery."""

from __future__ import annotations
import json
import threading

import numpy as np
import pytest

from terrain_engine import engine as engine_mod
from terrain_engine import errors, scheduler, storage


def test_height_mismatch_injection_isolated_per_tile(task, make_engine,
                                                     workspace, sample_dir):
    injections = json.loads(
        (sample_dir / "injections.json").read_text(encoding="utf-8"))
    engine = make_engine(
        task, workspace,
        {key: value for key, value in injections.items()
         if key != "tile_not_divisible"})
    report = engine.render_all()
    failed = [item for item in report["outcomes"]
              if item["status"] == "failed"]
    codes = {item["error_code"] for item in failed}
    assert errors.E_HEIGHT_COUNT in codes
    assert errors.E_DIGEST in codes
    # Legal tiles still complete.
    assert sum(item["status"] == "rendered"
               for item in report["outcomes"]) == len(task.tiles) - 2


def test_tile_not_divisible_injection(task, sample_dir):
    injections = json.loads(
        (sample_dir / "injections.json").read_text(encoding="utf-8"))
    with pytest.raises(errors.EngineError) as info:
        engine_mod.Engine(task, "/tmp/unused",
                          injections={"tile_not_divisible":
                                      injections["tile_not_divisible"]})
    assert info.value.code == errors.E_TILE_DIVISIBILITY


def test_out_of_bounds_edit_recorded_not_raised(task, make_engine,
                                                workspace, sample_dir):
    from terrain_engine.model import Edit
    engine = make_engine(task, workspace)
    engine.render_all()
    bad = Edit(9, 0, "raise", 40, 30, 55, 40, 2)
    report = engine.incremental(task.terrain, [bad])
    entry = report["invalidation"][0]
    assert entry["error_code"] == errors.E_BOUNDS
    assert report["rerender_count"] == 0


def test_graceful_stop_only_at_tile_boundary(task, make_engine, workspace):
    stop_event = threading.Event()
    engine = make_engine(task, workspace, stop_event=stop_event)
    stop_event.set()  # signalled before any tile is admitted
    report = engine.render_all()
    assert report["interrupted"] is True
    state_dir = storage.state_path(task, workspace)
    marker = json.loads(
        (state_dir / engine_mod.INTERRUPT_MARKER).read_text(encoding="utf-8"))
    assert marker["exit_code"] == scheduler.GRACEFUL_EXIT_CODE
    # No tile was falsely marked complete; manifests must not exist.
    assert not list(state_dir.glob("*.manifest.json"))


def test_restart_recovers_only_valid_committed_tiles(task, make_engine,
                                                     workspace):
    first = make_engine(task, workspace)
    first.render_all()
    # Damage one manifest to simulate a hard-killed partial write.
    tile = task.tiles[3]
    manifest_path = storage.state_path(task, workspace) / (
        tile.tile_id + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["array_digest"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    restarted = make_engine(task, workspace)
    report = restarted.render_all()
    digest_failures = report["digest_failures"]
    assert any(item["code"] == errors.E_DIGEST for item in digest_failures)
    assert tile.tile_id in {
        item["tile"] for item in report["outcomes"]
        if item["status"] == "rendered"}
    # After recovery every tile validates cleanly.
    for candidate in task.tiles:
        assert storage.load_tile(
            task, workspace, candidate, restarted.version) is not None


def test_hard_kill_without_marker_relies_on_committed_tiles(
        task, make_engine, workspace):
    engine = make_engine(task, workspace)
    engine.preview(task.tiles[0].tile_id)
    # Simulate process death: no interrupt marker is written.  A new engine
    # simply resumes from whatever digest-valid tiles exist.
    resumed = make_engine(task, workspace)
    report = resumed.render_all()
    assert task.tiles[0].tile_id in report["reused_tiles"]
    assert not (storage.state_path(task, workspace)
                / engine_mod.INTERRUPT_MARKER).exists()

