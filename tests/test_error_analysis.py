import unittest
from dataclasses import replace

from gbtdiag import TrainConfig, make_regression_dataset, validate
from gbtdiag.diagnostics import run_error_analysis
from gbtdiag.errors import ErrorCode


class ErrorAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.dataset = make_regression_dataset(n_rows=256, seed=31)
        self.config = TrainConfig(
            n_trees=10, max_depth=3, n_thresholds=6, seed=31
        )

    def test_float64_reference_has_zero_deviation(self):
        report = run_error_analysis(self.dataset, self.config)
        for round_block in report["rounds"]:
            for deviation in round_block["deviations"]:
                self.assertEqual(deviation["abs_deviation"], 0.0)
                self.assertEqual(deviation["rel_deviation"], 0.0)
        self.assertEqual(report["code"], ErrorCode.OK.value)

    def test_float32_report_contains_five_stages_and_dominant_source(self):
        config = replace(self.config, precision="float32")
        report = run_error_analysis(self.dataset, config)
        expected_stages = {
            "quantization",
            "gain",
            "leaf_division",
            "lr_scale",
            "accumulation",
        }
        for round_block in report["rounds"]:
            stages = {d["stage"] for d in round_block["deviations"]}
            self.assertEqual(stages, expected_stages)
            for deviation in round_block["deviations"]:
                self.assertIn("abs_deviation", deviation)
                self.assertIn("rel_deviation", deviation)
                self.assertEqual(len(deviation["interval"]), 2)
                self.assertIn("amplification", deviation)
            self.assertIn(
                round_block["dominant_stage"], expected_stages
            )
        self.assertIn(report["dominant_stage"], expected_stages)

    def test_tight_tolerance_raises_exceeded_with_location(self):
        config = replace(
            self.config,
            precision="float32",
            stage_tolerances=(1e-12, 1e-12, 1e-12, 1e-12, 1e-12),
        )
        report = run_error_analysis(self.dataset, config)
        self.assertEqual(
            report["code"], ErrorCode.TOLERANCE_EXCEEDED.value
        )
        violation = report["violations"][0]
        self.assertIn("stage", violation)
        self.assertIn("round_index", violation)
        self.assertIn("sample_index", violation)
        self.assertGreater(violation["abs_deviation"], 0.0)

    def test_validation_report_is_traceable(self):
        config = replace(
            self.config,
            precision="float32",
            stage_tolerances=(1e-12,) * 5,
        )
        report = validate(self.dataset, config)
        self.assertEqual(
            report["error_analysis"]["code"],
            ErrorCode.TOLERANCE_EXCEEDED.value,
        )
        trace_checks = [
            check
            for check in report["verification"]["checks"]
            if check["step"] == "error_attribution_trace"
        ]
        self.assertTrue(trace_checks)
        self.assertTrue(all(check["passed"] for check in trace_checks))


if __name__ == "__main__":
    unittest.main()
