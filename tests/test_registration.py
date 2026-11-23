from __future__ import annotations

import unittest

import numpy as np

from sceneopt.config import PipelineConfig
from sceneopt.geometry import generate_observations, generate_world_geometry
from sceneopt.models import load_scene_manifest
from sceneopt.registration import register_point_cloud, registration_residual
from sceneopt.samples import ensure_samples


class RegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples("samples")
        cls.scene = load_scene_manifest("samples/scenes.jsonl")[0]
        cls.config = PipelineConfig()
        cls.geometry = generate_world_geometry(cls.scene, cls.config)
        cls.observation = generate_observations(
            cls.scene, cls.geometry, cls.config
        )[0]

    def test_repeatable_transform_and_independent_residual(self) -> None:
        first = register_point_cloud(
            self.geometry.points,
            self.observation.source_points_camera,
            self.observation.camera.world_from_camera,
            max_iterations=self.config.icp_max_iterations,
            tolerance=self.config.icp_tolerance,
        )
        second = register_point_cloud(
            self.geometry.points,
            self.observation.source_points_camera,
            self.observation.camera.world_from_camera,
            max_iterations=self.config.icp_max_iterations,
            tolerance=self.config.icp_tolerance,
        )
        self.assertEqual(
            first.world_from_camera.tobytes(), second.world_from_camera.tobytes()
        )
        self.assertEqual(first.residual, second.residual)
        recomputed, _distances, _nearest = registration_residual(
            self.geometry.points,
            self.observation.source_points_camera,
            first.world_from_camera,
        )
        self.assertEqual(first.residual, recomputed)

    def test_nearest_tie_breaks_by_target_index(self) -> None:
        target = np.asarray(((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)))
        source = np.asarray(((0.0, 0.0, 0.0),))
        _residual, _distances, nearest = registration_residual(
            target, source, np.eye(4)
        )
        self.assertEqual(int(nearest[0]), 0)


if __name__ == "__main__":
    unittest.main()

