from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from livingroom_stereo.errors import ProjectValidationError
from livingroom_stereo.generator import build_project
from livingroom_stereo.loader import load_project, validate_project_data


class LoaderTests(unittest.TestCase):
    def test_generated_sample_is_valid(self) -> None:
        issues, project = validate_project_data(build_project())
        self.assertEqual(issues, [])
        self.assertIsNotNone(project)

    def test_validation_errors_are_path_addressed_and_stable(self) -> None:
        data = build_project()
        data["stereo"]["interocular_distance_m"] = 0.5
        data["screen"]["blocks"]["cols"] = 7
        data["furniture"][2]["bounds_m"]["min"][0] = 99.0
        furniture_id = data["viewing_poses"][0]["id"]
        original_furniture_id = data["viewing_poses"][0]["furniture_id"]
        data["viewing_poses"][0]["furniture_id"] = "missing-furniture"
        data["relationships"][0]["to"] = "missing-target"
        data["acoustic_samples"][0]["viewing_pose_id"] = "missing-pose"
        data["acoustic_samples"][1]["position_m"][0] = -5.0

        first_issues, first_project = validate_project_data(
            copy.deepcopy(data)
        )
        second_issues, second_project = validate_project_data(
            copy.deepcopy(data)
        )
        self.assertIsNone(first_project)
        self.assertIsNone(second_project)
        self.assertEqual(first_issues, second_issues)
        self.assertEqual(first_issues, sorted(first_issues))
        serialised = [issue.as_dict() for issue in first_issues]
        self.assertEqual(
            serialised,
            sorted(
                serialised,
                key=lambda item: (
                    item["path"],
                    item["code"],
                    item["message"],
                ),
            ),
        )
        paths_and_codes = {
            (issue.path, issue.code) for issue in first_issues
        }
        self.assertIn(
            ("$.stereo.interocular_distance_m", "E_RANGE"),
            paths_and_codes,
        )
        self.assertIn(
            ("$.screen.blocks.cols", "E_GRID_RESOLUTION_MISMATCH"),
            paths_and_codes,
        )
        self.assertIn(
            ("$.furniture[2].bounds_m", "E_BOUNDS_ORDER"),
            paths_and_codes,
        )
        pose_index = next(
            index
            for index, item in enumerate(data["viewing_poses"])
            if item["id"] == furniture_id
        )
        self.assertIn(
            (
                f"$.viewing_poses[{pose_index}].furniture_id",
                "E_REFERENCE",
            ),
            paths_and_codes,
        )
        self.assertIn(
            ("$.relationships[0].to", "E_REFERENCE"),
            paths_and_codes,
        )
        self.assertIn(
            ("$.acoustic_samples[0].viewing_pose_id", "E_REFERENCE"),
            paths_and_codes,
        )
        self.assertIn(
            ("$.acoustic_samples[1].position_m", "E_POSITION_BOUNDS"),
            paths_and_codes,
        )
        del original_furniture_id

    def test_missing_required_top_level_field(self) -> None:
        data = build_project()
        del data["stereo"]
        issues, project = validate_project_data(data)
        self.assertIsNone(project)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].path, "$.stereo")
        self.assertEqual(issues[0].code, "E_REQUIRED")

    def test_invalid_collection_type_does_not_crash(self) -> None:
        data = build_project()
        data["viewing_poses"] = None
        issues, project = validate_project_data(data)
        self.assertIsNone(project)
        self.assertTrue(
            any(
                issue.path == "$.viewing_poses"
                and issue.code == "E_TYPE"
                for issue in issues
            )
        )

    def test_load_project_wraps_io_and_json_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaises(ProjectValidationError) as missing_context:
                load_project(missing)
            self.assertEqual(
                missing_context.exception.issues[0].code, "E_IO"
            )

            invalid = Path(directory) / "invalid.json"
            invalid.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ProjectValidationError) as json_context:
                load_project(invalid)
            self.assertEqual(
                json_context.exception.issues[0].code, "E_JSON_PARSE"
            )

    def test_generated_project_is_byte_stable(self) -> None:
        first = json.dumps(
            build_project(seed=99, scenario="base"),
            sort_keys=True,
            separators=(",", ":"),
        )
        second = json.dumps(
            build_project(seed=99, scenario="base"),
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
