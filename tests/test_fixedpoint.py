import unittest
from decimal import Decimal

from gsb.canon import canonical_bytes
from gsb.fixedpoint import DelayAccumulator, Scale, derive_margin


class DelayCarryTests(unittest.TestCase):
    def test_alpha_sequence_matches_seed(self):
        scale = Scale.make("100", "ROUND_FLOOR", "100ms")
        acc = DelayAccumulator("rep-alpha", scale)
        for delta in (43, 91, 120, 67):
            acc.add(delta)
        result = acc.result()
        self.assertEqual(result["delay_exact_ms"], "321")
        self.assertEqual(result["delay_fixed_units"], 3)
        self.assertEqual(result["delay_fixed_ms"], "300")
        self.assertEqual(result["carried_remainder_ms"], "21")

    def test_carry_participates_in_next_accumulation(self):
        scale = Scale.make("100", "ROUND_FLOOR")
        acc = DelayAccumulator("x", scale)
        first = acc.add(43)
        self.assertEqual(first["cumulative_units"], 0)
        self.assertEqual(first["carried_remainder_ms"], "43")
        second = acc.add(91)  # 43 结转 + 91 = 134 -> 1 单位，余 34
        self.assertEqual(second["cumulative_units"], 1)
        self.assertEqual(second["newly_recognized_units"], 1)
        self.assertEqual(second["carried_remainder_ms"], "34")

    def test_no_double_count_identity(self):
        scale = Scale.make("7", "ROUND_FLOOR")
        acc = DelayAccumulator("x", scale)
        deltas = [1, 13, 29, 8, 44, 2]
        for delta in deltas:
            acc.add(delta)
        result = acc.result()
        reconstructed = Decimal(result["delay_fixed_ms"]) + Decimal(
            result["carried_remainder_ms"]
        )
        self.assertEqual(reconstructed, Decimal(result["delay_exact_ms"]))
        self.assertEqual(result["delay_exact_ms"], str(sum(deltas)))

    def test_bit_identical_repeat(self):
        scale = Scale.make("100", "ROUND_FLOOR")

        def run():
            acc = DelayAccumulator("rep-alpha", scale)
            for delta in (43, 91, 120, 67):
                acc.add(delta, txn_id=1000 + delta)
            return canonical_bytes(acc.result())

        self.assertEqual(run(), run())


class MarginErrorBoundTests(unittest.TestCase):
    SCALE = Scale.make("10", "ROUND_CEILING")

    def test_conservative_margin_within_error_bound(self):
        for nbytes in range(0, 4001, 37):
            margin = derive_margin(nbytes, "80", "1.25", "2", self.SCALE)
            self.assertGreaterEqual(margin.margin_ms, margin.exact_ms)
            self.assertGreaterEqual(
                margin.error_bound_ms, margin.margin_ms - margin.exact_ms
            )
            ops = [trace["op"] for trace in margin.trace]
            self.assertTrue(any("div" in op for op in ops))
            self.assertTrue(any("mul" in op for op in ops))
            self.assertTrue(any("add" in op for op in ops))

    def test_finer_scale_reduces_bound(self):
        coarse = derive_margin(3072, "80", "1.25", "2", self.SCALE)
        fine = derive_margin(3072, "80", "1.25", "2", self.SCALE.finer(1000))
        self.assertLess(fine.error_bound_ms, coarse.error_bound_ms)
        self.assertLess(fine.margin_ms, coarse.margin_ms)


if __name__ == "__main__":
    unittest.main()
