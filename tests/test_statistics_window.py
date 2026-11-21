import unittest
from drift_monitor.config_reader import read_config
from drift_monitor.data import DriftInjection, generate_batch, generate_stream
from drift_monitor.errors import DriftError
from drift_monitor.hashing import canonicalize
from drift_monitor.statistics import (
    compute_batch_stat,
    fold_window,
    subtract_categorical,
    subtract_numeric,
)
from drift_monitor.windowing import ReferenceWindow, validate_window_params


def specs():
    config = read_config(
        """# DRIFT_MONITOR_CONFIG v1
income_score|numeric|1.2|4|true
device_class|categorical|0.18|4|true
"""
    )
    return config.features


class StatisticTests(unittest.TestCase):
    def test_deterministic_batch_fingerprint_and_stats(self):
        first = generate_batch(batch_id=3, sample_count=120)
        second = generate_batch(batch_id=3, sample_count=120)
        self.assertEqual(first.fingerprint(), second.fingerprint())
        spec = specs()
        stat_a = compute_batch_stat(first, spec)
        stat_b = compute_batch_stat(second, spec)
        self.assertEqual(
            canonicalize(stat_a.to_cache_dict()),
            canonicalize(stat_b.to_cache_dict()),
        )

    def test_numeric_and_categorical_calibers_distinct(self):
        batch = generate_batch(batch_id=1)
        stat = compute_batch_stat(batch, specs())
        self.assertIn("income_score", stat.numeric)
        self.assertIn("device_class", stat.categorical)
        numeric = stat.numeric["income_score"]
        categorical = stat.categorical["device_class"]
        self.assertEqual(numeric.count + numeric.missing, numeric.total)
        self.assertEqual(sum(categorical.frequencies.values()), categorical.observed)

    def test_exact_subtraction_matches_fold(self):
        stream = generate_stream(batch_count=6, sample_count=80)
        spec = specs()
        records = [compute_batch_stat(batch, spec) for batch in stream]
        window = ReferenceWindow(length=4, step=1)
        for record in records:
            state = window.append_and_slide(record, spec)
        folded = fold_window(records[-4:], spec)
        aggregate = window.current_aggregate()
        numeric = aggregate["numeric"]["income_score"]
        expected = folded["numeric"]["income_score"]
        self.assertEqual(numeric.sorted_values, expected.sorted_values)
        self.assertEqual(numeric.sum, expected.sum)
        self.assertEqual(numeric.sum_squares, expected.sum_squares)
        self.assertEqual(state.batch_ids, [2, 3, 4, 5])
        self.assertEqual(state.last_removed, [1])

    def test_illegal_value_is_attributed(self):
        batch = generate_batch(batch_id=2)
        batch.columns["income_score"][0] = "NaN"
        with self.assertRaises(DriftError) as ctx:
            compute_batch_stat(batch, specs())
        self.assertEqual(ctx.exception.code, "E_ILLEGAL_VALUE")
        self.assertEqual(ctx.exception.context["feature"], "income_score")
        self.assertEqual(ctx.exception.context["batch_id"], 2)

    def test_empty_batch(self):
        with self.assertRaises(DriftError) as ctx:
            generate_batch(batch_id=0, sample_count=0)
        self.assertEqual(ctx.exception.code, "E_EMPTY_BATCH")

    def test_window_validation(self):
        with self.assertRaises(DriftError) as ctx:
            validate_window_params(length=0, step=1)
        self.assertEqual(ctx.exception.code, "E_WINDOW_INVALID")
        with self.assertRaises(DriftError) as ctx:
            validate_window_params(length=3, step=4)
        self.assertEqual(ctx.exception.code, "E_WINDOW_INVALID")

    def test_missing_rate_drift_increases_missing_count(self):
        normal = generate_batch(batch_id=4)
        drifted = generate_batch(
            batch_id=4,
            injection=DriftInjection(batch_id=4, missing_rate=0.3),
        )
        spec = specs()
        normal_missing = compute_batch_stat(normal, spec).numeric["income_score"].missing
        drifted_missing = compute_batch_stat(drifted, spec).numeric["income_score"].missing
        self.assertGreater(drifted_missing, normal_missing)


if __name__ == "__main__":
    unittest.main()
