from __future__ import annotations

import json
import threading
from unittest import mock

from pointcloud_workbench.errors import EXIT_DIGEST, BusinessError, InterruptedRun
from pointcloud_workbench.samplegen import SAMPLE_CACHE_KEY

from tests.helpers import SampleTest, assert_images_equal


class RecoveryTests(SampleTest):
    def test_clean_rerun_reuses_every_block(self) -> None:
        task = self.samples / "task_base.json"
        first = self.render(task, state_name="state_cache", output_name="out_first", verify_full=True)
        second = self.render(task, state_name="state_cache", output_name="out_second", verify_full=True)
        self.assertTrue(all(camera["rendered_block_count"] == 24 for camera in first["cameras"]))
        for camera in second["cameras"]:
            self.assertEqual(camera["rendered_block_count"], 0)
            self.assertEqual(camera["reused_block_count"], 24)
            self.assertTrue(camera["full_recompute_verification"]["pixel_identical"])
        assert_images_equal(self, self.read_images("out_first"), self.read_images("out_second"))
        leftovers = list((self.root / "state_cache").rglob(".tmp-*"))
        self.assertEqual(leftovers, [])

    def test_scheduling_budget_change_does_not_invalidate_pixels(self) -> None:
        task = self.samples / "task_base.json"
        self.render(task, state_name="state_cache", output_name="out_first")

        def mutate(document: dict) -> None:
            document["render"]["max_workers"] = 1
            document["render"]["memory_budget_bytes"] = 32768
            document["render"]["round_budget_units"] = 17.0

        changed = self.clone_task("task_base.json", "task_budget_only.json", mutate=mutate)
        report = self.render(
            changed,
            state_name="state_cache",
            output_name="out_second",
            verify_full=True,
        )
        for camera in report["cameras"]:
            self.assertEqual(camera["rendered_block_count"], 0)
            self.assertEqual(camera["reused_block_count"], 24)
            self.assertTrue(camera["full_recompute_verification"]["pixel_identical"])

    def test_missing_block_is_the_only_block_rerendered(self) -> None:
        task = self.samples / "task_base.json"
        self.render(task, state_name="state_cache", output_name="out_first")
        block_path = (
            self.root
            / "state_cache"
            / SAMPLE_CACHE_KEY
            / "cam_front"
            / "blocks"
            / "b0006.npz"
        )
        block_path.unlink()
        report = self.render(
            task,
            state_name="state_cache",
            output_name="out_second",
            verify_full=True,
        )
        front = next(camera for camera in report["cameras"] if camera["camera_id"] == "cam_front")
        self.assertEqual(front["rendered_block_count"], 1)
        self.assertEqual(front["reused_block_count"], 23)
        self.assertEqual(front["invalidated_blocks"][0]["block_index"], 6)
        self.assertEqual(front["invalidated_blocks"][0]["reasons"][0]["code"], "MISSING")
        assert_images_equal(self, self.read_images("out_first"), self.read_images("out_second"))

    def test_corrupt_block_content_is_selectively_repaired(self) -> None:
        task = self.samples / "task_base.json"
        self.render(task, state_name="state_cache", output_name="out_first")
        block_path = (
            self.root
            / "state_cache"
            / SAMPLE_CACHE_KEY
            / "cam_front"
            / "blocks"
            / "b0007.npz"
        )
        block_path.write_bytes(b"not a numpy archive")
        report = self.render(
            task,
            state_name="state_cache",
            output_name="out_second",
            verify_full=True,
        )
        front = next(camera for camera in report["cameras"] if camera["camera_id"] == "cam_front")
        self.assertEqual(front["rendered_block_count"], 1)
        self.assertEqual(front["reused_block_count"], 23)
        codes = {
            reason["code"]
            for block in front["invalidated_blocks"]
            for reason in block["reasons"]
        }
        self.assertIn("CORRUPT", codes)
        assert_images_equal(self, self.read_images("out_first"), self.read_images("out_second"))

    def test_final_digest_verification_repairs_only_damaged_block(self) -> None:
        task = self.samples / "task_base.json"
        self.render(task, state_name="state_cache", output_name="out_first")
        missing = (
            self.root
            / "state_cache"
            / SAMPLE_CACHE_KEY
            / "cam_front"
            / "blocks"
            / "b0001.npz"
        )
        victim = (
            self.root
            / "state_cache"
            / SAMPLE_CACHE_KEY
            / "cam_front"
            / "blocks"
            / "b0010.npz"
        )
        missing.unlink()
        lock = threading.Lock()
        corrupted = {"done": False}

        def corrupt_after_admission(index: int, camera_id: str) -> None:
            with lock:
                if not corrupted["done"]:
                    corrupted["done"] = True
                    victim.write_bytes(b"post-planner corruption")

        report = self.render(
            task,
            state_name="state_cache",
            output_name="out_second",
            verify_full=True,
            after_block=corrupt_after_admission,
        )
        front = next(camera for camera in report["cameras"] if camera["camera_id"] == "cam_front")
        self.assertEqual(front["rendered_block_count"], 1)
        self.assertEqual(front["reused_block_count"], 22)
        self.assertEqual(front["invalidated_block_count"], 2)
        repaired = [entry["block_index"] for entry in front["digest_repairs"]]
        self.assertIn(10, repaired)
        self.assertEqual(front["digest_repairs"][0]["business_code"], "E_DIGEST")
        self.assertTrue(front["digest_repairs"][0]["repaired"])
        assert_images_equal(self, self.read_images("out_first"), self.read_images("out_second"))

    def test_interrupt_at_boundary_then_resume(self) -> None:
        task = self.samples / "task_base.json"
        stop_event = threading.Event()
        lock = threading.Lock()
        seen = {"value": False}

        def stop_after_first(index: int, camera_id: str) -> None:
            with lock:
                if not seen["value"]:
                    seen["value"] = True
                    stop_event.set()

        with self.assertRaises(InterruptedRun) as caught:
            self.render(
                task,
                state_name="state_interrupt",
                output_name="out_interrupted",
                stop_event=stop_event,
                after_block=stop_after_first,
            )
        self.assertEqual(caught.exception.exit_code, 130)
        self.assertFalse((self.root / "out_interrupted").exists())
        marker = self.root / "state_interrupt" / SAMPLE_CACHE_KEY / "INTERRUPTED.json"
        self.assertTrue(marker.is_file())
        marker_doc = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_doc["exit_code"], 130)
        self.assertGreater(len(marker_doc["completed_blocks"]), 0)

        resumed = self.render(
            task,
            state_name="state_interrupt",
            output_name="out_resumed",
            verify_full=True,
        )
        self.assertFalse(marker.exists())
        self.assertGreater(
            sum(camera["reused_block_count"] for camera in resumed["cameras"]),
            0,
        )
        self.render(
            task,
            state_name="state_resume_reference",
            output_name="out_resume_reference",
            verify_full=True,
        )
        assert_images_equal(self, self.read_images("out_resumed"), self.read_images("out_resume_reference"))

    def test_fresh_edited_render_recovers_target_version_blocks_without_commit(self) -> None:
        task = self.samples / "task_edited.json"
        stop_event = threading.Event()
        lock = threading.Lock()
        seen = {"value": False}

        def stop_after_first(index: int, camera_id: str) -> None:
            with lock:
                if not seen["value"]:
                    seen["value"] = True
                    stop_event.set()

        with self.assertRaises(InterruptedRun):
            self.render(
                task,
                state_name="state_fresh_interrupt",
                output_name="out_fresh_interrupt",
                stop_event=stop_event,
                after_block=stop_after_first,
            )
        resumed = self.render(
            task,
            state_name="state_fresh_interrupt",
            output_name="out_fresh_resumed",
            verify_full=True,
        )
        self.assertGreater(
            sum(camera["reused_block_count"] for camera in resumed["cameras"]),
            0,
        )
        self.assertTrue(
            all(camera["full_recompute_verification"]["pixel_identical"] for camera in resumed["cameras"])
        )

    def test_unregistered_version_change_resets_whole_image(self) -> None:
        task = self.samples / "task_base.json"
        self.render(task, state_name="state_cache", output_name="out_first")

        def mutate(document: dict) -> None:
            document["cameras"][0]["vertical_fov_deg"] = 51.0

        changed = self.clone_task("task_base.json", "task_changed_fov.json", mutate=mutate)
        report = self.render(
            changed,
            state_name="state_cache",
            output_name="out_changed",
            verify_full=True,
        )
        front = next(camera for camera in report["cameras"] if camera["camera_id"] == "cam_front")
        self.assertEqual(front["rendered_block_count"], 24)
        self.assertEqual(front["reused_block_count"], 0)
        self.assertTrue(
            any(
                reason["code"] == "VERSION_RESET"
                for block in front["invalidated_blocks"]
                for reason in block["reasons"]
            )
        )

    def test_unrepairable_digest_returns_e_digest(self) -> None:
        task = self.samples / "task_base.json"

        def corrupt_write(path, *args, **kwargs):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"always corrupt after repair")
            return {"dependency_count": 0, "content_digest": "corrupt"}

        with mock.patch("pointcloud_workbench.pipeline.write_block", side_effect=corrupt_write):
            with self.assertRaises(BusinessError) as caught:
                self.render(
                    task,
                    state_name="state_digest_failure",
                    output_name="out_digest_failure",
                )
        self.assertEqual(caught.exception.code, "E_DIGEST")
        self.assertEqual(caught.exception.exit_code, EXIT_DIGEST)
        self.assertFalse((self.root / "out_digest_failure").exists())

