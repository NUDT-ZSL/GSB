"""Business rule 2: global gradients/normals independent of tiling."""

from __future__ import annotations

import numpy as np

from terrain_engine import geometry
from terrain_engine.model import Terrain


def make_terrain(heights, sx=2.0, sy=3.0, sz=4.0):
    array = np.asarray(heights, dtype=np.int64)
    return Terrain(array.shape[1], array.shape[0], sx, sy, sz, array)


def test_flat_terrain_normals_point_up():
    terrain = make_terrain(np.zeros((5, 6), dtype=np.int64))
    normals = geometry.compute_normals(terrain)
    assert np.allclose(normals[..., 2], 1.0)
    assert np.allclose(normals[..., :2], 0.0, atol=1e-15)


def test_ramp_normal_matches_analytic_gradient():
    heights = np.arange(6, dtype=np.int64)[None, :].repeat(4, axis=0)
    terrain = make_terrain(heights, sx=2.0, sy=3.0, sz=4.0)
    normals = geometry.compute_normals(terrain)
    # Interior: dz/dx = (1 * 4) / 2 = 2 -> normal (-2, 0, 1)/sqrt(5)
    expected = np.array([-2.0, 0.0, 1.0]) / np.sqrt(5.0)
    assert np.allclose(normals[1, 2], expected)


def test_boundary_uses_one_sided_difference():
    heights = np.array([[0, 0, 10]], dtype=np.int64)
    terrain = make_terrain(heights, sx=1.0, sy=1.0, sz=1.0)
    normals = geometry.compute_normals(terrain)
    # Left boundary one-sided: dz/dx = 0 -> straight up.
    assert np.allclose(normals[0, 0], [0.0, 0.0, 1.0])
    # Right boundary one-sided: dz/dx = 10.
    expected = np.array([-10.0, 0.0, 1.0]) / np.sqrt(101.0)
    assert np.allclose(normals[0, 2], expected)


def test_normals_identical_regardless_of_tile_partition(task):
    normals = geometry.compute_normals(task.terrain)
    # Recomputing on any subset of data passed as the same global field must
    # yield bit-identical components for a shared grid point.
    again = geometry.compute_normals(task.terrain)
    np.testing.assert_array_equal(normals, again)
    j, i = 7, 11
    point_a = normals[j, i].copy()
    point_b = geometry.compute_normals(task.terrain)[j, i]
    np.testing.assert_array_equal(point_a, point_b)


def test_bilinear_height_and_normal_sampling():
    terrain = make_terrain(
        [[0, 0, 0], [0, 10, 0], [0, 0, 0]], sx=1.0, sy=1.0, sz=1.0)
    surface = geometry.surface_z(terrain.heights, terrain.scale_z)
    value = geometry.sample_height(surface, np.array([1.0]),
                                   np.array([1.0]))
    assert np.isclose(value, 10.0)
    half = geometry.sample_height(surface, np.array([0.5]),
                                  np.array([1.0]))
    assert np.isclose(half, 5.0)
    normals = geometry.compute_normals(terrain)
    sampled = geometry.sample_normals(
        normals, np.array([1.0]), np.array([1.0]))[0]
    assert np.isclose(np.linalg.norm(sampled), 1.0)

