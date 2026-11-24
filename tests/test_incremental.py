from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from tests.helpers import SampleTest, assert_images_equal


class IncrementalTests(SampleTest):
    def test_edit_invalidates_only_dependency_blocks_and_matches_full(self) -> None:
        base_task = self.samples / "task_base.json"
        edited_task = self.samples / "task_edited.json"
        self.render(
            base_task,
            state_name="state_shared",
            output_name="out_base",
            verify_full=True,
        )
        edited_report = self.render(
            edited_task,
            state_name="state_shared",
            output_name="out_edited",
            verify_full=True,
        )
        base_images = self.read_images("out_base")
        edited_images = self.read_images("out_edited")
        changed = False
        for camera_id in self.camera_ids():
            changed |= not np.array_equal(base_images[camera_id][0], edited_images[camera_id][0])
        self.assertTrue(changed)

        total_blocks = 24
        for camera in edited_report["cameras"]:
            self.assertEqual(camera["block_count"], total_blocks)
            self.assertGreater(camera["rendered_block_count"], 0)
            self.assertGreater(camera["reused_block_count"], 0)
            self.assertEqual(
                camera["rendered_block_count"] + camera["reused_block_count"],
                total_blocks,
            )
            self.assertGreater(camera["affected_point_count"], 0)
            self.assertTrue(camera["full_recompute_verification"]["pixel_identical"])
            self.assertTrue(camera["digest_checks"]["all_valid"])
            self.assertIn("disjoint", camera["reuse_safety"])
            invalidation_codes = {
                reason["code"]
                for block in camera["invalidated_blocks"]
                for reason in block["reasons"]
            }
            self.assertTrue(
                invalidation_codes.intersection(
                    {"EDIT_DEPENDENCY_CHANGED", "EDIT_NEW_COVERAGE"}
                )
            )

        fresh_report = self.render(
            edited_task,
            state_name="state_fresh",
            output_name="out_fresh",
            verify_full=True,
        )
        self.assertTrue(
            all(
                camera["scheduler"]["mode"] == "resume"
                for camera in fresh_report["cameras"]
            )
        )
        assert_images_equal(self, edited_images, self.read_images("out_fresh"))

    def test_second_edited_run_reuses_all_blocks(self) -> None:
        self.render(
            self.samples / "task_base.json",
            state_name="state_shared",
            output_name="out_base",
        )
        first = self.render(
            self.samples / "task_edited.json",
            state_name="state_shared",
            output_name="out_edited_first",
        )
        second = self.render(
            self.samples / "task_edited.json",
            state_name="state_shared",
            output_name="out_edited_second",
            verify_full=True,
        )
        self.assertTrue(any(camera["rendered_block_count"] > 0 for camera in first["cameras"]))
        for camera in second["cameras"]:
            self.assertEqual(camera["rendered_block_count"], 0)
            self.assertEqual(camera["reused_block_count"], 24)
            self.assertTrue(camera["full_recompute_verification"]["pixel_identical"])

    def test_unaffected_block_dependency_sets_do_not_intersect_edit(self) -> None:
        self.render(
            self.samples / "task_base.json",
            state_name="state_shared",
            output_name="out_base",
        )
        report = self.render(
            self.samples / "task_edited.json",
            state_name="state_shared",
            output_name="out_edited",
        )
        edit = json.loads((self.samples / "edits.json").read_text(encoding="utf-8"))
        affected_indices = set()
        for region in edit["regions"]:
            affected_indices.update(range(region["start"], region["start"] + region["count"]))
        for camera in report["cameras"]:
            blocks_path = Path(camera["outputs"]["blocks"])
            blocks = json.loads(blocks_path.read_text(encoding="utf-8"))["blocks"]
            for block in blocks:
                if block["status"] in {"reused", "reused_promoted"}:
                    self.assertTrue(
                        affected_indices.isdisjoint(block["dependency_indices"]),
                        (camera["camera_id"], block["block_id"]),
                    )

