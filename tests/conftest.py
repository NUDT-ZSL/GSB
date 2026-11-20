from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scene_pipeline.config import PipelineConfig
from scene_pipeline.manifest import SceneManifest, write_manifest
from scene_pipeline.observations import generate_scene_bundle, load_observation_bundle
from scene_pipeline.synth import build_camera_specs


TEST_POINT_COUNT = 64


def _make_manifest(index: int, scene_id: str | None = None,
                   revision: int = 1, camera_count: int = 3,
                   geometry_mode: str = "solid",
                   texture_seed: int | None = None) -> SceneManifest:
    point_seed = 1000 + index * 17
    cameras, _ = build_camera_specs(point_seed, camera_count)
    return SceneManifest(
        scene_id=scene_id or f"scene_{index:03d}",
        scene_revision=revision,
        camera_poses=cameras,
        point_cloud_seed=point_seed,
        texture_seed=texture_seed if texture_seed is not None else 5000 + index * 31,
        geometry_mode=geometry_mode,
        target_face_budget=64 if index % 2 else 96,
        priority=(index * 7) % 10,
    )


@pytest.fixture
def config():
    return PipelineConfig()


@pytest.fixture
def small_config():
    config = PipelineConfig()
    config.workers = 1
    config.scenes_per_batch = 2
    config.icp.max_iterations = 6
    config.geometry.grid_resolution = 4
    config.texture.resolution = 8
    config.memory.resident_budget_units = 30000
    config.texture.tolerance = 0.5
    return config


@pytest.fixture
def manifest():
    return _make_manifest(0)


@pytest.fixture
def bundle_factory(tmp_path):
    def _build(manifest: SceneManifest):
        observation_dir = tmp_path / "observations"
        generate_scene_bundle(
            manifest, str(observation_dir), point_count=TEST_POINT_COUNT
        )
        return load_observation_bundle(manifest, str(observation_dir))

    return _build


@pytest.fixture
def manifest_writer(tmp_path):
    def _write(manifests, name="manifest.jsonl"):
        path = tmp_path / name
        write_manifest(str(path), manifests)
        return str(path)

    return _write


@pytest.fixture
def paths(tmp_path):
    return {
        "observations": str(tmp_path / "observations"),
        "snapshots": str(tmp_path / "snapshots"),
        "report": str(tmp_path / "report.json"),
        "state": str(tmp_path / "state.json"),
    }
