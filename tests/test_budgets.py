"""Face, texture, memory and registration budget arbitration."""
from __future__ import annotations

import pytest

from scene_pipeline.errors import (
    E_FACE_BUDGET,
    E_MEMORY_BUDGET,
    E_REG_BUDGET,
    FaceBudgetError,
    TextureConsistencyError,
)
from scene_pipeline.geometry import process_geometry
from scene_pipeline.manifest import SceneManifest
from scene_pipeline.memory import SceneResourceRequest, arbitrate
from scene_pipeline.registration import plan_registration_budget, register_scene
from scene_pipeline.texture import process_texture


def test_face_count_never_exceeds_budget(bundle_factory, manifest, config):
    bundle = bundle_factory(manifest)
    registration = register_scene(
        bundle, config.icp, config.memory.max_registration_comparisons
    )
    for budget in (8, 16, 32, 64):
        result = process_geometry(
            registration, manifest.geometry_mode, budget,
            config.geometry.grid_resolution,
        )
        assert result.post_simplify_face_count <= budget
        assert result.overage == 0


def test_face_budget_error_carries_scene_and_overage(bundle_factory,
                                                     manifest, config):
    bundle = bundle_factory(manifest)
    registration = register_scene(
        bundle, config.icp, config.memory.max_registration_comparisons
    )
    with pytest.raises(FaceBudgetError) as exc_info:
        process_geometry(
            registration, manifest.geometry_mode, 2,
            config.geometry.grid_resolution,
        )
    assert exc_info.value.code == E_FACE_BUDGET


def test_texture_consistency_error_reports_camera_and_deviation(
        bundle_factory, manifest, config):
    bundle = bundle_factory(manifest)
    registration = register_scene(
        bundle, config.icp, config.memory.max_registration_comparisons
    )
    geometry = process_geometry(
        registration, manifest.geometry_mode,
        manifest.target_face_budget, config.geometry.grid_resolution,
    )
    with pytest.raises(TextureConsistencyError) as exc_info:
        process_texture(
            geometry, registration, bundle,
            config.texture.resolution, tolerance=0.0,
        )
    assert exc_info.value.code == "E_TEX_CONSISTENCY"
    assert exc_info.value.details["camera_id"]
    assert exc_info.value.details["excess"] >= 0.0


def test_registration_budget_downgrades_comparisons():
    candidate_limit, iterations, downgraded = plan_registration_budget(
        camera_count=8, point_count=240, max_iterations=12,
        comparison_budget=5000,
    )
    assert downgraded is True
    assert 8 * candidate_limit * (iterations + 1) <= 5000


def test_memory_arbiter_downgrades_then_rejects():
    request = SceneResourceRequest(
        scene_id="scene_x", priority=5, face_budget=512,
        vertex_estimate=64, texture_resolution=64,
        camera_count=8, point_count=240, icp_iterations=12,
    )
    decision = arbitrate(
        request, memory_budget=4000, comparison_budget=10_000_000,
        downgrade_order=("texture_resolution", "face_budget", "icp_iterations"),
    )
    assert decision.admitted is True
    assert decision.downgrade_categories
    assert decision.total_units <= 4000


def test_memory_arbiter_rejects_when_even_minimum_exceeds_budget():
    request = SceneResourceRequest(
        scene_id="scene_x", priority=5, face_budget=4,
        vertex_estimate=512, texture_resolution=4,
        camera_count=8, point_count=240, icp_iterations=2,
    )
    decision = arbitrate(
        request, memory_budget=10, comparison_budget=10_000_000,
        downgrade_order=("texture_resolution", "face_budget", "icp_iterations"),
    )
    assert decision.admitted is False
    assert decision.rejection_code in {E_MEMORY_BUDGET, E_REG_BUDGET}


def test_registration_budget_rejection_path():
    request = SceneResourceRequest(
        scene_id="scene_x", priority=5, face_budget=16,
        vertex_estimate=32, texture_resolution=8,
        camera_count=16, point_count=240, icp_iterations=12,
    )
    decision = arbitrate(
        request, memory_budget=10_000_000, comparison_budget=100,
        downgrade_order=("texture_resolution", "face_budget", "icp_iterations"),
    )
    assert decision.admitted is False
    assert decision.rejection_code == E_REG_BUDGET
