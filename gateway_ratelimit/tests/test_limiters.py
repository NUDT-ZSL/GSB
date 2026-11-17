import unittest

from gateway.limiters import (
    SlidingWindowCounter,
    SlidingWindowLog,
    TokenBucket,
)
from gateway.models import (
    PATH_TOKEN_BUCKET,
    PATH_WINDOW_COUNTER,
    PATH_WINDOW_LOG,
    Route,
)
from gateway.limiters import LimiterSet


def route(**overrides) -> Route:
    base = dict(
        name="r",
        prefix="/r",
        auth_policy="token",
        rate_per_sec=10,
        burst=2,
        window_ms=500,
        max_in_window=2,
    )
    base.update(overrides)
    return Route(**base)


class TokenBucketTest(unittest.TestCase):
    def test_burst_then_refill_with_floor_truncation(self) -> None:
        bucket = TokenBucket(rate_per_sec=10, burst=2)
        self.assertTrue(bucket.admit(0).allow)
        self.assertTrue(bucket.admit(0).allow)
        # Bucket empty: 49ms * 10 / 1000 = 0 (floor), still empty.
        self.assertFalse(bucket.admit(49).allow)
        # Anchor stays at t=0; at t=100 the integer refill yields one token.
        self.assertTrue(bucket.admit(100).allow)
        self.assertFalse(bucket.admit(100).allow)

    def test_cap_never_exceeds_burst(self) -> None:
        bucket = TokenBucket(rate_per_sec=100, burst=3)
        bucket.admit(0)
        bucket.admit(0)
        decision = bucket.admit(10_000)
        self.assertTrue(decision.allow)
        self.assertEqual(bucket.tokens, 2)


class SlidingWindowLogTest(unittest.TestCase):
    def test_exact_window_boundary_slides_out(self) -> None:
        log = SlidingWindowLog(window_ms=500, max_in_window=2)
        self.assertTrue(log.admit(0).allow)
        self.assertTrue(log.admit(400).allow)
        self.assertFalse(log.admit(450).allow)
        # t=500: the t=0 entry has age exactly window_ms and slides out.
        self.assertTrue(log.admit(500).allow)

    def test_snapshot_restore_roundtrip(self) -> None:
        log = SlidingWindowLog(500, 2)
        log.admit(10)
        log.admit(20)
        clone = SlidingWindowLog(500, 2)
        clone.restore(log.snapshot())
        self.assertEqual(clone.log, log.log)


class SlidingWindowCounterTest(unittest.TestCase):
    def test_epoch_alignment_and_weighted_estimate(self) -> None:
        counter = SlidingWindowCounter(window_ms=500, max_in_window=2)
        self.assertTrue(counter.admit(0).allow)
        self.assertTrue(counter.admit(499).allow)
        # New epoch; previous bucket count 2 with offset 1 -> weight (499/500)
        # floors to 1, so estimate already reaches the limit.
        denied = counter.admit(500)
        self.assertFalse(denied.allow)
        self.assertIn("aligned bucket=1", denied.detail)

    def test_zero_window_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SlidingWindowCounter(window_ms=0, max_in_window=2)


class LimiterSetTest(unittest.TestCase):
    def test_all_three_paths_emit_decisions_in_fixed_order(self) -> None:
        limiter_set = LimiterSet(route())
        decisions = limiter_set.decide(0)
        self.assertEqual(
            list(decisions),
            [PATH_TOKEN_BUCKET, PATH_WINDOW_COUNTER, PATH_WINDOW_LOG],
        )
        snapshot = limiter_set.snapshot()
        clone = LimiterSet(route())
        clone.restore(snapshot)
        self.assertEqual(clone.snapshot(), snapshot)


if __name__ == "__main__":
    unittest.main()
