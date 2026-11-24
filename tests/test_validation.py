from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from pointcloud_workbench.errors import BusinessError
from pointcloud_workbench.spec import load_task

from tests.helpers import SampleTest


class ValidationTests(SampleTest):
    def assert_rejected(
        self,
        task_path: Path,
        code: str,
        *,
        state_name: str = "state_rejected",
        output_name: str = "out_rejected",
    ) -> BusinessError:
        with self.assertRaises(BusinessError) as caught:
            self.render(task_path, state_name=state_name, output_name=output_name)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse((self.root / output_name).exists())
        return caught.exception

    def test_malformed_json(self) -> None:
        path = self.samples / "bad_json.json"
        path.write_text('{"schema_version": 1,', encoding="utf-8")
        self.assert_rejected(path, "E_JSON")

    def test_invalid_utf8_json(self) -> None:
        path = self.samples / "bad_utf8.json"
        path.write_bytes(b"\xff\xfe\x00")
        self.assert_rejected(path, "E_JSON")

    def test_non_finite_coordinate(self) -> None:
        task = self.samples / "task_base.json"
        self.modify_point_record(task, 7, 0, math.nan)
        self.assert_rejected(task, "E_POINT_DATA")

    def test_grayscale_out_of_range(self) -> None:
        task = self.samples / "task_base.json"
        self.modify_point_record(task, 11, 3, 1.25)
        self.assert_rejected(task, "E_POINT_DATA")

    def test_zero_normal(self) -> None:
        task = self.samples / "task_base.json"
        for offset in (4, 5, 6):
            self.modify_point_record(task, 13, offset, 0.0)
        self.assert_rejected(task, "E_POINT_DATA")

    def test_tiny_but_nonzero_normal_is_accepted_and_normalized(self) -> None:
        task = self.samples / "task_base.json"
        self.modify_point_record(task, 13, 4, 1e-20)
        self.modify_point_record(task, 13, 5, 0.0)
        self.modify_point_record(task, 13, 6, 0.0)
        loaded = load_task(task)
        np.testing.assert_allclose(loaded.pointcloud.normals[13], [1.0, 0.0, 0.0])

    def test_truncated_pointcloud(self) -> None:
        task = self.samples / "task_base.json"
        cloud = self.samples / "points_base.bin"
        cloud.write_bytes(cloud.read_bytes()[:-5])
        self.assert_rejected(task, "E_POINTCLOUD_FORMAT")

    def test_bad_world_scale(self) -> None:
        def mutate(document: dict) -> None:
            document["pointcloud"]["world_scale"] = 0.0

        task = self.clone_task("task_base.json", "task_bad_scale.json", mutate=mutate)
        self.assert_rejected(task, "E_WORLD")

    def test_bad_origin_offset(self) -> None:
        def mutate(document: dict) -> None:
            document["pointcloud"]["origin_offset"][1] = float("inf")

        task = self.clone_task("task_base.json", "task_bad_origin.json", mutate=mutate)
        self.assert_rejected(task, "E_WORLD")

    def test_fov_upper_bound_is_exclusive(self) -> None:
        def mutate(document: dict) -> None:
            document["cameras"][0]["vertical_fov_deg"] = 180.0

        task = self.clone_task("task_base.json", "task_bad_fov.json", mutate=mutate)
        self.assert_rejected(task, "E_FOV")

    def test_aspect_must_be_positive(self) -> None:
        def mutate(document: dict) -> None:
            document["cameras"][0]["aspect_ratio"] = 0.0

        task = self.clone_task("task_base.json", "task_bad_aspect.json", mutate=mutate)
        self.assert_rejected(task, "E_ASPECT")

    def test_supersample_must_be_positive_finite(self) -> None:
        for value, name in [(0.0, "zero"), (float("nan"), "nan")]:
            def mutate(document: dict, value=value) -> None:
                document["render"]["supersample"] = value

            task = self.clone_task(
                "task_base.json",
                f"task_bad_ss_{name}.json",
                mutate=mutate,
            )
            self.assert_rejected(task, "E_SUPERSAMPLE", output_name=f"out_ss_{name}")

    def test_overflowing_supersample_grid_is_rejected(self) -> None:
        def mutate(document: dict) -> None:
            document["render"]["supersample"] = 1e308

        task = self.clone_task("task_base.json", "task_ss_overflow.json", mutate=mutate)
        self.assert_rejected(task, "E_SUPERSAMPLE", output_name="out_ss_overflow")

    def test_oversized_integer_numeric_field_is_rejected(self) -> None:
        def mutate(document: dict) -> None:
            document["pointcloud"]["world_scale"] = 10**400

        task = self.clone_task("task_base.json", "task_world_overflow.json", mutate=mutate)
        self.assert_rejected(task, "E_WORLD", output_name="out_world_overflow")

    def test_fractional_supersample_is_accepted(self) -> None:
        def mutate(document: dict) -> None:
            document["render"]["supersample"] = 1.5

        task = self.clone_task("task_base.json", "task_fractional.json", mutate=mutate)
        report = self.render(
            task,
            state_name="state_fractional",
            output_name="out_fractional",
            verify_full=True,
        )
        self.assertTrue(report["ok"])
        self.assertTrue(
            all(
                camera["full_recompute_verification"]["pixel_identical"]
                for camera in report["cameras"]
            )
        )

    def test_block_size_must_be_positive(self) -> None:
        def mutate(document: dict) -> None:
            document["render"]["blocks"]["width"] = 0

        task = self.clone_task("task_base.json", "task_bad_blocks.json", mutate=mutate)
        self.assert_rejected(task, "E_BLOCKS")

    def test_memory_budget_does_not_reduce_supersampling(self) -> None:
        def mutate(document: dict) -> None:
            document["render"]["memory_budget_bytes"] = 100

        task = self.clone_task("task_base.json", "task_bad_budget.json", mutate=mutate)
        error = self.assert_rejected(task, "E_BUDGET")
        self.assertIn("largest_block_sample_bytes", error.details)

    def test_up_parallel_to_view_direction(self) -> None:
        def mutate(document: dict) -> None:
            camera = document["cameras"][0]
            camera["position"] = [0.0, 0.0, 3.0]
            camera["target"] = [0.0, 0.0, 0.0]
            camera["up"] = [0.0, 0.0, 1.0]

        task = self.clone_task("task_base.json", "task_parallel.json", mutate=mutate)
        self.assert_rejected(task, "E_CAMERA")

    def test_missing_pointcloud(self) -> None:
        def mutate(document: dict) -> None:
            document["pointcloud"]["path"] = "missing.bin"

        task = self.clone_task("task_base.json", "task_missing_cloud.json", mutate=mutate)
        self.assert_rejected(task, "E_POINTCLOUD_IO")

    def test_edit_old_digest_mismatch(self) -> None:
        document = self.task_document("task_edited.json")
        edit_path = self.samples / document["edit_manifest"]
        edit = json.loads(edit_path.read_text(encoding="utf-8"))
        edit["regions"][0]["old_digest"] = "0" * 64
        edit_path.write_text(json.dumps(edit), encoding="utf-8")
        self.assert_rejected(self.samples / "task_edited.json", "E_EDIT")

    def test_task_root_must_be_object(self) -> None:
        path = self.samples / "task_list.json"
        path.write_text("[]", encoding="utf-8")
        self.assert_rejected(path, "E_SCHEMA")

