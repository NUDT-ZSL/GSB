from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from livingroom_stereo.evaluator import evaluate_project
from livingroom_stereo.generator import build_project
from livingroom_stereo.incremental import (
    compare_evaluations,
    incremental_evaluate,
    load_state,
    write_state,
)
from livingroom_stereo.loader import validate_project_data


class IncrementalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        base_issues, cls.base_project = validate_project_data(
            build_project(scenario="base")
        )
        changed_issues, cls.changed_project = validate_project_data(
            build_project(scenario="adjusted")
        )
        assert cls.base_project is not None
        assert cls.changed_project is not None
        assert not base_issues
        assert not changed_issues
        cls.base_evaluation = evaluate_project(cls.base_project)
        cls.result = incremental_evaluate(
            cls.base_project,
            cls.changed_project,
            baseline_evaluation=cls.base_evaluation,
            verify_full=True,
        )

    def test_incremental_and_full_results_are_identical(self) -> None:
        consistency = self.result["consistency"]
        self.assertTrue(consistency["consistent"])
        self.assertIsNone(consistency["first_divergence"])
        self.assertEqual(consistency["mismatch_count"], 0)
        affected = self.result["affected"]
        self.assertFalse(affected["full_recompute"])
        self.assertGreater(affected["recomputed_block_count"], 0)
        self.assertGreater(affected["reused_block_count"], 0)
        self.assertLess(
            affected["recomputed_block_count"],
            affected["total_block_count"],
        )

    def test_unaffected_blocks_are_copied_verbatim(self) -> None:
        affected_sets: dict[str, set[tuple[int, int]]] = {}
        for pose in self.result["affected"]["poses"]:
            blocks: set[tuple[int, int]] = set()
            for block_range in pose["block_ranges"]:
                for row in [block_range["row"]]:
                    for col in range(
                        block_range["col_start"],
                        block_range["col_end_exclusive"],
                    ):
                        blocks.add((row, col))
            affected_sets[pose["pose_id"]] = blocks
        base_poses = {
            pose["pose_id"]: pose
            for pose in self.base_evaluation["poses"]
        }
        for pose in self.result["evaluation"]["poses"]:
            affected = affected_sets.get(pose["pose_id"], set())
            base_blocks = {
                (block["row"], block["col"]): block
                for block in base_poses[pose["pose_id"]]["blocks"]
            }
            for block in pose["blocks"]:
                index = (block["row"], block["col"])
                if index not in affected:
                    self.assertEqual(block, base_blocks[index])

    def test_repeated_incremental_path_is_deterministic(self) -> None:
        repeated = incremental_evaluate(
            self.base_project,
            self.changed_project,
            baseline_evaluation=self.base_evaluation,
            verify_full=False,
        )
        self.assertEqual(
            repeated["evaluation"], self.result["evaluation"]
        )
        self.assertEqual(
            repeated["affected"], self.result["affected"]
        )

    def test_acoustic_only_change_reuses_every_block(self) -> None:
        data = copy.deepcopy(self.base_project.raw)
        data["acoustic_samples"][4]["position_m"] = [2.65, 4.25]
        issues, changed = validate_project_data(data)
        self.assertEqual(issues, [])
        assert changed is not None
        result = incremental_evaluate(
            self.base_project,
            changed,
            baseline_evaluation=self.base_evaluation,
            verify_full=True,
        )
        self.assertTrue(result["consistency"]["consistent"])
        self.assertEqual(result["affected"]["recomputed_block_count"], 0)
        self.assertEqual(
            result["affected"]["reused_block_count"],
            result["affected"]["total_block_count"],
        )

    def test_state_reload_has_same_incremental_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "base.state.json"
            write_state(path, self.base_project, self.base_evaluation)
            loaded_project, loaded_evaluation = load_state(path)
            self.assertEqual(
                loaded_project.fingerprint,
                self.base_project.fingerprint,
            )
            result = incremental_evaluate(
                loaded_project,
                self.changed_project,
                baseline_evaluation=loaded_evaluation,
                verify_full=True,
            )
            self.assertTrue(result["consistency"]["consistent"])
            self.assertEqual(
                result["evaluation"], self.result["evaluation"]
            )

    def test_screen_change_forces_all_blocks(self) -> None:
        data = copy.deepcopy(self.base_project.raw)
        data["screen"]["center_m"][0] += 0.01
        issues, changed = validate_project_data(data)
        self.assertEqual(issues, [])
        assert changed is not None
        result = incremental_evaluate(
            self.base_project,
            changed,
            baseline_evaluation=self.base_evaluation,
            verify_full=True,
        )
        self.assertTrue(result["affected"]["full_recompute"])
        self.assertIn(
            "screen_parameters_changed",
            result["affected"]["force_full_reasons"],
        )
        self.assertEqual(result["affected"]["reused_block_count"], 0)
        self.assertTrue(result["consistency"]["consistent"])

    def test_first_divergence_location_is_explicit(self) -> None:
        corrupt = copy.deepcopy(self.result["evaluation"])
        first_pose = sorted(
            corrupt["poses"], key=lambda item: item["pose_id"]
        )[0]
        first_block = sorted(
            first_pose["blocks"],
            key=lambda item: (item["row"], item["col"]),
        )[0]
        first_block["comfort_score"] = 12.345
        full = evaluate_project(self.changed_project)
        comparison = compare_evaluations(corrupt, full)
        self.assertFalse(comparison["consistent"])
        divergence = comparison["first_divergence"]
        self.assertEqual(divergence["pose_id"], first_pose["pose_id"])
        self.assertEqual(divergence["block_row"], first_block["row"])
        self.assertEqual(divergence["block_col"], first_block["col"])
        self.assertEqual(divergence["field"], "comfort_score")


if __name__ == "__main__":
    unittest.main()
