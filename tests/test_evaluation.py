from __future__ import annotations

import unittest

from livingroom_stereo.evaluator import evaluate_project
from livingroom_stereo.generator import build_project
from livingroom_stereo.loader import validate_project_data


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        issues, project = validate_project_data(build_project())
        assert project is not None
        assert not issues
        cls.project = project
        cls.first = evaluate_project(project)
        cls.second = evaluate_project(project)

    def test_evaluation_is_deterministic(self) -> None:
        self.assertEqual(self.first, self.second)

    def test_block_statistics_and_masks_are_well_formed(self) -> None:
        screen = self.project.screen
        for pose in self.first["poses"]:
            self.assertEqual(
                len(pose["blocks"]),
                screen.blocks[0] * screen.blocks[1],
            )
            for block in pose["blocks"]:
                self.assertEqual(len(block["overlimit_mask"]), 3)
                self.assertTrue(
                    all(len(row) == 3 for row in block["overlimit_mask"])
                )
                self.assertEqual(
                    block["overlimit"],
                    any("1" in row for row in block["overlimit_mask"]),
                )
                self.assertGreaterEqual(block["comfort_score"], 0.0)
                self.assertLessEqual(block["comfort_score"], 100.0)
                self.assertIn(
                    block["primary_cause"],
                    {
                        "within_comfort",
                        "convergence_overflow",
                        "divergence_overflow",
                        "mixed_overflow",
                    },
                )
                self.assertLessEqual(
                    block["parallax_statistics"]["min_px"],
                    block["parallax_statistics"]["mean_px"],
                )
                self.assertLessEqual(
                    block["parallax_statistics"]["mean_px"],
                    block["parallax_statistics"]["max_px"],
                )

    def test_expected_comfortable_and_overlimit_blocks_exist(self) -> None:
        summary = self.first["summary"]
        self.assertGreater(summary["comfortable_block_count"], 0)
        self.assertGreater(summary["overlimit_block_count"], 0)
        center = next(
            pose
            for pose in self.first["poses"]
            if pose["pose_id"] == "seat.sofa.center"
        )
        self.assertEqual(center["overlimit_block_count"], 0)
        self.assertTrue(center["visual_region"]["eligible"])
        chair = next(
            pose
            for pose in self.first["poses"]
            if pose["pose_id"] == "seat.chair"
        )
        self.assertGreater(chair["overlimit_block_count"], 0)
        causes = {
            block["primary_cause"]
            for pose in self.first["poses"]
            for block in pose["blocks"]
        }
        self.assertIn("convergence_overflow", causes)

    def test_layout_has_nominal_reconstructed_and_contributors(self) -> None:
        layout = self.first["layout"]
        self.assertGreaterEqual(len(layout["relationships"]), 2)
        relation_ids = {
            item["relationship_id"]
            for item in layout["relationships"]
        }
        self.assertIn("sofa_to_tv_cabinet", relation_ids)
        self.assertIn("coffee_table_to_screen", relation_ids)
        for relation in layout["relationships"]:
            self.assertIn("nominal_distance_m", relation)
            self.assertIn("max_abs_delta_m", relation)
            self.assertTrue(relation["poses"])
            for pose in relation["poses"]:
                self.assertIn("perceptual_distance_m", pose)
                self.assertIn("delta_m", pose)
                self.assertIn("contributors", pose)
        self.assertTrue(layout["furniture_proportions"])
        for record in layout["furniture_proportions"]:
            self.assertEqual(len(record["nominal_extent_m"]), 3)
            self.assertEqual(len(record["perceptual_extent_m"]), 3)
            self.assertEqual(len(record["extent_ratio"]), 3)

    def test_alignment_reports_both_mismatch_directions(self) -> None:
        alignment = self.first["alignment"]
        self.assertGreater(alignment["mismatch_count"], 0)
        self.assertGreater(alignment["mismatch_counts"]["visual_only"], 0)
        self.assertGreater(
            alignment["mismatch_counts"]["acoustic_only"], 0
        )
        for mismatch in alignment["mismatches"]:
            self.assertIn(
                mismatch["type"],
                {"visual_only", "acoustic_only"},
            )
            self.assertGreaterEqual(mismatch["distance_m"], 0.0)
            self.assertTrue(mismatch["sample_id"])


if __name__ == "__main__":
    unittest.main()
