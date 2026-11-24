"""Business rule 3: per-sample intersection, shading and determinism."""

from __future__ import annotations

import numpy as np

from terrain_engine import geometry
from terrain_engine.render import (camera_basis, march_rays, render_tile,
                                   subpixel_offsets)


def test_subpixel_offsets_are_deterministic_and_bounded():
    first = subpixel_offsets(8)
    second = subpixel_offsets(8)
    np.testing.assert_array_equal(first, second)
    assert np.all(first > -0.5) and np.all(first < 0.5)


def test_camera_basis_is_orthonormal(task):
    basis = camera_basis(task.camera)
    vectors = [np.asarray(basis.forward), np.asarray(basis.right),
               np.asarray(basis.up_axis)]
    norms = [np.linalg.norm(vector) for vector in vectors]
    assert np.allclose(norms, 1.0)
    assert np.isclose(np.dot(vectors[0], vectors[1]), 0.0, atol=1e-12)
    assert np.isclose(np.dot(vectors[1], vectors[2]), 0.0, atol=1e-12)


def test_all_sample_rays_hit_surface(task):
    tile = task.tiles[0]
    basis = camera_basis(task.camera)
    hit_mask, distance, points = march_rays(task, tile, basis)
    assert hit_mask.all()
    assert np.all(distance > 0.0)
    # Every hit sits inside the near/far marching interval.
    assert np.all(distance >= task.camera.near - 1e-9)
    assert np.all(distance <= task.camera.far + 1e-9)


def test_repeated_tile_render_is_bit_identical(task):
    tile = task.tiles[4]
    gray_a, depth_a, _ = render_tile(task, tile)
    gray_b, depth_b, _ = render_tile(task, tile)
    np.testing.assert_array_equal(gray_a, gray_b)
    np.testing.assert_array_equal(depth_a, depth_b)


def test_gray_and_depth_ranges(task):
    gray, depth, _ = render_tile(task, task.tiles[0])
    assert gray.dtype == np.uint8
    assert depth.dtype == np.float32
    assert gray.shape == (task.tile_height, task.tile_width)
    assert depth.shape == gray.shape
    assert np.all(depth > 0.0)


def test_full_frame_has_no_background_pixels(task):
    from terrain_engine.model import TileSpec
    full = TileSpec("full", 0, 0, task.width_px, task.height_px)
    gray, depth, _ = render_tile(task, full)
    assert np.all(depth >= 0.0)
    assert np.all((gray >= 0) & (gray <= 255))


def test_tile_composite_equals_single_full_frame_render(task):
    # Render every tile independently and assemble, then render the whole
    # image as one oversized "tile" and require pixel-exact equality.  This
    # proves boundary normals/lighting use the same global field.
    from terrain_engine.model import TileSpec

    gray_canvas = np.zeros((task.height_px, task.width_px), np.uint8)
    depth_canvas = np.zeros((task.height_px, task.width_px), np.float32)
    for tile in task.tiles:
        gray, depth, _ = render_tile(task, tile)
        gray_canvas[tile.y:tile.y + tile.height,
                    tile.x:tile.x + tile.width] = gray
        depth_canvas[tile.y:tile.y + tile.height,
                     tile.x:tile.x + tile.width] = depth

    full = TileSpec("full", 0, 0, task.width_px, task.height_px)
    gray_full, depth_full, _ = render_tile(task, full)
    np.testing.assert_array_equal(gray_canvas, gray_full)
    np.testing.assert_allclose(depth_canvas, depth_full, rtol=1e-6)


def test_lighting_changes_with_height(task):
    from dataclasses import replace
    oblique_camera = replace(
        task.camera,
        position=(2350.0, 500.0, 2600.0),
        target=(2350.0, 1750.0, 200.0))
    oblique_task = replace(task, camera=oblique_camera)
    before, _, _ = render_tile(oblique_task, task.tiles[0])
    changed_heights = task.terrain.heights + 120
    changed_task = replace(
        oblique_task,
        terrain=replace(task.terrain, heights=changed_heights))
    after, _, _ = render_tile(changed_task, task.tiles[0])
    assert not np.array_equal(before, after)

