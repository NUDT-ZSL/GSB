import unittest
from dataclasses import replace

from drift_monitor.config_migration import upgrade_to_v3
from drift_monitor.config_reader import read_config
from drift_monitor.data import DriftInjection, generate_batch
from drift_monitor.drift import FeatureResult, sort_results
from drift_monitor.statistics import compute_batch_stat, fold_window
from drift_monitor.drift import evaluate_feature


CONFIG = """# DRIFT_MONITOR_CONFIG v1
income_score|numeric|1.0|4|true
session_length|numeric|1.0|4|true
device_class|categorical|0.18|4|true
"""


class DriftMethodTests(unittest.TestCase):
    def setUp(self):
        self.config = upgrade_to_v3(read_config(CONFIG))
        self.specs = self.config.features
        reference_batches = [generate_batch(batch_id=i, sample_count=150) for i in range(4)]
        self.reference_stats = [
            compute_batch_stat(batch, self.specs) for batch in reference_batches
        ]
        self.window = fold_window(self.reference_stats, self.specs)

    def test_quantile_distance_method(self):
        spec = replace(self.specs["income_score"], methods=["quantile_dist"])
        drifted = generate_batch(
            batch_id=5,
            sample_count=150,
            injection=DriftInjection(batch_id=5, mean_shift=25.0),
        )
        current = compute_batch_stat(drifted, self.specs)
        result = evaluate_feature(
            spec,
            self.window["numeric"]["income_score"],
            None,
            current,
            window_start=0,
            window_end=3,
        )
        self.assertEqual(result.method, "quantile_dist")
        self.assertIn("reference_quantiles", result.detail)
        self.assertTrue(result.triggered)

    def test_categorical_proportion_distance(self):
        drifted = generate_batch(
            batch_id=6,
            sample_count=150,
            injection=DriftInjection(batch_id=6, category_shift=True),
        )
        current = compute_batch_stat(drifted, self.specs)
        spec = self.specs["device_class"]
        result = evaluate_feature(
            spec,
            None,
            self.window["categorical"]["device_class"],
            current,
            0,
            3,
        )
        self.assertEqual(result.method, "proportion_dist")
        self.assertTrue(0.0 <= result.drift_amount <= 1.0)

    def test_tie_break_by_feature_name(self):
        shared = dict(
            column_type="numeric",
            method="mean_shift",
            drift_amount=1.25,
            threshold=1.0,
            triggered=True,
            severity="low",
            window_start=0,
            window_end=3,
            detail={},
        )
        zeta = FeatureResult(feature="zeta", **shared)
        alpha = FeatureResult(feature="alpha", **shared)
        ordered = sort_results([zeta, alpha])
        self.assertEqual([item.feature for item in ordered], ["alpha", "zeta"])


if __name__ == "__main__":
    unittest.main()
