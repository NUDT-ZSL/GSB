from __future__ import annotations

import unittest

from patrol_stats.statistics import build_equal_depth_histogram


class EqualDepthHistogramTests(unittest.TestCase):
    def test_bucket_counts_are_exact_and_split_sorted_values(self) -> None:
        buckets = build_equal_depth_histogram(range(1, 21), 5)
        self.assertEqual(len(buckets), 5)
        self.assertEqual(sum(bucket["observed_count"] for bucket in buckets), 20)
        self.assertEqual(
            [bucket["observed_count"] for bucket in buckets], [4, 4, 4, 4, 4]
        )
        self.assertEqual(buckets[0]["lower_value"], 1.0)
        self.assertEqual(buckets[-1]["upper_value"], 20.0)
        for index, bucket in enumerate(buckets):
            self.assertEqual(bucket["bucket_no"], index)
            self.assertEqual(bucket["lower_inclusive"], 1 if index == 0 else 0)
            self.assertEqual(bucket["upper_inclusive"], 1)

    def test_duplicate_boundaries_do_not_lose_rows(self) -> None:
        buckets = build_equal_depth_histogram([1] * 8 + [2] * 4 + [3] * 2, 8)
        self.assertEqual(sum(bucket["observed_count"] for bucket in buckets), 14)
        self.assertEqual([bucket["observed_count"] for bucket in buckets], [8, 4, 2])
        self.assertEqual([bucket["ndv"] for bucket in buckets], [1, 1, 1])

    def test_single_value_produces_one_zero_width_bucket(self) -> None:
        buckets = build_equal_depth_histogram([7, 7, 7], 10)
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["lower_value"], 7.0)
        self.assertEqual(buckets[0]["upper_value"], 7.0)
        self.assertEqual(buckets[0]["observed_count"], 3)


if __name__ == "__main__":
    unittest.main()
