"""Byte-level reproducibility across runs, processes and ordering changes."""
from __future__ import annotations

import numpy as np

from scene_pipeline.geometry import process_geometry
from scene_pipeline.hashing import checksum_numpy
from scene_pipeline.registration import register_scene
from scene_pipeline.texture import process_texture


def _run_all(bundle, manifest, config):
    registration = register_scene(
        bundle, config.icp, config.memory.max_registration_comparisons
    )
    geometry = process_geometry(
        registration, manifest.geometry_mode,
        manifest.target_face_budget, config.geometry.grid_resolution,
    )
    texture = process_texture(
        geometry, registration, bundle,
        config.texture.resolution, config.texture.tolerance,
    )
    return registration, geometry, texture


def test_identical_inputs_produce_identical_bytes(bundle_factory, manifest, config):
    bundle_a = bundle_factory(manifest)
    bundle_b = bundle_factory(manifest)
    reg_a, geo_a, tex_a = _run_all(bundle_a, manifest, config)
    reg_b, geo_b, tex_b = _run_all(bundle_b, manifest, config)

    assert checksum_numpy(reg_a.points) == checksum_numpy(reg_b.points)
    for camera_a, camera_b in zip(reg_a.cameras, reg_b.cameras):
        assert checksum_numpy(camera_a.transform) == checksum_numpy(camera_b.transform)
        assert camera_a.residual == camera_b.residual
        assert camera_a.comparisons == camera_b.comparisons
    assert checksum_numpy(geo_a.mesh.vertices) == checksum_numpy(geo_b.mesh.vertices)
    assert checksum_numpy(geo_a.mesh.faces) == checksum_numpy(geo_b.mesh.faces)
    assert checksum_numpy(tex_a.texture) == checksum_numpy(tex_b.texture)


def test_nearest_neighbour_tie_breaks_by_index():
    from scene_pipeline.registration import _projection_correspondences

    # Symmetric observations produce equal-distance candidates; argmin must
    # always select the lowest index deterministically.
    rng = np.random.default_rng(0)
    points = rng.random((12, 3))
    depth = np.zeros((8, 8), dtype=np.float64)
    depth[3, 3] = 2.0
    depth[4, 4] = 2.0

    class _Camera:
        fx = fy = 5.0
        cx = cy = 3.5
        width = height = 8
        camera_id = "cam"

    extrinsic = np.eye(4)
    source_a, target_a, _ = _projection_correspondences(
        extrinsic, _Camera(), points, depth, 2, 0
    )
    source_b, target_b, _ = _projection_correspondences(
        extrinsic, _Camera(), points, depth, 2, 0
    )
    np.testing.assert_array_equal(target_a, target_b)


def test_geometry_tie_breaks_by_vertex_index(bundle_factory, manifest, config):
    bundle = bundle_factory(manifest)
    registration = register_scene(
        bundle, config.icp, config.memory.max_registration_comparisons
    )
    geo_a = process_geometry(
        registration, manifest.geometry_mode, 24, config.geometry.grid_resolution
    )
    geo_b = process_geometry(
        registration, manifest.geometry_mode, 24, config.geometry.grid_resolution
    )
    assert checksum_numpy(geo_a.mesh.faces) == checksum_numpy(geo_b.mesh.faces)
