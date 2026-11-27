from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from livingroom_stereo.evaluator import evaluate_project
from livingroom_stereo.generator import build_project
from livingroom_stereo.incremental import incremental_evaluate
from livingroom_stereo.loader import validate_project_data
from livingroom_stereo.snapshot import (
    list_snapshots,
    load_snapshot,
    load_snapshot_state,
    query_snapshot,
    save_snapshot,
    snapshot_identity,
)


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _issues, cls.base_project = validate_project_data(
            build_project(scenario="base")
        )
        _changed_issues, cls.changed_project = validate_project_data(
            build_project(scenario="adjusted")
        )
        assert cls.base_project is not None
        assert cls.changed_project is not None
        cls.base_evaluation = evaluate_project(cls.base_project)
        cls.changed_evaluation = evaluate_project(cls.changed_project)

    def test_snapshot_save_list_query_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "snapshots"
            save_snapshot(
                store,
                self.base_project,
                "v0001",
                evaluation=self.base_evaluation,
            )
            save_snapshot(
                store,
                self.changed_project,
                "v0002",
                evaluation=self.changed_evaluation,
            )
            index = list_snapshots(
                store, self.base_project.project_id
            )
            self.assertEqual(
                [item["version"] for item in index["versions"]],
                ["v0001", "v0002"],
            )
            query = query_snapshot(
                store,
                self.base_project.project_id,
                "v0001",
                pose_ids=["seat.sofa.center"],
                row_range=(1, 2),
                col_range=(3, 4),
            )
            self.assertEqual(len(query["poses"]), 1)
            self.assertTrue(query["blocks"])
            for block in query["blocks"]:
                self.assertGreaterEqual(block["row"], 1)
                self.assertLessEqual(block["row"], 2)
                self.assertGreaterEqual(block["col"], 3)
                self.assertLessEqual(block["col"], 4)
                self.assertIn("comfort_score", block)
                self.assertIn("overlimit_mask", block)
            self.assertIn("mismatches", query["alignment"])

            loaded_project, loaded_evaluation = load_snapshot_state(
                store,
                self.base_project.project_id,
                "v0001",
            )
            self.assertEqual(
                loaded_project.fingerprint,
                self.base_project.fingerprint,
            )
            self.assertEqual(loaded_evaluation, self.base_evaluation)

    def test_snapshot_reload_incremental_matches_direct_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "snapshots"
            save_snapshot(
                store,
                self.base_project,
                "v0001",
                evaluation=self.base_evaluation,
            )
            snapshot_project, snapshot_evaluation = load_snapshot_state(
                store,
                self.base_project.project_id,
                "v0001",
            )
            self.assertEqual(
                snapshot_project.fingerprint,
                self.base_project.fingerprint,
            )
            from_snapshot = incremental_evaluate(
                self.base_project,
                self.changed_project,
                baseline_evaluation=snapshot_evaluation,
                verify_full=True,
            )
            direct = incremental_evaluate(
                self.base_project,
                self.changed_project,
                baseline_evaluation=self.base_evaluation,
                verify_full=True,
            )
            none_reloaded = incremental_evaluate(
                self.base_project,
                self.changed_project,
                verify_full=False,
            )
            self.assertEqual(
                from_snapshot["evaluation"], direct["evaluation"]
            )
            self.assertEqual(
                from_snapshot["evaluation"],
                none_reloaded["evaluation"],
            )
            self.assertTrue(
                from_snapshot["consistency"]["consistent"]
            )

    def test_snapshot_generation_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as first_directory:
            with tempfile.TemporaryDirectory() as second_directory:
                first_store = Path(first_directory) / "store"
                second_store = Path(second_directory) / "store"
                save_snapshot(
                    first_store,
                    self.base_project,
                    "v0001",
                    evaluation=self.base_evaluation,
                )
                save_snapshot(
                    second_store,
                    self.base_project,
                    "v0001",
                    evaluation=self.base_evaluation,
                )
                first = load_snapshot(
                    first_store,
                    self.base_project.project_id,
                    "v0001",
                )
                second = load_snapshot(
                    second_store,
                    self.base_project.project_id,
                    "v0001",
                )
                self.assertEqual(
                    snapshot_identity(first),
                    snapshot_identity(second),
                )


if __name__ == "__main__":
    unittest.main()
