from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import numpy as np

from diagnostic_app.acceptance import run_acceptance
from diagnostic_app.blocks import verify_stitch
from diagnostic_app.errors import AppError, ErrorCode, ErrorLedger
from diagnostic_app.metadata import verify_v2_checksums
from diagnostic_app.planner import plan_blocks
from diagnostic_app.samples import generate_all, sample_config, sample_tasks
from diagnostic_app.selfcheck import run_cross_module_self_check
from diagnostic_app.service import DiagnosticService
from diagnostic_app.snapshots import SnapshotStore


class ServiceWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tasks = sample_tasks()
        self.config = sample_config()
        self.ledger = ErrorLedger(self.root / "errors.jsonl")
        self.service = DiagnosticService(
            workspace=self.root / "runtime",
            config=self.config,
            tasks=self.tasks,
            error_ledger=self.ledger,
        )
        reference = np.linspace(0.0, 1.0, 36 * 48 * 3, dtype=np.float32).reshape(36, 48, 3)
        self.service.set_reference("tdc-001", reference)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_center_first_rest_verify_and_selfcheck(self) -> None:
        submission = self.service.submit_plan("tdc-001")
        self.assertIn("version", submission["plan_snapshot"])
        center = self.service.preview_center("tdc-001")
        self.assertEqual(center["block_index"], submission["plan"]["center_block_index"])
        self.assertIn("observed_generation_ms", center["timing"])

        remaining = self.service.preview_remaining("tdc-001")
        self.assertTrue(remaining["ok"], remaining["failures"])
        verification = self.service.verify_blocks("tdc-001")
        self.assertTrue(verification["bytewise_equal"])

        report = run_cross_module_self_check(self.service, "tdc-001")
        self.assertTrue(report["ok"], report)

    def test_latency_budget_returns_completed_blocks(self) -> None:
        self.service.submit_plan("tdc-001")
        center = self.service.preview_center("tdc-001")
        other = next(
            block["block_index"] for block in self.service.active_plan["blocks"]
            if block["block_index"] != center["block_index"]
        )
        self.service.config["latency_budget_ms"] = 0.0
        with self.assertRaises(AppError) as context:
            self.service.preview_block("tdc-001", other, enforce_budget=True)
        self.assertEqual(context.exception.code, ErrorCode.LATENCY_BUDGET)
        self.assertEqual(context.exception.details["completed_blocks"], [center["block_index"]])
        self.assertIn("observed_ms", context.exception.details)

    def test_mismatch_diagnostic_reports_coordinate_and_block(self) -> None:
        self.service.submit_plan("tdc-001")
        self.service.preview_center("tdc-001")
        self.service.preview_remaining("tdc-001")
        result = self.service.mismatch_diagnostic("tdc-001")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], ErrorCode.BLOCK_MISMATCH)
        self.assertIn("first_coordinate", result["details"])
        self.assertIn("block_index", result["details"])
        self.assertIn("delta_value", result["details"])

    def test_verify_stitch_detects_difference(self) -> None:
        plan = plan_blocks(self.tasks[0], self.config)
        global_image = np.zeros((4, 4, 1), dtype=np.float32)
        stitched = global_image.copy()
        stitched[2, 3, 0] = 0.5
        blocks = [
            {"block_index": 0, "row_start": 0, "row_end": 2, "col_start": 0, "col_end": 4},
            {"block_index": 1, "row_start": 2, "row_end": 4, "col_start": 0, "col_end": 4},
        ]
        result = verify_stitch(stitched, global_image, blocks)
        self.assertFalse(result["ok"])
        self.assertEqual(result["mismatch"]["block_index"], 1)
        self.assertEqual(result["mismatch"]["first_coordinate"][:2], [2, 3])

    def test_anchor_validation_error_codes(self) -> None:
        from diagnostic_app.pipeline import build_context

        task = self.tasks[0]
        with self.assertRaises(AppError) as context:
            build_context(task, self.config,
                          reference=self.service._references["tdc-001"],  # noqa: SLF001
                          anchor_strength=1.5)
        self.assertEqual(context.exception.code, ErrorCode.ANCHOR_STRENGTH)

        bad_reference = np.zeros((4, 5, 3), dtype=np.float32)
        with self.assertRaises(AppError) as context:
            build_context(task, self.config, reference=bad_reference, anchor_strength=0.2)
        self.assertEqual(context.exception.code, ErrorCode.REFERENCE_SHAPE)

    def test_snapshot_rollback_is_rejected(self) -> None:
        store = SnapshotStore(self.root / "snapshots", error_ledger=self.ledger)
        first = store.commit("plan", {"v": 1})
        store.commit("plan", {"v": 2})
        with self.assertRaises(AppError) as context:
            store.commit_explicit_version("plan", first["version"], {"old": True})
        self.assertEqual(context.exception.code, ErrorCode.SNAPSHOT_ROLLBACK)

    def test_stage_busy_is_immediate(self) -> None:
        self.service.submit_plan("tdc-001")
        with self.service.submission_lease():
            with self.assertRaises(AppError) as context:
                self.service.submit_plan("tdc-001")
        self.assertEqual(context.exception.code, ErrorCode.STAGE_BUSY)

    def test_parallel_and_total_resource_limits(self) -> None:
        limited_config = copy.deepcopy(self.config)
        limited_config["resource_limits"]["max_parallel_tasks"] = 1
        service = DiagnosticService(
            workspace=self.root / "limited-runtime",
            config=limited_config,
            tasks=self.tasks,
            error_ledger=self.ledger,
        )
        service.submit_plan("tdc-001")
        self.assertTrue(service.acquire_generation_slot(blocking=False))
        try:
            with self.assertRaises(AppError) as context:
                service.preview_center("tdc-001")
            self.assertEqual(context.exception.code, ErrorCode.RESOURCE_LIMIT)
        finally:
            service.release_generation_slot()

        tiny_config = copy.deepcopy(self.config)
        tiny_config["resource_limits"]["max_total_pixels"] = 16
        with self.assertRaises(AppError) as context:
            plan_blocks(self.tasks[0], tiny_config)
        self.assertEqual(context.exception.code, ErrorCode.RESOURCE_LIMIT)


class SampleAndAcceptanceTests(unittest.TestCase):
    def test_generated_metadata_and_full_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            samples = root / "samples"
            output = root / "acceptance"
            manifest = generate_all(samples)
            self.assertEqual(manifest["task_count"], 3)
            metadata = verify_v2_checksums(samples / "descriptions" / "v2" / "metadata.sqlite")
            self.assertTrue(metadata["ok"], metadata)

            report = run_acceptance(output, samples)
            self.assertTrue(report["ok"], report.get("missing_error_codes"))
            self.assertEqual(report["missing_error_codes"], [])
            self.assertTrue(Path(report["report_path"]).exists())
            self.assertTrue(Path(report["markdown_path"]).exists())


if __name__ == "__main__":
    unittest.main()
