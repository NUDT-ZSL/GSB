from __future__ import annotations

import copy
import math
import unittest

from livingroom_stereo.evaluator import evaluate_project
from livingroom_stereo.loader import validate_project_data
from livingroom_stereo.optics import (
    cast_scene_hit,
    compute_stereo_projection,
    screen_sample_point,
)


def simple_project(*, include_near_table: bool) -> dict:
    furniture = [
        {
            "id": "viewer_marker",
            "kind": "seat",
            "bounds_m": {
                "min": [1.95, 0.0, 1.45],
                "max": [2.05, 0.05, 1.55],
            },
        }
    ]
    if include_near_table:
        furniture.append(
            {
                "id": "near_table",
                "kind": "table",
                "bounds_m": {
                    "min": [1.99, 2.99, 1.49],
                    "max": [2.01, 3.01, 1.51],
                },
            }
        )
    return {
        "schema_version": 1,
        "project_id": "optics-unit-test",
        "seed": 7,
        "room": {"width_m": 4.0, "depth_m": 6.0, "height_m": 3.0},
        "screen": {
            "id": "screen.test",
            "center_m": [2.0, 4.0, 1.5],
            "width_m": 2.0,
            "height_m": 1.0,
            "resolution": {"width_px": 1000, "height_px": 500},
            "blocks": {"cols": 1, "rows": 1},
            "samples_per_block": {"cols": 1, "rows": 1},
            "right": [1.0, 0.0, 0.0],
            "up": [0.0, 0.0, 1.0],
            "normal": [0.0, -1.0, 0.0],
            "design_viewing_distance_m": 4.0,
            "zero_parallax_distance_m": 5.0,
        },
        "stereo": {"interocular_distance_m": 0.06},
        "assessment": {
            "comfort_parallax_px": {"min": -3.0, "max": 3.0},
            "depth_distortion_threshold": 0.5,
            "score_weights": {
                "overflow_ratio": 0.65,
                "overflow_magnitude": 0.35,
            },
            "min_comfortable_block_ratio": 0.0,
            "viewing_zone_radius_m": 1.0,
        },
        "viewing_poses": [
            {
                "id": "seat.test",
                "position_m": [2.0, 0.0, 1.5],
                "forward": [0.0, 1.0, 0.0],
                "up": [0.0, 0.0, 1.0],
                "furniture_id": "viewer_marker",
            }
        ],
        "furniture": furniture,
        "relationships": [],
        "acoustic_samples": [
            {
                "id": "A01",
                "position_m": [2.0, 0.0],
                "coverage_radius_m": 0.5,
                "viewing_pose_id": "seat.test",
            }
        ],
    }


class OpticsTests(unittest.TestCase):
    def test_fronto_parallel_formula_and_positive_divergent_sign(self) -> None:
        issues, project = validate_project_data(
            simple_project(include_near_table=False)
        )
        self.assertEqual(issues, [])
        assert project is not None
        pose = project.viewing_poses[0]
        target = screen_sample_point(project.screen, 0, 0)
        hit = cast_scene_hit(project, pose, target)
        projection = compute_stereo_projection(project, pose, hit)

        baseline = 0.06
        screen_distance = 4.0
        zero_distance = 5.0
        nominal_depth = 6.0
        expected_parallax_m = baseline * screen_distance * (
            1.0 / zero_distance - 1.0 / nominal_depth
        )
        pixel_pitch = 2.0 / 1000.0
        expected_px = expected_parallax_m / pixel_pitch

        self.assertAlmostEqual(hit.object_id, "room")
        self.assertAlmostEqual(projection.nominal_depth_m, 6.0)
        self.assertAlmostEqual(
            projection.raw_parallax_px, expected_px, places=6
        )
        self.assertGreater(projection.raw_parallax_px, 0.0)
        self.assertEqual(projection.display_parallax_px, 4.0)
        self.assertAlmostEqual(
            projection.reconstructed_depth_m, nominal_depth, places=6
        )

    def test_crossed_sign_and_reconstruction_for_near_scene(self) -> None:
        issues, project = validate_project_data(
            simple_project(include_near_table=True)
        )
        self.assertEqual(issues, [])
        assert project is not None
        pose = project.viewing_poses[0]
        target = screen_sample_point(project.screen, 0, 0)
        hit = cast_scene_hit(project, pose, target)
        projection = compute_stereo_projection(project, pose, hit)

        self.assertEqual(hit.object_id, "near_table")
        self.assertAlmostEqual(hit.distance_m, 2.99)
        self.assertAlmostEqual(projection.nominal_depth_m, 2.99)
        expected_px = (
            0.06
            * 4.0
            * (
                1.0 / 5.0
                - 1.0 / projection.nominal_depth_m
            )
            / (2.0 / 1000.0)
        )
        self.assertAlmostEqual(
            projection.raw_parallax_px, expected_px, places=6
        )
        self.assertLess(projection.raw_parallax_px, 0.0)
        self.assertEqual(projection.display_parallax_px, -16.0)
        self.assertAlmostEqual(
            projection.reconstructed_depth_m, 2.99, places=6
        )

    def test_threshold_mask_and_primary_cause_use_overlimit_direction(self) -> None:
        issues, project = validate_project_data(
            simple_project(include_near_table=True)
        )
        self.assertEqual(issues, [])
        assert project is not None
        evaluation = evaluate_project(project)
        block = evaluation["poses"][0]["blocks"][0]
        self.assertLess(block["parallax_statistics"]["mean_px"], -3.0)
        self.assertEqual(block["overlimit_mask"], ["1"])
        self.assertEqual(block["parallax_overlimit_mask"], ["1"])
        self.assertEqual(block["primary_cause"], "convergence_overflow")
        self.assertTrue(block["overlimit"])
        self.assertIn("parallax_convergence", block["limit_causes"])
        self.assertLess(block["comfort_score"], 100.0)

    def test_exact_math_has_expected_quantisation(self) -> None:
        data = simple_project(include_near_table=False)
        data["assessment"]["comfort_parallax_px"] = {
            "min": -100.0,
            "max": 100.0,
        }
        issues, project = validate_project_data(data)
        self.assertEqual(issues, [])
        assert project is not None
        pose = project.viewing_poses[0]
        projection = compute_stereo_projection(
            project,
            pose,
            cast_scene_hit(
                project,
                pose,
                screen_sample_point(project.screen, 0, 0),
            ),
        )
        self.assertTrue(math.isfinite(projection.raw_parallax_px))
        self.assertAlmostEqual(
            projection.display_parallax_px,
            round(projection.raw_parallax_px),
        )

    def test_scene_hits_are_deterministic(self) -> None:
        data = simple_project(include_near_table=True)
        _issues, project = validate_project_data(copy.deepcopy(data))
        assert project is not None
        pose = project.viewing_poses[0]
        target = screen_sample_point(project.screen, 0, 0)
        first = cast_scene_hit(project, pose, target)
        second = cast_scene_hit(project, pose, target)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
