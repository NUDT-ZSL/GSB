from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from tests.helpers import SampleTest, assert_images_equal


class RenderTests(SampleTest):
    def test_concurrency_one_matches_four_and_full_reference(self) -> None:
        concurrent = self.clone_task(
            "task_base.json",
            "task_concurrent.json",
            mutate=lambda document: document["render"].__setitem__("max_workers", 4),
        )
        serial = self.clone_task(
            "task_base.json",
            "task_serial.json",
            mutate=lambda document: document["render"].__setitem__("max_workers", 1),
        )
        concurrent_report = self.render(
            concurrent,
            state_name="state_concurrent",
            output_name="out_concurrent",
            verify_full=True,
        )
        serial_report = self.render(
            serial,
            state_name="state_serial",
            output_name="out_serial",
            verify_full=True,
        )
        for camera in concurrent_report["cameras"]:
            self.assertTrue(camera["full_recompute_verification"]["pixel_identical"])
        for camera in serial_report["cameras"]:
            self.assertTrue(camera["full_recompute_verification"]["pixel_identical"])
        assert_images_equal(self, self.read_images("out_concurrent"), self.read_images("out_serial"))
        serial_admission = [
            wave
            for camera in serial_report["cameras"]
            for wave in camera["scheduler"]["phase_1"]["admission_sequence"]
        ]
        self.assertTrue(all(len(wave) <= 1 for wave in serial_admission))

    def test_world_scale_scaling_preserves_visibility(self) -> None:
        base_task = self.samples / "task_base.json"
        self.render(
            base_task,
            state_name="state_scale_base",
            output_name="out_scale_base",
        )
        base_images = self.read_images("out_scale_base")

        document = self.task_document("task_base.json")
        cloud_path = self.samples / document["pointcloud"]["path"]
        header = cloud_path.read_bytes()[:24]
        records = np.frombuffer(cloud_path.read_bytes()[24:], dtype="<f4").reshape((-1, 8)).copy()
        records[:, 0:3] *= 2.0
        records[:, 7] *= 2.0
        scaled_cloud = self.samples / "points_scaled.bin"
        scaled_cloud.write_bytes(header + records.astype("<f4").tobytes())
        document["pointcloud"]["path"] = scaled_cloud.name
        document["pointcloud"]["origin_offset"] = [
            float(value) * 2.0 for value in document["pointcloud"]["origin_offset"]
        ]
        for camera in document["cameras"]:
            camera["position"] = [value * 2.0 for value in camera["position"]]
            camera["target"] = [value * 2.0 for value in camera["target"]]
            camera["up"] = [value * 2.0 for value in camera["up"]]
        scaled_task = self.write_task(document, "task_scaled.json")
        scaled_report = self.render(
            scaled_task,
            state_name="state_scale_scaled",
            output_name="out_scale_scaled",
            verify_full=True,
        )
        scaled_images = self.read_images("out_scale_scaled")
        self.assertTrue(
            all(camera["full_recompute_verification"]["pixel_identical"] for camera in scaled_report["cameras"])
        )
        for camera_id in self.camera_ids():
            np.testing.assert_array_equal(
                base_images[camera_id][0],
                scaled_images[camera_id][0],
            )
            np.testing.assert_array_equal(
                scaled_images[camera_id][1],
                (base_images[camera_id][1].astype(np.float64) * 2.0).astype(np.float32),
            )

    def test_aspect_ratio_affects_projection(self) -> None:
        narrow = self.clone_task(
            "task_base.json",
            "task_aspect_narrow.json",
            mutate=lambda document: document["cameras"][0].__setitem__("aspect_ratio", 1.0),
        )
        wide = self.clone_task(
            "task_base.json",
            "task_aspect_wide.json",
            mutate=lambda document: document["cameras"][0].__setitem__("aspect_ratio", 2.0),
        )
        self.render(narrow, state_name="state_aspect_narrow", output_name="out_aspect_narrow")
        self.render(wide, state_name="state_aspect_wide", output_name="out_aspect_wide")
        narrow_gray = self.read_images("out_aspect_narrow")["cam_front"][0]
        wide_gray = self.read_images("out_aspect_wide")["cam_front"][0]
        self.assertFalse(np.array_equal(narrow_gray, wide_gray))

    def test_depth_is_world_units_and_background_infinite(self) -> None:
        task = self.samples / "task_base.json"
        self.render(task, state_name="state_depth", output_name="out_depth")
        depth = self.read_images("out_depth")["cam_front"][1]
        finite = depth[np.isfinite(depth)]
        self.assertGreater(finite.size, 0)
        self.assertTrue(np.all(finite > 0.0))
        self.assertTrue(np.any(~np.isfinite(depth)))

    def test_partial_edge_blocks_cover_full_non_divisible_resolution(self) -> None:
        def mutate(document: dict) -> None:
            document["render"]["width"] = 100
            document["render"]["height"] = 70
            document["render"]["blocks"] = {"width": 16, "height": 16}
            for camera in document["cameras"]:
                camera["aspect_ratio"] = 100.0 / 70.0

        task = self.clone_task("task_base.json", "task_partial_blocks.json", mutate=mutate)
        report = self.render(
            task,
            state_name="state_partial",
            output_name="out_partial",
            verify_full=True,
        )
        self.assertTrue(
            all(camera["full_recompute_verification"]["pixel_identical"] for camera in report["cameras"])
        )
        front = report["cameras"][0]
        blocks = json.loads(Path(front["outputs"]["blocks"]).read_text(encoding="utf-8"))["blocks"]
        self.assertEqual(blocks[-1]["rect"]["x1"], 100)
        self.assertEqual(blocks[-1]["rect"]["y1"], 70)
        gray, depth = self.read_images("out_partial")["cam_front"]
        self.assertEqual(gray.shape, (70, 100))
        self.assertEqual(depth.shape, (70, 100))

