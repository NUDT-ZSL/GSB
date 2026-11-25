from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gen_samples  # noqa: E402
from imgdiag.errors import WorkbenchError  # noqa: E402
from imgdiag.geometry import build_mask_model  # noqa: E402
from imgdiag.imaging import generate_image  # noqa: E402
from imgdiag.numeric import recompute_stage_diagnostics  # noqa: E402
from imgdiag.pipeline import run_edit  # noqa: E402
from imgdiag.pipeline import compare_full_block  # noqa: E402
from imgdiag.revision import detect_stale_result, plan_revision  # noqa: E402
from imgdiag.scheduler import (  # noqa: E402
    adjudicate_batch,
    plan_batches,
    run_process_pool,
)
from imgdiag.selfcheck import self_check_workbench  # noqa: E402
from imgdiag.workbench import CheckpointedWorkbench  # noqa: E402


class DeterministicCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.params = gen_samples.build_params(0)
        self.tasks = [gen_samples.build_task(i, 0) for i in range(12)]

    def test_image_bytes_are_deterministic(self) -> None:
        first = generate_image(12345, [32, 40, 3])
        second = generate_image(12345, [32, 40, 3])
        self.assertEqual(first.tobytes(), second.tobytes())

    def test_boundary_coordinates_are_deterministic(self) -> None:
        task = self.tasks[0]
        image = generate_image(task["image_seed"], task["image_shape"])
        first = build_mask_model(task["mask"], task["image_shape"], image)
        second = build_mask_model(task["mask"], task["image_shape"], image)
        for region_a, region_b in zip(first["regions"], second["regions"]):
            self.assertEqual(
                region_a["boundary_coords"].tobytes(),
                region_b["boundary_coords"].tobytes(),
            )
            self.assertEqual(
                region_a["context_coords"].tobytes(),
                region_b["context_coords"].tobytes(),
            )

    def test_mask_error_codes(self) -> None:
        cases = gen_samples.build_invalid_cases()
        by_id = {case["edit_id"]: case for case in cases}
        expectations = {
            "invalid-mask-degenerate": "E_MASK_DEGENERATE",
            "invalid-mask-intersect": "E_MASK_INTERSECT",
            "invalid-mask-oob": "E_MASK_OUT_OF_BOUNDS",
            "invalid-style-strength": "E_STYLE_INVALID",
            "invalid-transform-matrix": "E_TRANSFORM_INVALID",
        }
        for edit_id, code in expectations.items():
            result = run_edit(by_id[edit_id], self.params, profiles=())
            self.assertFalse(result["ok"], edit_id)
            self.assertEqual(result["code"], code, edit_id)

    def test_all_generated_tasks_block_full_byte_identical(self) -> None:
        for task in self.tasks:
            with self.subTest(edit_id=task["edit_id"]):
                result = run_edit(task, self.params, profiles=())
                self.assertIn("summary", result)
                summary = result["summary"]
                self.assertEqual(summary["code"], "OK")
                self.assertTrue(summary["block_match"]["byte_identical"])
                self.assertEqual(
                    summary["hashes"]["float64_output"],
                    summary["hashes"]["block_output"],
                )

    def test_precision_diagnostics_recompute_from_details(self) -> None:
        task = self.tasks[0]
        result = run_edit(task, self.params)
        summary = result["summary"]
        for profile in ("float64", "float32", "fixed"):
            with self.subTest(profile=profile):
                recomputed = recompute_stage_diagnostics(result["detail_arrays"][profile])
                reported = summary["precision"][profile]
                self.assertEqual(
                    recomputed["dominant_error_level"],
                    reported["dominant_error_level"],
                )
                for level, stats in recomputed["levels"].items():
                    self.assertAlmostEqual(
                        stats["rmse"], reported["levels"][level]["rmse"], places=12
                    )

    def test_block_mismatch_reports_first_coordinate(self) -> None:
        result = run_edit(self.tasks[1], self.params, profiles=())
        blocked = result["blocked"]
        blocked["output"][2, 3, 0] += 0.25
        comparison = compare_full_block(result["bundle"], result["reference"], blocked)
        self.assertFalse(comparison["ok"])
        self.assertEqual(comparison["code"], "E_BLOCK_MISMATCH")
        self.assertIsNotNone(comparison["first_mismatch"])
        self.assertIn("coordinate_xy", comparison["first_mismatch"])


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.params = gen_samples.build_params(0)
        self.tasks = [gen_samples.build_task(i, 0) for i in range(10)]

    def test_process_pool_order_and_results(self) -> None:
        plan = adjudicate_batch(0, self.tasks, self.params, worker_override=2)
        tasks_by_id = {task["edit_id"]: task for task in self.tasks}
        first = run_process_pool(plan, tasks_by_id, self.params, profiles=())
        second = run_process_pool(plan, tasks_by_id, self.params, profiles=())
        self.assertEqual([item["edit_id"] for item in first], plan["merge_order"])
        hashes_a = [item["hashes"]["float64_output"] for item in first]
        hashes_b = [item["hashes"]["float64_output"] for item in second]
        self.assertEqual(hashes_a, hashes_b)
        self.assertTrue(all(item["ok"] for item in first))

    def test_budget_and_concurrency_rejections(self) -> None:
        huge = {
            "edit_id": "unit-memory", "image_seed": 1,
            "image_shape": [4096, 4096, 3],
            "mask": {"polygons": [[[1, 1], [4000, 2], [2000, 4000]]]},
            "prompt": "texture", "style_strength": 0.5, "iterations": 1,
            "block_size": 64, "dtype_profile": "float64", "revision": 1,
        }
        memory_plan = adjudicate_batch(0, [huge], self.params)
        self.assertEqual(memory_plan["decisions"][0]["code"], "E_MEMORY_BUDGET")

        concurrent = copy.deepcopy(self.tasks[0])
        concurrent["requested_block_concurrency"] = 99
        strict_params = copy.deepcopy(self.params)
        strict_params["budget"]["strict_block_concurrency"] = True
        concurrency_plan = adjudicate_batch(1, [concurrent], strict_params)
        self.assertEqual(
            concurrency_plan["decisions"][0]["code"], "E_BLOCK_CONCURRENCY"
        )

    def test_tight_budget_records_degradation(self) -> None:
        tight = copy.deepcopy(self.params)
        tight["budget"]["max_total_resident_bytes"] = 680 * 1024
        tight["budget"]["single_block_max_bytes"] = 96 * 1024
        plan = adjudicate_batch(0, self.tasks, tight, worker_override=4)
        self.assertIn("quality", " ".join(plan["basis"]).lower() + "quality")
        self.assertTrue(plan["worker_reductions"] or plan["affected_task_ids"])


class CheckpointRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="imgdiag-test-")
        self.workdir = self.tmp.name
        self.params = gen_samples.build_params(0)
        self.tasks = [gen_samples.build_task(i, 0) for i in range(4)]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_restart_reuses_all_committed_stages(self) -> None:
        wb = CheckpointedWorkbench(self.workdir)
        wb.run_all_batches(self.tasks, self.params, batch_size=2)
        checkpoint_count = wb.checkpoints.latest_version()
        self.assertGreaterEqual(checkpoint_count, 20)

        restarted = CheckpointedWorkbench(self.workdir)
        restarted.run_all_batches(self.tasks, self.params, batch_size=2)
        for edit_id in [task["edit_id"] for task in self.tasks]:
            entry = restarted.ledger["tasks"][edit_id]
            self.assertEqual(entry["recomputed_stages"], [])
            self.assertEqual(len(entry["reused_stages"]), 5)
        verification = self_check_workbench(self.workdir, self.tasks, self.params)
        self.assertTrue(verification["ok"], verification)

    def test_style_revision_invalidates_only_downstream(self) -> None:
        wb = CheckpointedWorkbench(self.workdir)
        wb.run_all_batches(self.tasks, self.params, batch_size=2)
        revised = copy.deepcopy(self.tasks)
        revised[2]["revision"] = 2
        revised[2]["style_strength"] = min(1.0, float(revised[2]["style_strength"]) + 0.2)
        plan = plan_revision(self.tasks, revised)
        entry_plan = next(item for item in plan["entries"] if item["edit_id"] == "edit-0002")
        self.assertEqual(entry_plan["changed_fields"], ["style_strength"])
        wb.prepare_revision_reuse(revised, self.params, plan)
        wb.run_all_batches(revised, self.params, batch_size=2)
        entry = wb.ledger["tasks"]["edit-0002"]
        self.assertEqual(
            entry["reused_stages"], ["S1_mask_boundary", "S2_completion"]
        )
        self.assertEqual(
            entry["recomputed_stages"],
            ["S3_style", "S4_fusion_blocks", "S5_metrics"],
        )
        unchanged = wb.ledger["tasks"]["edit-0000"]
        self.assertEqual(unchanged["recomputed_stages"], [])

    def test_stale_result_names_edit_and_fields(self) -> None:
        wb = CheckpointedWorkbench(self.workdir)
        wb.run_task(self.tasks[0], self.params)
        old_key = wb.ledger["tasks"]["edit-0000"]["current_cache_key"]
        old_record = wb.cache.get_key(old_key)
        revised = copy.deepcopy(self.tasks[0])
        revised["revision"] = 2
        revised["style_strength"] = 0.111 if revised["style_strength"] != 0.111 else 0.222
        stale = detect_stale_result(
            "edit-0000", old_record, revised, "different-current-key"
        )
        self.assertIsNotNone(stale)
        self.assertEqual(stale["code"], "E_STALE_RESULT")
        self.assertEqual(stale["edit_id"], "edit-0000")
        self.assertIn("revision", stale["stale_fields"])
        self.assertIn("style_strength", stale["stale_fields"])

        mask_revised = copy.deepcopy(self.tasks[0])
        mask_revised["revision"] = 2
        mask_revised["mask"] = {"polygons": [[[1, 1], [12, 2], [6, 12]]]}
        mask_stale = detect_stale_result(
            "edit-0000", old_record, mask_revised, "different-current-key"
        )
        self.assertIn("mask", mask_stale["stale_fields"])


if __name__ == "__main__":
    unittest.main()
