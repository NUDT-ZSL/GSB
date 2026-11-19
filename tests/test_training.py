from __future__ import annotations

import tempfile
import unittest

from numeric_workbench.diagnostics import compare_runs, deviation_metrics
from numeric_workbench.training import train_prepared
from tests.helpers import config_with, make_dataset, prepare_for


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path, self.dataset, self.split = make_dataset(self.temp.name)
        self.config = config_with(
            max_iterations=12,
            convergence_tolerance=1e-9,
            min_improvement=0.0,
            chunk_size=24,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_same_precision_repeats_byte_identically(self):
        prepared = prepare_for(self.dataset, self.split, self.config)
        first = train_prepared(
            prepared,
            self.config,
            reduce_mode="chunked",
            run_id="repeat",
            collect_chunk_details=False,
        )
        second = train_prepared(
            prepared,
            self.config,
            reduce_mode="chunked",
            run_id="repeat",
            collect_chunk_details=False,
        )
        self.assertEqual(first.core_digest, second.core_digest)
        self.assertEqual(first.losses_hex, second.losses_hex)
        self.assertEqual(first.params_hex, second.params_hex)
        self.assertEqual(first.validation_trajectory, second.validation_trajectory)

    def test_chunked_and_full_compare_round_by_round(self):
        prepared = prepare_for(self.dataset, self.split, self.config)
        chunked = train_prepared(
            prepared,
            self.config,
            reduce_mode="chunked",
            run_id="chunked",
            collect_chunk_details=False,
        )
        full = train_prepared(
            prepared,
            self.config,
            reduce_mode="full",
            run_id="full",
            collect_chunk_details=False,
        )
        comparison = compare_runs(chunked, full, tolerance=1e-6)
        self.assertTrue(comparison["within_tolerance"])
        self.assertIsNone(comparison["first_exceedance"])
        self.assertEqual(len(comparison["rounds"]), chunked.rounds)
        self.assertIn("parameter_abs_deviation", comparison["rounds"][0])
        self.assertEqual(len(comparison["final_parameter_spectrum"]), len(prepared.feature_names))
        strict = compare_runs(chunked, full, tolerance=0.0)
        self.assertFalse(strict["within_tolerance"])
        self.assertEqual(strict["first_exceedance"]["round"], 1)

    def test_fixed_point_replay_is_exact(self):
        config = self.config.replace(precision="fixed_point")
        prepared = prepare_for(self.dataset, self.split, config, precision="fixed_point")
        run = train_prepared(
            prepared,
            config,
            reduce_mode="chunked",
            run_id="fixed",
            collect_chunk_details=False,
        )
        proof = run.fixed_replay_proof()
        self.assertIsNotNone(proof)
        self.assertTrue(proof["recomputed_final_matches"])
        self.assertTrue(proof["quantize_dequantize_roundtrip_matches"])
        self.assertEqual(proof["scale"], config.fixed_scale)
        self.assertEqual(proof["rounding_mode"], config.rounding_mode)

    def test_precision_switch_keeps_chunk_plan_and_changes_only_numeric_results(self):
        plans = {}
        results = {}
        for precision in ("float64", "float32", "fixed_point"):
            config = self.config.replace(precision=precision)
            prepared = prepare_for(self.dataset, self.split, config, precision=precision)
            plans[precision] = prepared.chunk_plan_fingerprint["sha256"]
            results[precision] = train_prepared(
                prepared,
                config,
                reduce_mode="chunked",
                run_id=precision,
                collect_chunk_details=False,
            )
        self.assertEqual(len(set(plans.values())), 1)
        metrics = deviation_metrics(results["float64"], results["float32"])
        self.assertIn("absolute", metrics)
        self.assertIn("relative", metrics)
        self.assertIn("error_amplification_ratio", metrics)

    def test_stump_ensemble_family_trains(self):
        config = self.config.replace(model_family="stump_ensemble")
        prepared = prepare_for(self.dataset, self.split, config)
        run = train_prepared(
            prepared,
            config,
            reduce_mode="chunked",
            run_id="stumps",
            collect_chunk_details=False,
        )
        self.assertGreater(len(prepared.feature_names), len(config.numeric_columns))
        self.assertEqual(run.rounds, config.max_iterations)
        self.assertIn("stump:", " ".join(prepared.feature_names))

    def test_minmax_standardization_trains(self):
        config = self.config.replace(standardization="minmax")
        prepared = prepare_for(self.dataset, self.split, config)
        run = train_prepared(
            prepared,
            config,
            reduce_mode="chunked",
            run_id="minmax",
            collect_chunk_details=False,
        )
        self.assertEqual(run.rounds, config.max_iterations)
        self.assertTrue(all(stats.method == "minmax" for stats in prepared.standardizer.stats.values()))

    def test_all_rounding_modes_replay_exactly(self):
        for mode in (
            "nearest_even",
            "half_away_from_zero",
            "toward_zero",
            "floor",
            "ceil",
        ):
            config = self.config.replace(
                precision="fixed_point", rounding_mode=mode, max_iterations=4
            )
            prepared = prepare_for(
                self.dataset, self.split, config, precision="fixed_point"
            )
            run = train_prepared(
                prepared,
                config,
                reduce_mode="chunked",
                run_id=f"fixed-{mode}",
                collect_chunk_details=False,
            )
            proof = run.fixed_replay_proof()
            self.assertTrue(proof["recomputed_final_matches"], mode)
            self.assertTrue(proof["quantize_dequantize_roundtrip_matches"], mode)


if __name__ == "__main__":
    unittest.main()
