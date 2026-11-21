from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from igeval import EvaluatorConfig, QualityEvaluator, build_demo_samples


class ErrorAttributionTests(unittest.TestCase):
    def test_stage_failure_is_attributed_to_stage_and_sample(self) -> None:
        config = EvaluatorConfig(workers=1, latency_budget_ms=1_000.0)
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = QualityEvaluator(
                build_demo_samples(2), config=config, runtime_dir=runtime
            )
            with mock.patch(
                "igeval.pipeline._semantic", side_effect=RuntimeError("synthetic")
            ):
                report = evaluator.evaluate_batch()
        self.assertEqual(report["status"], "error")
        self.assertEqual(len(report["errors_by_stage"]["semantic"]), 2)
        self.assertEqual(report["unattributed_errors"], [])
        for error in report["errors_by_stage"]["semantic"]:
            self.assertEqual(error["code"], "E_STAGE_FAILURE")
            self.assertEqual(error["stage"], "semantic")
            self.assertIsNotNone(error["sample_id"])

    def test_unattributed_failure_is_aggregated(self) -> None:
        config = EvaluatorConfig(workers=1, latency_budget_ms=1_000.0)
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = QualityEvaluator(
                build_demo_samples(2), config=config, runtime_dir=runtime
            )
            with mock.patch(
                "igeval.pipeline.run_pipeline", side_effect=RuntimeError("synthetic")
            ):
                report = evaluator.evaluate_batch()
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["summary"]["unattributed"], 2)
        self.assertEqual(len(report["unattributed_errors"]), 2)
        for error in report["unattributed_errors"]:
            self.assertEqual(error["code"], "E_UNATTRIBUTED")
            self.assertIsNone(error["stage"])
            self.assertIsNotNone(error["sample_id"])


if __name__ == "__main__":
    unittest.main()

