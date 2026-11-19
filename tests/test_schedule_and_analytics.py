from __future__ import annotations

import unittest
from decimal import Decimal

from lbdiag.analytics import (
    advance_instance_state,
    build_sensitivity_payload,
    build_window_payload,
    initial_state_row,
)
from lbdiag.fixed import fixed_record, quantize_fixed
from lbdiag.model import parse_sample, schedule_checks

from .helpers import clone, sample_raw


def fake_check(index: int, classification: str) -> dict:
    return {
        "instance_id": "unused",
        "check_index": index,
        "scheduled_at_ms": 1_000_000 + index * 1000,
        "jitter_offset_ms": 0,
        "classification": classification,
        "error_code": "E_OK" if classification == "success" else "E_TEST",
        "http_status": 200,
        "expected_status": 200,
        "body_sha256": None,
        "expected_body_sha256": None,
        "response_bytes": 2,
        "abort_requested": False,
        "detail_code": None,
    }


def manual_window(instance_id: str, score: str, weight: str, factor: str = "1") -> dict:
    scale = 1_000_000
    return {
        "instance_id": instance_id,
        "window_index": 0,
        "window_version": "v1:manual",
        "health_score_fixed": fixed_record(Decimal(score), scale, quantity="health_score"),
        "effective_weight_fixed": fixed_record(
            Decimal(weight), scale, quantity="effective_weight"
        ),
        "slow_start_factor_fixed": fixed_record(
            Decimal(factor), scale, quantity="slow_start_factor"
        ),
    }


class ScheduleTests(unittest.TestCase):
    def test_schedule_is_bitwise_deterministic(self) -> None:
        config = parse_sample(sample_raw())
        first = schedule_checks(config)
        second = schedule_checks(config)
        self.assertEqual(first, second)
        for instance_id, checks in first.items():
            self.assertEqual(len(checks), config.plan.rounds)
            boundary = Decimal(config.plan.jitter_ratio) * config.plan.interval_ms
            for check in checks:
                self.assertLessEqual(abs(check.jitter_offset_ms), int(boundary) + 1)
                self.assertEqual(
                    check.scheduled_at_ms,
                    config.diagnostics.logical_clock_start_ms
                    + check.check_index * config.plan.interval_ms
                    + check.jitter_offset_ms,
                )

    def test_logic_clock_rollback_error(self) -> None:
        from lbdiag.engine import LogicalClock
        from lbdiag.errors import E_CLOCK_ROLLBACK, BusinessError

        clock = LogicalClock(
            now_ms=100,
            step_ms=10,
            rollback_tolerance_ms=2,
            high_water_ms=100,
        )
        clock.advance_to(150)
        with self.assertRaises(BusinessError) as caught:
            clock.check_rollback(147)
        self.assertEqual(caught.exception.code, E_CLOCK_ROLLBACK)


class AnalyticsTests(unittest.TestCase):
    def test_fixed_point_half_even(self) -> None:
        self.assertEqual(quantize_fixed(Decimal("0.0000005"), 1_000_000)[0], 0)
        self.assertEqual(quantize_fixed(Decimal("0.0000015"), 1_000_000)[0], 2)

    def test_window_rate_score_quantile_and_fixed_trail(self) -> None:
        config = parse_sample(sample_raw())
        alpha = config.instance("alpha")
        state = initial_state_row(
            alpha, config.plan, config.diagnostics.fixed_scale
        )
        results = [
            fake_check(i, "status_mismatch" if i < 2 else "success") for i in range(8)
        ]
        payload, new_state = build_window_payload(
            config, alpha, 0, state, results, results
        )
        self.assertEqual(payload["success_count"], 6)
        self.assertEqual(payload["success_rate_fixed"]["fixed"], 750000)
        self.assertEqual(payload["health_score_fixed"]["fixed"], 980000)
        self.assertEqual(payload["health_score_raw"], 0.98)
        self.assertEqual(payload["quality_quantiles"]["p50"]["fixed"], 1000000)
        self.assertEqual(payload["rounding_mode"], "ROUND_HALF_EVEN")
        self.assertEqual(payload["rounding_trail"][0]["scale"], 1000000)
        self.assertIn("raw_float", payload["rounding_trail"][0])
        self.assertEqual(new_state["applied_check_count"], 8)
        self.assertEqual(payload["state_after"], "HEALTHY")

    def test_sensitivity_true_for_equal_weights(self) -> None:
        config = parse_sample(sample_raw())
        window_a = manual_window("alpha", "1.00", "10.00")
        window_b = manual_window("beta", "0.98", "9.80")
        payload = build_sensitivity_payload(config, window_a, window_b, 0)
        self.assertTrue(payload["precision_sensitive"])
        self.assertTrue(payload["allocation_depends_on_score_difference"])
        self.assertLess(
            payload["score_difference"]["raw_float"],
            payload["tolerance"]["raw_float"],
        )

    def test_no_false_positive_when_ordering_is_independent(self) -> None:
        raw = clone(sample_raw())
        raw["diagnostics"].pop("sample_sha256", None)
        raw["instances"][0]["weight"] = 100
        raw["instances"][1]["weight"] = 1
        config = parse_sample(raw)
        window_a = manual_window("alpha", "1.00", "100.00")
        window_b = manual_window("beta", "0.98", "0.98")
        payload = build_sensitivity_payload(config, window_a, window_b, 0)
        self.assertFalse(payload["precision_sensitive"])
        self.assertFalse(payload["allocation_depends_on_score_difference"])
        self.assertIn(
            "ORDERING_INDEPENDENT_OF_SCORE_DIFFERENCE", payload["reasons"]
        )

    def test_zero_weight_is_not_marked(self) -> None:
        config = parse_sample(sample_raw())
        window_a = manual_window("alpha", "0", "0")
        window_b = manual_window("beta", "0.001", "0.001")
        payload = build_sensitivity_payload(config, window_a, window_b, 0)
        self.assertFalse(payload["precision_sensitive"])
        self.assertIn("ZERO_ALLOCATION_WEIGHT", payload["reasons"])

    def test_identical_scores_are_not_marked(self) -> None:
        config = parse_sample(sample_raw())
        window_a = manual_window("alpha", "1.00", "10.00")
        window_b = manual_window("beta", "1.00", "10.00")
        payload = build_sensitivity_payload(config, window_a, window_b, 0)
        self.assertFalse(payload["precision_sensitive"])
        self.assertIn("IDENTICAL_SCORES_NO_DELTA", payload["reasons"])

    def test_unhealthy_recovery_and_slow_start_weight(self) -> None:
        config = parse_sample(sample_raw())
        alpha = config.instance("alpha")
        state = {
            "instance_id": "alpha",
            "state": "UNHEALTHY",
            "consecutive_failures": 3,
            "consecutive_successes": 0,
            "recovery_age": 0,
            "weight_factor_raw": Decimal("0"),
            "effective_weight_raw": Decimal("0"),
            "effective_weight_fixed": 0,
            "applied_check_count": 0,
            "last_check_index": -1,
            "logical_watermark_ms": 0,
        }
        first_window = [fake_check(i, "success") for i in range(8)]
        state, transition = advance_instance_state(
            state,
            alpha,
            config.plan,
            first_window,
            first_window,
            Decimal("1"),
            Decimal("1"),
            config.diagnostics.fixed_scale,
        )
        self.assertEqual(transition["previous_state"], "UNHEALTHY")
        self.assertEqual(state["state"], "SUSPECT")
        self.assertEqual(state["weight_factor_raw"], config.plan.slow_start.suspect_ratio)

        second_window = [fake_check(i, "success") for i in range(4, 12)]
        state, transition = advance_instance_state(
            state,
            alpha,
            config.plan,
            second_window,
            second_window[4:],
            Decimal("1"),
            Decimal("1"),
            config.diagnostics.fixed_scale,
        )
        self.assertEqual(state["state"], "HEALTHY")
        self.assertEqual(state["recovery_age"], 0)
        self.assertEqual(
            state["weight_factor_raw"], config.plan.slow_start.initial_ratio
        )
        self.assertEqual(state["effective_weight_raw"], Decimal("2.500000"))

        third_window = [fake_check(i, "success") for i in range(8, 16)]
        state, _ = advance_instance_state(
            state,
            alpha,
            config.plan,
            third_window,
            third_window[4:],
            Decimal("1"),
            Decimal("1"),
            config.diagnostics.fixed_scale,
        )
        self.assertEqual(state["state"], "HEALTHY")
        self.assertEqual(state["recovery_age"], 1)
        self.assertEqual(state["effective_weight_raw"], Decimal("4.375000"))


if __name__ == "__main__":
    unittest.main()

