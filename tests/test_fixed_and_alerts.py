from __future__ import annotations

import unittest
from dataclasses import replace
from fractions import Fraction

from sre_burnrate import Event, FullWindowAggregator, compute_budget_metrics, compute_burn_rate, quantize
from sre_burnrate.alerts import AlertStateMachine, CRITICAL, NORMAL, WARNING
from sre_burnrate.models import minute_to_iso

from tests.helpers import assert_no_floats, make_config


class FixedPointTests(unittest.TestCase):
    def test_quantization_and_error_bound(self) -> None:
        value = quantize(Fraction(1, 3), 1_000_000_000)
        self.assertEqual(value.ticks, 333_333_333)
        self.assertEqual(value.error_upper_ticks, 1)
        self.assertEqual(value.value, "0.333333333")
        self.assertEqual(value.error_upper, "0.000000001")
        exact = quantize(Fraction(1, 2), 1_000_000_000)
        self.assertEqual(exact.ticks, 500_000_000)
        self.assertEqual(exact.error_upper_ticks, 0)
        negative = quantize(Fraction(-1, 3), 1_000_000_000)
        self.assertEqual(negative.ticks, -333_333_333)
        self.assertEqual(negative.error_upper_ticks, 1)

    def test_budget_and_burn_formula(self) -> None:
        config = make_config(long_minutes=1, short_minutes=1, step_minutes=1, target_ppm=999000)
        result = FullWindowAggregator(config).aggregate([Event(0, 1000, 2, 1)])
        window = result.long_windows[0]
        self.assertEqual(window.sli.value, "0.998000000")
        self.assertEqual(window.error_rate.value, "0.002000000")
        self.assertEqual(window.budget_fraction.value, "0.001000000")
        self.assertEqual(window.budget_total.ticks, 1_000_000)
        self.assertEqual(window.budget_consumed.ticks, 2_000_000)
        self.assertEqual(window.budget_remaining.ticks, -1_000_000)
        self.assertEqual(window.burn_rate.value, "2.000000")
        self.assertEqual(window.burn_rate.error_upper_ticks, 0)
        assert_no_floats(self, window.to_dict())

    def test_standalone_budget_api_reuses_exact_formula(self) -> None:
        metrics = compute_budget_metrics(1000, 2, 999000)
        self.assertEqual(metrics.sli.value, "0.998000000")
        self.assertEqual(metrics.budget_total.ticks, 1_000_000)
        self.assertEqual(metrics.budget_remaining.ticks, -1_000_000)
        self.assertEqual(compute_burn_rate(1000, 2, 999000).value, "2.000000")

    def test_undefined_metrics_are_explicit(self) -> None:
        config = make_config(long_minutes=1, short_minutes=1, step_minutes=1)
        window = FullWindowAggregator(config).aggregate([Event(0, 0, 0, 1)]).long_windows[0]
        self.assertFalse(window.sli.defined)
        self.assertFalse(window.burn_rate.defined)
        self.assertIsNone(window.sli.ticks)
        self.assertIsNone(window.burn_rate.error_upper_ticks)
        self.assertTrue(window.budget_total.defined)


class AlertStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = make_config(long_minutes=1, short_minutes=1, step_minutes=1, threshold=10)
        base = FullWindowAggregator(self.config).aggregate([Event(0, 100, 0, 1)])
        self.base_long = base.long_windows[0]
        self.base_short = base.short_windows[0]

    def _window(self, kind: str, index: int, burn: int):
        base = self.base_long if kind == "long" else self.base_short
        start = index * 60
        return replace(
            base,
            window_id=f"{kind}-{index + 1:06d}",
            start_minute=start,
            end_minute_exclusive=start + 60,
            start_iso=minute_to_iso(start),
            end_iso_exclusive=minute_to_iso(start + 60),
            burn_rate=quantize(Fraction(burn, 1), self.config.precision.burn_scale),
            exact_burn_rate=Fraction(burn, 1),
        )

    def test_three_level_state_sequence_and_transitions(self) -> None:
        pairs = [(0, 0), (3, 3), (20, 20), (1, 1)]
        long_windows = tuple(self._window("long", index, value[0]) for index, value in enumerate(pairs))
        short_windows = tuple(self._window("short", index, value[1]) for index, value in enumerate(pairs))
        evaluation = AlertStateMachine(self.config).evaluate(long_windows, short_windows)
        self.assertEqual([step.state for step in evaluation.steps], [NORMAL, WARNING, CRITICAL, NORMAL])
        self.assertEqual(
            [(transition.from_state, transition.to_state) for transition in evaluation.transitions],
            [(NORMAL, WARNING), (WARNING, CRITICAL), (CRITICAL, NORMAL)],
        )
        self.assertEqual(evaluation.transitions[1].trigger_window_ids, ("long-000003", "short-000003"))
        self.assertEqual(evaluation.transitions[1].slo_version, "slo-test-v1")
        self.assertEqual(evaluation.final_state, NORMAL)

    def test_both_windows_must_cross_threshold(self) -> None:
        pairs = [(20, 1), (1, 20)]
        long_windows = tuple(self._window("long", index, value[0]) for index, value in enumerate(pairs))
        short_windows = tuple(self._window("short", index, value[1]) for index, value in enumerate(pairs))
        evaluation = AlertStateMachine(self.config).evaluate(long_windows, short_windows)
        self.assertEqual([step.state for step in evaluation.steps], [NORMAL, NORMAL])
        self.assertEqual(evaluation.transitions, ())

    def test_repeated_evaluation_is_identical(self) -> None:
        pairs = [(0, 0), (3, 3), (20, 20), (1, 1)]
        long_windows = tuple(self._window("long", index, value[0]) for index, value in enumerate(pairs))
        short_windows = tuple(self._window("short", index, value[1]) for index, value in enumerate(pairs))
        first = AlertStateMachine(self.config).evaluate(long_windows, short_windows)
        second = AlertStateMachine(self.config).evaluate(long_windows, short_windows)
        self.assertEqual(
            [step.to_dict() for step in first.steps], [step.to_dict() for step in second.steps]
        )
        self.assertEqual(
            [record.to_dict() for record in first.records],
            [record.to_dict() for record in second.records],
        )
        self.assertEqual(
            [transition.to_dict() for transition in first.transitions],
            [transition.to_dict() for transition in second.transitions],
        )


if __name__ == "__main__":
    unittest.main()
