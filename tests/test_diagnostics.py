from __future__ import annotations

import unittest

import numpy as np

from embedding_ops.cli import sample_specs
from embedding_ops.config import NumericsConfig
from embedding_ops.diagnostics import (
    build_error_spectrum,
    run_error_injection_suite,
)
from embedding_ops.numeric import protect_subnormal
from embedding_ops.pipeline import run_job


class SpectrumTests(unittest.TestCase):
    def test_float64_has_zero_reference_error(self) -> None:
        record = run_job(sample_specs()[0], NumericsConfig())
        self.assertTrue(record["self_check"]["passed"])
        self.assertEqual(record["spectrum"]["final_max_abs_deviation"], 0.0)
        self.assertEqual(record["spectrum"]["dominant_source"], "none")
        self.assertTrue(record["spectrum"]["within_tolerance"])
        for stage in record["spectrum"]["stages"]:
            self.assertLessEqual(
                stage["error_interval"][0], stage["error_interval"][1]
            )
            self.assertEqual(stage["max_abs_deviation"], 0.0)

    def test_precision_tiers_have_expected_error_ordering(self) -> None:
        tiers = {
            "float64": run_job(sample_specs()[0], NumericsConfig()),
            "float32": run_job(sample_specs()[1], NumericsConfig()),
            "qint16": run_job(sample_specs()[2], NumericsConfig()),
            "qint8": run_job(sample_specs()[3], NumericsConfig()),
        }
        errors = {
            tier: record["spectrum"]["final_max_abs_deviation"]
            for tier, record in tiers.items()
        }
        self.assertEqual(errors["float64"], 0.0)
        self.assertLess(errors["float64"], errors["float32"])
        self.assertLess(errors["float32"], errors["qint16"])
        self.assertLess(errors["qint16"], errors["qint8"])
        for tier, record in tiers.items():
            self.assertIn(
                record["spectrum"]["dominant_source"],
                {
                    "none",
                    "dequantization",
                    "reduction_weights",
                    "weighted_accumulation",
                },
                tier,
            )
            self.assertTrue(record["self_check"]["passed"])
            interval = record["output_interval"]
            lower = np.asarray(interval["lower"])
            upper = np.asarray(interval["upper"])
            self.assertTrue(np.all(lower <= upper))
            self.assertTrue(interval["contains_actual"])
            self.assertTrue(interval["contains_reference"])

    def test_tolerance_is_fixed_and_not_widened(self) -> None:
        record = run_job(sample_specs()[3], NumericsConfig())
        self.assertFalse(record["spectrum"]["within_tolerance"])
        self.assertEqual(
            record["spectrum"]["tolerance"]["policy"],
            "fixed_no_auto_widening",
        )
        self.assertIn("fixed_tolerance_exceeded", record["degradation_reasons"])

    def test_accumulation_rounding_is_attributed(self) -> None:
        record = run_job(
            sample_specs()[0],
            NumericsConfig(
                accum_rounding_enabled=True,
                accum_rounding_step=0.05,
            ),
        )
        self.assertGreater(
            record["spectrum"]["accumulation_rounding_delta_max_abs"], 0.0
        )
        self.assertEqual(
            record["spectrum"]["dominant_source"],
            "weighted_accumulation",
        )
        self.assertTrue(record["self_check"]["passed"])

    def test_max_deviation_position_matches_interval_shape(self) -> None:
        record = run_job(sample_specs()[3], NumericsConfig())
        stages = {stage["stage"]: stage for stage in record["spectrum"]["stages"]}
        for name, position in (
            ("dequantize", [16, 6]),
            ("gather", [6, 2, 6]),
            ("reduce", [6, 6]),
            ("accumulate", [6, 6]),
        ):
            self.assertEqual(stages[name]["shape"], position)
            self.assertEqual(len(stages[name]["max_deviation_position"]), len(position))

    def test_max_relative_deviation_is_global_not_abs_position(self) -> None:
        reference = np.array([100.0, 1e-6], dtype=np.float64)
        actual = np.array([100.1, 2e-6], dtype=np.float64)
        spectrum = build_error_spectrum(
            job_id="relative",
            table_dtype="float64",
            dequantized_table=actual,
            reference_table=reference,
            gathered_actual=actual,
            gathered_reference=reference,
            reduced_actual=actual,
            reduced_reference=reference,
            output_actual=actual,
            output_reference=reference,
            config=NumericsConfig(),
            quant_scale=0.0,
        )
        stage = spectrum["stages"][0]
        self.assertEqual(stage["max_deviation_position"], [0])
        self.assertEqual(stage["max_rel_deviation_position"], [1])
        self.assertAlmostEqual(stage["max_rel_deviation"], 1.0, places=12)

    def test_subnormal_policies(self) -> None:
        values = np.array([0.0, 1e-310, -1e-310, 1.0])
        flushed = protect_subnormal(values, "flush_to_zero", 1e-308)
        clamped = protect_subnormal(values, "clamp_to_threshold", 1e-308)
        np.testing.assert_array_equal(
            flushed,
            np.array([0.0, 0.0, -0.0, 1.0]),
        )
        np.testing.assert_array_equal(
            clamped,
            np.array([0.0, 1e-308, -1e-308, 1.0]),
        )


class InjectionTests(unittest.TestCase):
    def test_required_injections_are_attributed(self) -> None:
        suite = run_error_injection_suite(
            sample_specs()[0],
            NumericsConfig(),
        )
        self.assertEqual(suite["unattributed_exceptions"], [])
        records = suite["records"]
        self.assertEqual(len(records), 8)
        self.assertTrue(
            all(record["result"] == "error_raised" for record in records)
        )
        codes = {record["code"] for record in records}
        self.assertTrue(
            {
                "E_SHAPE_MISMATCH",
                "E_INDEX_EMPTY",
                "E_INDEX_OUT_OF_RANGE",
                "E_SCALE_NEGATIVE",
                "E_DTYPE_UNKNOWN",
                "E_QUANT_INVALID",
                "E_DUP_JOB",
            }.issubset(codes)
        )
        for record in records:
            self.assertTrue(record["stage"])
            self.assertEqual(record["job_id"], record["injection"]["job_id"])


if __name__ == "__main__":
    unittest.main()
