from __future__ import annotations

import dataclasses
import unittest

from llm_sampling_diag.canonical import canonical_json
from llm_sampling_diag.diagnostics import diagnose
from llm_sampling_diag.errors import DiagnosticError, ErrorCode
from llm_sampling_diag.model import OperatorConfig
from llm_sampling_diag.operators import execute_sampling
from llm_sampling_diag.pipeline import run_task
from llm_sampling_diag.samplegen import build_tasks


class OperatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = build_tasks()
        cls.config = OperatorConfig(
            mask_priority="mask_first",
            dtype="float32",
            rounding_mode="none",
            subnormal_guard="clamp_tiny",
            tolerance=1e-5,
        )

    def task(self, case_id: str):
        return next(task for task in self.tasks if task.case_id == case_id)

    def test_deterministic_byte_identical_execution(self) -> None:
        task = self.task("case_tie_topk_mask")
        first = execute_sampling(task, self.config).to_dict()
        second = execute_sampling(task, self.config).to_dict()
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first["sample_sequence"], second["sample_sequence"])

    def test_different_seed_changes_sampling(self) -> None:
        task = self.task("case_tie_topk_mask")
        first = execute_sampling(task, self.config).to_dict()
        changed = dataclasses.replace(task, seed=task.seed + 1)
        second = execute_sampling(changed, self.config).to_dict()
        self.assertNotEqual(first["sample_sequence"], second["sample_sequence"])

    def test_tie_order_is_score_desc_then_index_asc(self) -> None:
        task = self.task("case_tie_topk_mask")
        result = execute_sampling(task, self.config).to_dict()
        by_index = {candidate["index"]: candidate for candidate in result["candidates"]}
        order = result["candidate_order"]
        for left, right in zip(order, order[1:]):
            self.assertGreaterEqual(by_index[left]["score"], by_index[right]["score"])
            if by_index[left]["score"] == by_index[right]["score"]:
                self.assertLess(left, right)
        self.assertEqual(
            task.logits[1] / task.temperature,
            task.logits[2] / task.temperature,
        )

    def test_tie_policy_unspecified(self) -> None:
        task = dataclasses.replace(self.task("case_tie_topk_mask"), tie_policy=None)
        with self.assertRaises(DiagnosticError) as context:
            execute_sampling(task, self.config)
        self.assertEqual(context.exception.code, ErrorCode.E_TIE_POLICY_UNSPECIFIED)

    def test_seed_unspecified(self) -> None:
        task = dataclasses.replace(self.task("case_tie_topk_mask"), seed=None)
        with self.assertRaises(DiagnosticError) as context:
            execute_sampling(task, self.config)
        self.assertEqual(context.exception.code, ErrorCode.E_SEED_UNSPECIFIED)

    def test_mask_priority_invalid_when_missing(self) -> None:
        config = dataclasses.replace(self.config, mask_priority=None)
        with self.assertRaises(DiagnosticError) as context:
            execute_sampling(self.task("case_tie_topk_mask"), config)
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_MASK_PRIORITY_INVALID)
        self.assertEqual(error.field, "mask_priority")

    def test_mask_priority_invalid_when_unknown(self) -> None:
        config = dataclasses.replace(self.config, mask_priority="unstable")
        with self.assertRaises(DiagnosticError) as context:
            execute_sampling(self.task("case_tie_topk_mask"), config)
        self.assertEqual(context.exception.code, ErrorCode.E_MASK_PRIORITY_INVALID)

    def test_masked_positions_are_never_sampled_or_retained(self) -> None:
        for case_id in ("case_tie_topk_mask", "case_topp_mask", "case_topk_topp_mask"):
            task = self.task(case_id)
            for priority in ("mask_first", "k_first"):
                config = dataclasses.replace(self.config, mask_priority=priority)
                result = execute_sampling(task, config).to_dict()
                masked = set(task.candidate_mask.excluded_indices)
                self.assertTrue(masked.isdisjoint(result["candidate_order"]))
                self.assertTrue(masked.isdisjoint(result["sample_sequence"]))
                removed = {entry["index"] for entry in result["removed_candidates"]}
                self.assertTrue(masked.issubset(removed))

    def test_top_p_cutoff_has_explicit_reason(self) -> None:
        task = self.task("case_topp_mask")
        result = execute_sampling(task, self.config).to_dict()
        reasons = {entry["reason"] for entry in result["removed_candidates"]}
        self.assertIn("mask_excluded", reasons)
        self.assertIn("top_p_cutoff", reasons)
        self.assertTrue(all(not item["reason"].startswith("top_p") or item["stage"] == "top_p"
                            for item in result["removed_candidates"]))

    def test_shape_mismatch(self) -> None:
        task = dataclasses.replace(self.task("case_tie_topk_mask"), logits_shape=(13,))
        with self.assertRaises(DiagnosticError) as context:
            execute_sampling(task, self.config)
        self.assertEqual(context.exception.code, ErrorCode.E_SHAPE_MISMATCH)

    def test_precision_spectrum_and_subnormal_report(self) -> None:
        task = self.task("case_precision_subnormal")
        spectrum = diagnose(task, self.config)
        self.assertLessEqual(spectrum["max_abs_error"], spectrum["effective_tolerance"])
        self.assertTrue(spectrum["within_tolerance"])
        self.assertTrue(spectrum["sample_sequence_equal"])
        self.assertGreater(len(spectrum["subnormal_events"]), 0)
        self.assertEqual(spectrum["subnormal_guard"], "clamp_tiny")
        self.assertIn("candidate_index", spectrum["subnormal_events"][0])

    def test_tolerance_exceeded_has_location(self) -> None:
        task = self.task("case_precision_subnormal")
        config = dataclasses.replace(self.config, tolerance=1e-12)
        with self.assertRaises(DiagnosticError) as context:
            diagnose(task, config)
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_TOLERANCE_EXCEEDED)
        self.assertEqual(error.case_id, task.case_id)
        self.assertIsNotNone(error.candidate_index)
        self.assertIsNotNone(error.stage)
        self.assertGreater(error.details["abs_error"], error.details["effective_tolerance"])

    def test_rounding_mode_trace_is_switchable(self) -> None:
        task = self.task("case_topp_mask")
        config = dataclasses.replace(self.config, rounding_mode="toward_zero")
        result = execute_sampling(task, config).to_dict()
        self.assertEqual(result["rounding_mode"], "toward_zero")
        self.assertGreater(len(result["rounding_trace"]), 0)
        self.assertTrue(all(item["mode"] == "toward_zero" for item in result["rounding_trace"]))

    def test_run_task_reports_cache_key_and_checksum(self) -> None:
        task = self.task("case_tie_topk_mask")
        report = run_task(task, self.config)
        self.assertEqual(len(report["cache_key"]), 64)
        self.assertEqual(len(report["result_checksum"]), 64)
        self.assertEqual(report["error_spectrum"]["case_id"], task.case_id)


if __name__ == "__main__":
    unittest.main()

