from __future__ import annotations

import dataclasses
import tempfile
import unittest

from igeval import DomainError, EvaluatorConfig, QualityEvaluator, build_demo_samples
from igeval.samples import render_signature


class ValidationTests(unittest.TestCase):
    def _evaluator(self, samples, runtime: str, **config_changes) -> QualityEvaluator:
        config = EvaluatorConfig(workers=1, latency_budget_ms=1_000.0, **config_changes)
        return QualityEvaluator(samples, config=config, runtime_dir=runtime)

    def test_duplicate_sample_id(self) -> None:
        base = build_demo_samples(2)
        duplicate = dataclasses.replace(base[1], sample_id=base[0].sample_id)
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = self._evaluator([base[0], duplicate], runtime)
            report = evaluator.evaluate_batch()
            query = evaluator.query(base[0].sample_id)
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["code"], "E_DUPLICATE_SAMPLE_ID")
        self.assertEqual(report["errors"][0]["field"], "sample_id")
        self.assertEqual(report["errors"][0]["sample_id"], base[0].sample_id)
        self.assertEqual(query["code"], "E_DUPLICATE_SAMPLE_ID")

    def test_seed_canvas_signature_mismatch(self) -> None:
        sample = build_demo_samples(1)[0]
        broken = dataclasses.replace(sample, width=sample.width + 1)
        with tempfile.TemporaryDirectory() as runtime:
            report = self._evaluator([broken], runtime).evaluate_batch()
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["code"], "E_CANVAS_MISMATCH")
        self.assertEqual(report["errors"][0]["field"], "canvas")
        self.assertEqual(report["errors"][0]["sample_id"], sample.sample_id)
        self.assertEqual(report["errors"][0]["stage"], "validation")

    def test_forged_signature_cannot_bypass_deterministic_pixels(self) -> None:
        sample = build_demo_samples(1)[0]
        altered = sample.image.copy()
        altered[0, 0, 0] = (int(altered[0, 0, 0]) + 1) % 256
        signature = render_signature(sample.seed, sample.width, sample.height, altered)
        forged = dataclasses.replace(
            sample,
            image=altered,
            render_signature=signature,
        )
        with tempfile.TemporaryDirectory() as runtime:
            report = self._evaluator([forged], runtime).evaluate_batch()
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["code"], "E_CANVAS_MISMATCH")
        self.assertIn(
            "expected_pixels_sha256",
            report["errors"][0]["details"],
        )

    def test_empty_prompt(self) -> None:
        sample = dataclasses.replace(build_demo_samples(1)[0], prompt="   ")
        with tempfile.TemporaryDirectory() as runtime:
            report = self._evaluator([sample], runtime).evaluate_batch()
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["code"], "E_EMPTY_PROMPT")
        self.assertEqual(report["validation_errors"][0]["field"], "prompt")

    def test_unknown_precision(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = self._evaluator(build_demo_samples(1), runtime)
            response = evaluator.query("sample-001", precision="fast-undeclared")
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], "E_UNKNOWN_PRECISION")
        self.assertEqual(response["errors"][0]["field"], "precision")

    def test_invalid_budget_and_memory_limits(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = self._evaluator(build_demo_samples(1), runtime)
            response = evaluator.query("sample-001", latency_budget_ms=0)
        self.assertEqual(response["code"], "E_INVALID_LATENCY_BUDGET")
        self.assertEqual(response["errors"][0]["field"], "latency_budget_ms")

        with self.assertRaises(DomainError) as context:
            EvaluatorConfig(max_feature_memory_bytes=0)
        self.assertEqual(context.exception.code, "E_INVALID_MEMORY_LIMIT")
        self.assertEqual(context.exception.field, "max_feature_memory_bytes")

        with self.assertRaises(DomainError) as context:
            EvaluatorConfig(max_cache_entries=0)
        self.assertEqual(context.exception.code, "E_INVALID_MEMORY_LIMIT")

    def test_latency_budget_error_contains_completed_stages(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = self._evaluator(build_demo_samples(1), runtime)
            response = evaluator.query("sample-001", latency_budget_ms=1e-9)
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], "E_LATENCY_BUDGET")
        self.assertEqual(
            response["latency"]["completed_stages"],
            ["preprocess", "semantic", "structure", "quality"],
        )
        self.assertEqual(response["latency"]["estimate"]["pixels"], 64 * 64)
        self.assertEqual(response["latency"]["estimate"]["blocks"], 16)
        self.assertGreater(
            response["latency"]["estimate"]["alignment_candidates"], 0
        )
        self.assertIsNotNone(response["final_score"])
        self.assertEqual(
            response["errors"][0]["degradation_category"],
            "LATENCY_BUDGET_EXCEEDED",
        )


if __name__ == "__main__":
    unittest.main()

