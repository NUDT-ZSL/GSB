"""Business rules 6 and 8: persistence, versions, digests, archiving."""

from __future__ import annotations

import json

import numpy as np
import pytest

from terrain_engine import digest, engine as engine_mod, errors, storage


def test_commit_and_reload_roundtrip(task, make_engine, workspace):
    engine = make_engine(task, workspace)
    tile = task.tiles[0]
    gray, depth, _ = __import__(
        "terrain_engine.render", fromlist=["render_tile"]).render_tile(
        task, tile)
    storage.commit_tile(
        task, workspace, tile, gray, depth,
        engine.dependencies[tile.tile_id], engine.version)
    stored = storage.load_tile(task, workspace, tile, engine.version)
    np.testing.assert_array_equal(stored.gray, gray)
    np.testing.assert_array_equal(stored.depth, depth)
    assert stored.dependency == engine.dependencies[tile.tile_id]


def test_stale_version_raises_digest(task, make_engine, workspace):
    engine = make_engine(task, workspace)
    tile = task.tiles[0]
    from terrain_engine.render import render_tile
    gray, depth, _ = render_tile(task, tile)
    storage.commit_tile(task, workspace, tile, gray, depth,
                        engine.dependencies[tile.tile_id], "v-old")
    with pytest.raises(errors.EngineError) as info:
        storage.load_tile(task, workspace, tile, "v-new")
    assert info.value.code == errors.E_DIGEST


def test_corrupt_digest_is_detected_and_re_rendered(task, make_engine,
                                                    workspace):
    engine = make_engine(task, workspace)
    tile = task.tiles[0]
    from terrain_engine.render import render_tile
    gray, depth, _ = render_tile(task, tile)
    storage.commit_tile(task, workspace, tile, gray, depth,
                        engine.dependencies[tile.tile_id],
                        engine.version, corrupt_digest=True)
    with pytest.raises(errors.EngineError) as info:
        storage.load_tile(task, workspace, tile, engine.version)
    assert info.value.code == errors.E_DIGEST

    # Recovery path: engine discards the bad tile and re-renders it.
    recovered = make_engine(task, workspace)
    assert recovered.try_reuse(tile) is None
    recovered.render_one(tile)
    stored = storage.load_tile(task, workspace, tile, engine.version)
    np.testing.assert_array_equal(stored.gray, gray)


def test_declared_pixel_drift_raises_digest(task, make_engine, workspace):
    engine = make_engine(task, workspace)
    tile = task.tiles[0]
    from terrain_engine.render import render_tile
    gray, depth, _ = render_tile(task, tile)
    manifest = storage.commit_tile(
        task, workspace, tile, gray, depth,
        engine.dependencies[tile.tile_id], engine.version)
    manifest["declared_pixels"] = manifest["declared_pixels"] + 1
    manifest_path = storage.state_path(task, workspace) / (
        tile.tile_id + ".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(errors.EngineError) as info:
        storage.load_tile(task, workspace, tile, engine.version)
    assert info.value.code == errors.E_DIGEST


def test_version_change_archives_old_generation(task, make_engine,
                                                workspace):
    engine = make_engine(task, workspace)
    tile = task.tiles[0]
    from terrain_engine.render import render_tile
    gray, depth, _ = render_tile(task, tile)
    dep = engine.dependencies[tile.tile_id]
    storage.commit_tile(task, workspace, tile, gray, depth, dep, "v1")
    storage.commit_tile(task, workspace, tile, gray, depth, dep, "v2")
    state_dir = storage.state_path(task, workspace)
    archived = state_dir / "archive" / "v1"
    assert (archived / f"{tile.tile_id}.npz").exists()
    assert (archived / f"{tile.tile_id}.manifest.json").exists()
    # Current generation remains readable at v2.
    assert storage.load_tile(task, workspace, tile, "v2") is not None


def test_preview_uses_cache_and_is_fast_path(task, make_engine, workspace):
    engine = make_engine(task, workspace)
    first = engine.preview(task.tiles[1].tile_id)
    assert first["status"] == "rendered"
    second = engine.preview(task.tiles[1].tile_id)
    assert second["status"] == "reused"


def test_atomic_publish_replaces_outputs(task, make_engine, workspace):
    engine = make_engine(task, workspace)
    engine.render_all()
    image_path = workspace / "out" / "image.pgm"
    depth_path = workspace / "out" / "depth.f32"
    image_path.parent.mkdir(parents=True)
    # Pre-existing output with a different size must be read before replace.
    dummy = np.zeros((4, 4), dtype=np.uint8)
    storage.write_pgm(image_path, dummy)
    info = engine.publish(image_path, depth_path)
    assert info["previous_header"]["width"] == 4
    header = storage.read_pgm_header(image_path)
    assert header["width"] == task.width_px
    depth = np.fromfile(depth_path, dtype="<f4").reshape(
        task.height_px, task.width_px)
    assert depth.shape == (task.height_px, task.width_px)

