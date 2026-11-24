"""Business rule 5: edit dependency closure and incremental parity."""

from __future__ import annotations

import copy

import numpy as np
import pytest
from dataclasses import replace

from terrain_engine import digest, edits as edit_mod, engine as engine_mod
from terrain_engine import storage
from terrain_engine.model import Edit


def full_render_arrays(engine):
    images = engine.composite()
    return images["gray"].copy(), images["depth"].copy()


def test_apply_raise_lower_smooth(task):
    terrain = task.terrain
    original = terrain.heights.copy()
    edit_mod.apply_edit(terrain, Edit(1, 0, "raise", 2, 2, 5, 5, 3))
    assert np.array_equal(
        terrain.heights[2:5, 2:5], original[2:5, 2:5] + 3)
    edit_mod.apply_edit(terrain, Edit(1, 1, "lower", 2, 2, 5, 5, 1))
    assert np.array_equal(
        terrain.heights[2:5, 2:5], original[2:5, 2:5] + 2)
    before_smooth = terrain.heights.copy()
    edit_mod.apply_edit(terrain, Edit(1, 2, "smooth", 2, 2, 5, 5, 0))
    assert not np.array_equal(before_smooth, terrain.heights)


def test_out_of_bounds_edit_raises_e_bounds(task):
    bad = Edit(1, 0, "raise", 40, 30, 60, 40, 2)
    with pytest.raises(engine_mod.errors.EngineError) as info:
        edit_mod.validate_edit_bounds(bad, task.terrain)
    assert info.value.code == engine_mod.errors.E_BOUNDS


def test_incremental_render_matches_from_scratch(task, make_engine,
                                                 workspace, edit_list):
    pristine = copy.deepcopy(task.terrain)
    engine_a = make_engine(task, workspace)
    engine_a.render_all()
    baseline_gray, baseline_depth = full_render_arrays(engine_a)

    # Incremental path: the engine mutates the shared height field in place.
    report = make_engine(task, workspace).incremental(
        task.terrain, edit_list)
    inc_gray, inc_depth = full_render_arrays(
        engine_mod.Engine(task, workspace))

    # From-scratch path rendered from the same final edited height field.
    scratch = workspace / "scratch"
    scratch.mkdir()
    fresh_task = copy.deepcopy(task)
    fresh_task = replace(
        fresh_task,
        terrain=replace(fresh_task.terrain,
                        heights=task.terrain.heights.copy()))
    fresh_engine = engine_mod.Engine(fresh_task, scratch)
    fresh_engine.render_all()
    fresh_gray, fresh_depth = full_render_arrays(fresh_engine)

    np.testing.assert_array_equal(inc_gray, fresh_gray)
    np.testing.assert_allclose(inc_depth, fresh_depth, rtol=1e-6)
    np.testing.assert_array_equal(baseline_gray.shape, fresh_gray.shape)

    assert report["rerender_count"] + report["reuse_count"] == len(task.tiles)
    assert report["invalidation"]
    assert all("reason" in item or "error_code" in item
               for item in report["invalidation"])


def test_only_intersecting_tiles_are_invalidated(task, make_engine,
                                                 workspace, edit_list):
    engine = make_engine(task, workspace)
    engine.render_all()
    small_edit = [Edit(1, 0, "raise", 0, 0, 2, 2, 2)]
    report = engine.incremental(task.terrain, small_edit)
    touched = set(report["rerendered_tiles"])
    all_tiles = {tile.tile_id for tile in task.tiles}
    assert touched and touched < all_tiles
    # All non-invalidated tiles remain reusable at the new version.
    for tile_id in report["reused_tiles"]:
        tile = engine.tile_by_id[tile_id]
        stored = storage.load_tile(task, workspace, tile, engine.version)
        assert stored is not None


def test_incremental_is_stable_across_batches(task, make_engine,
                                              workspace, edit_list):
    pristine = copy.deepcopy(task.terrain)
    engine = make_engine(task, workspace)
    engine.render_all()
    for batch in sorted({edit.batch for edit in edit_list}):
        batch_edits = [edit for edit in edit_list if edit.batch == batch]
        engine.incremental(task.terrain, batch_edits)
    staged_gray, staged_depth = full_render_arrays(engine)

    scratch = workspace / "scratch2"
    scratch.mkdir()
    fresh = engine_mod.Engine(copy.deepcopy(task), scratch)
    fresh.task = replace(fresh.task,
                         terrain=replace(fresh.task.terrain,
                                         heights=task.terrain.heights.copy()))
    fresh.render_all()
    fresh_gray, fresh_depth = full_render_arrays(fresh)
    np.testing.assert_array_equal(staged_gray, fresh_gray)
    np.testing.assert_allclose(staged_depth, fresh_depth, rtol=1e-6)

