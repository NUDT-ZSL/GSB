import unittest

from layout_order.config import AppConfig
from layout_order.edges import build_candidates
from layout_order.generator import generate_page
from layout_order.geometry import normalize_page, parse_page
from layout_order.metrics import (
    adjacent_pair_accuracy,
    deviation_spectrum,
    evaluate_page,
    ground_truth_order,
    select_reference_subset,
)
from layout_order.solvers import solve_greedy


class MetricsTests(unittest.TestCase):
    def test_deviation_spectrum_buckets_and_pair_accuracy(self) -> None:
        truth = ["a", "b", "c", "d"]
        order = ["b", "a", "c", "d"]
        spectrum = deviation_spectrum(order, truth)
        self.assertEqual(spectrum["0-0"], 2)
        self.assertEqual(spectrum["1-1"], 2)
        accuracy, hits, total = adjacent_pair_accuracy(order, truth)
        self.assertEqual((hits, total), (1, 3))
        self.assertEqual(accuracy, round(1 / 3, 9))

    def test_reference_available_on_normal_pages_and_matches_exact(self) -> None:
        config = AppConfig()
        record = generate_page("two_column", 20, 31, "ref")
        features = normalize_page(parse_page(record))
        graph = build_candidates(features, config.weights)
        order = solve_greedy(features, graph).order
        metrics = evaluate_page(features, order, config)
        reference = metrics.reference
        self.assertTrue(reference["reference_available"])
        self.assertGreaterEqual(reference["subset_size"], 2)
        self.assertIn("reference_deviation_spectrum", reference)
        self.assertIn("reference_adjacent_pair_accuracy", reference)

    def test_reference_subset_is_deterministic_and_bounded(self) -> None:
        config = AppConfig()
        record = generate_page("three_column", 40, 41, "sub")
        features = normalize_page(parse_page(record))
        first = select_reference_subset(features, config.policy.band_max_subset)
        second = select_reference_subset(features, config.policy.band_max_subset)
        self.assertEqual(
            [feature.block_id for feature in first],
            [feature.block_id for feature in second],
        )
        self.assertLessEqual(len(first), config.policy.band_max_subset)

    def test_reference_missing_reason_recorded_for_single_block_page(self) -> None:
        config = AppConfig()
        record = generate_page("single_column", 3, 5, "tiny")
        # 手工裁剪为单块页，验证“规模不足无法取参考值”必须显式记录原因。
        record["blocks"] = [
            block
            for block in record["blocks"]
            if block["reading_index"] == min(b["reading_index"] for b in record["blocks"])
        ]
        record["blocks"][0]["reading_index"] = 0
        features = normalize_page(parse_page(record))
        metrics = evaluate_page(
            features, [feature.block_id for feature in features], config
        )
        self.assertFalse(metrics.reference["reference_available"])
        self.assertEqual(metrics.reference["reason"], "subset_below_two_blocks")

    def test_ground_truth_order_uses_reading_index(self) -> None:
        record = generate_page("mixed_regions", 12, 7, "gt")
        features = normalize_page(parse_page(record))
        order = ground_truth_order(features)
        indices = [
            next(f.reading_index for f in features if f.block_id == block_id)
            for block_id in order
        ]
        self.assertEqual(indices, sorted(indices))


if __name__ == "__main__":
    unittest.main()
