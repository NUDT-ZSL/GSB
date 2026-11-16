import unittest

from sre_guard.degrade import choose_degrade, deterministic_selector
from sre_guard.errors import E_DEGRADE, GovernanceError
from sre_guard.fixedpoint import Grid
from sre_guard.models import DegradeTarget


class DegradeTests(unittest.TestCase):
    def setUp(self):
        self.grid = Grid.from_exponent(4)
        self.chain = [
            DegradeTarget("a", 70, 995),
            DegradeTarget("b", 25, 900),
            DegradeTarget("c", 5, 700),
        ]

    def test_selector_is_deterministic_and_order_based(self):
        first = deterministic_selector("svc", 3, 1000, 1)
        second = deterministic_selector("svc", 3, 1000, 1)
        self.assertEqual(first, second)

    def test_weighted_choice_and_error_trace(self):
        decision = choose_degrade(
            "svc", 0, 1000, self.chain, self.grid,
            is_available=lambda target: True,
            observed_success=lambda target: None)
        self.assertIn(decision.selected_target, {"a", "b", "c"})
        self.assertEqual(decision.levels[0].level, 1)
        # 单级：一次乘法截断 = 1 ulp
        self.assertEqual(decision.total_error_ulp, 1)
        self.assertFalse(decision.error_dominated)

    def test_fallback_through_unavailable_targets(self):
        # 第一级选中的目标若不可用则移除并继续，最终成功
        decision = choose_degrade(
            "svc", 5, 2000, self.chain, self.grid,
            is_available=lambda target: target == "c",
            observed_success=lambda target: None)
        self.assertEqual(decision.selected_target, "c")
        self.assertGreaterEqual(len(decision.levels), 2)

    def test_chain_exhausted_raises_e_degrade(self):
        with self.assertRaises(GovernanceError) as ctx:
            choose_degrade(
                "svc", 1, 1000, self.chain, self.grid,
                is_available=lambda target: False,
                observed_success=lambda target: None)
        self.assertEqual(ctx.exception.code, E_DEGRADE)

    def test_zero_weight_sum_raises_e_degrade(self):
        chain = [DegradeTarget("a", 0, 1000)]
        with self.assertRaises(GovernanceError) as ctx:
            choose_degrade(
                "svc", 1, 1000, chain, self.grid,
                is_available=lambda target: True,
                observed_success=lambda target: None)
        self.assertEqual(ctx.exception.code, E_DEGRADE)

    def test_observed_success_quantization_adds_input_error(self):
        # 观测成功概率现场量化，输入量化误差计入
        decision = choose_degrade(
            "svc", 2, 3000, self.chain, self.grid,
            is_available=lambda target: True,
            observed_success=lambda target: (9000, 1))
        self.assertEqual(decision.error_trace[0].input_quant_ulp, 1)
        self.assertEqual(decision.total_error_ulp, 2)  # 输入量化 1 + 截断 1


if __name__ == "__main__":
    unittest.main()
