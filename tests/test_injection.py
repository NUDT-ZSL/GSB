import unittest

import numpy as np

from gbtdiag import (
    TrainConfig,
    inject_diagnose,
    make_injection,
    make_regression_dataset,
)
from gbtdiag.errors import ErrorCode


class InjectionFrameworkTests(unittest.TestCase):
    def setUp(self):
        self.config = TrainConfig(
            n_trees=10,
            max_depth=3,
            n_thresholds=6,
            seed=17,
            precision="float32",
        )
        self.dataset = make_regression_dataset(n_rows=256, seed=17)

    def test_four_stage_injections_and_degradation_chain(self):
        specs = [
            make_injection(
                "category_oor",
                feature_index=5,
                sample_indices=[0, 10, 20],
                value=42,
            ),
            make_injection(
                "duplicate_threshold", round_index=2, feature_index=0
            ),
            make_injection(
                "nonfinite_hessian",
                round_index=4,
                sample_indices=[1, 2, 3],
                value=float("inf"),
            ),
            make_injection(
                "negative_learning_rate", round_index=6, value=-0.2
            ),
        ]
        report = inject_diagnose(
            self.dataset, self.config, specs
        )
        records = report["training"]["records"]
        codes = {record["code"] for record in records}
        for expected in (
            ErrorCode.CATEGORY_OUT_OF_RANGE,
            ErrorCode.DUPLICATE_THRESHOLD,
            ErrorCode.NONFINITE_HESSIAN,
            ErrorCode.NEGATIVE_LEARNING_RATE,
        ):
            self.assertIn(expected.value, codes)

        stages = {record["stage"] for record in records}
        self.assertEqual(
            stages, {"binning", "gain", "leaf", "prediction"}
        )

        categories = {
            record["degradation"]["category"]
            for record in records
            if record["degradation"]
        }
        self.assertEqual(
            categories,
            {
                "row_quarantine",
                "skip_round_split",
                "default_floor_fallback",
                "zero_lr_hold",
            },
        )

        # Every degradation points back at the triggering upstream code.
        for record in records:
            if record["degradation"]:
                self.assertEqual(
                    record["degradation"]["trigger_code"],
                    record["code"],
                )

        self.assertTrue(report["verification"]["passed"])

    def test_injection_records_carry_sample_indices(self):
        specs = [
            make_injection(
                "category_oor",
                feature_index=5,
                sample_indices=[7, 8],
                value=-3,
            ),
            make_injection(
                "nonfinite_hessian",
                round_index=1,
                sample_indices=[9, 11],
                value=float("nan"),
            ),
        ]
        report = inject_diagnose(self.dataset, self.config, specs)
        by_code = {
            record["code"]: record
            for record in report["training"]["records"]
        }
        self.assertEqual(
            by_code[ErrorCode.CATEGORY_OUT_OF_RANGE.value][
                "sample_indices"
            ],
            [7, 8],
        )
        self.assertEqual(
            by_code[ErrorCode.NONFINITE_HESSIAN.value][
                "sample_indices"
            ],
            [9, 11],
        )

    def test_negative_learning_rate_holds_prediction(self):
        specs = [
            make_injection(
                "negative_learning_rate", round_index=3, value=-0.5
            )
        ]
        report = inject_diagnose(self.dataset, self.config, specs)
        round_3 = report["training"]["rounds"][3]
        self.assertEqual(round_3["learning_rate"], 0.0)
        before = report["training"]["rounds"][2]["prediction_after"]
        after = round_3["prediction_after"]
        self.assertTrue(np.array_equal(np.asarray(before), np.asarray(after)))


if __name__ == "__main__":
    unittest.main()
