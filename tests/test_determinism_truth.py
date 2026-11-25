from __future__ import annotations

import unittest

from radapt.aggregate import find_first_divergence
from radapt.generator import generate_batch
from radapt.parsing import parse_bundle
from radapt.sample import sample_raw


class DeterminismTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        raw = sample_raw()
        self.bundle = parse_bundle(raw["table"], raw["plan"], raw["arrivals"], raw["config"])
        self.table = self.bundle.tables[0]

    def _batches(self):
        return [generate_batch(self.table, self.table.global_seed, spec) for spec in self.bundle.arrivals.batches]

    def test_batch_digests_are_bitwise_reproducible(self) -> None:
        first = self._batches()
        second = self._batches()
        self.assertEqual([x.summary.to_dict() for x in first], [x.summary.to_dict() for x in second])
        for left, right in zip(first, second):
            for name in left.columns:
                self.assertEqual(left.columns[name].tobytes(), right.columns[name].tobytes())

    def test_hash_and_sort_match_exact_truth(self) -> None:
        batches = self._batches()
        for window in self.bundle.plan.windows:
            envelopes = [batch for batch in batches if batch.spec.window_id == window.window_id]
            for operator in ("hash", "sort"):
                with self.subTest(window=window.window_id, operator=operator):
                    divergence = find_first_divergence(
                        window,
                        self.table,
                        envelopes,
                        stream_operator=operator,
                    )
                    self.assertIsNone(divergence)

    def test_first_divergence_reports_batch_and_intermediate_values(self) -> None:
        window = self.bundle.plan.window("w1")
        envelopes = [batch for batch in self._batches() if batch.spec.window_id == "w1"]
        report = find_first_divergence(
            window,
            self.table,
            envelopes,
            stream_operator="hash",
            omit_row_predicate=lambda seq: seq == 2,
        )
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report["first_divergent_batch"], 2)
        self.assertEqual(report["code"], "E_AGGREGATE_VALUE_MISMATCH")
        self.assertIn("expected", report)
        self.assertIn("actual", report)
        self.assertIn("intermediate_values", report)
        self.assertEqual(report["stream_rows"], report["truth_rows"] - 1)


if __name__ == "__main__":
    unittest.main()
