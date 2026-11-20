import unittest

from dqueue.backoff import backoff_delay_ms, next_fire_ms
from dqueue.clock import LogicalClock
from dqueue.errors import DQError, ErrorCode
from dqueue.manifest import parse_manifest, parse_policies, payload_digest


POLICIES = parse_policies([
    {"label": "fixed", "base_ms": 500, "multiplier": 2, "cap_ms": 4000,
     "jitter_pct": 0},
    {"label": "jittered", "base_ms": 200, "multiplier": 2, "cap_ms": 3200,
     "jitter_pct": 25},
])


def task(**over):
    payload = over.pop("payload", {"k": 1})
    base = {
        "task_id": "t1", "topic": "orders.created", "fire_ms": 100,
        "policy_label": "fixed", "max_retries": 2,
        "payload": payload, "payload_digest": payload_digest(payload)}
    base.update(over)
    return base


class ManifestValidationTest(unittest.TestCase):
    def test_valid_manifest_normalizes(self):
        tasks = parse_manifest([task()], POLICIES)
        self.assertEqual(tasks[0]["task_id"], "t1")

    def test_duplicate_task_id(self):
        with self.assertRaises(DQError) as ctx:
            parse_manifest([task(), task()], POLICIES)
        self.assertEqual(ctx.exception.code, ErrorCode.E_TASK_DUPLICATE)

    def test_bad_topic(self):
        with self.assertRaises(DQError) as ctx:
            parse_manifest([task(topic="1bad-topic")], POLICIES)
        self.assertEqual(ctx.exception.code, ErrorCode.E_TOPIC_INVALID)

    def test_negative_clock_and_retries(self):
        with self.assertRaises(DQError) as ctx:
            parse_manifest([task(fire_ms=-1)], POLICIES)
        self.assertEqual(ctx.exception.code, ErrorCode.E_CLOCK_INVALID)
        with self.assertRaises(DQError) as ctx:
            parse_manifest([task(max_retries=-2)], POLICIES)
        self.assertEqual(ctx.exception.code, ErrorCode.E_RETRIES_INVALID)

    def test_unknown_policy(self):
        with self.assertRaises(DQError) as ctx:
            parse_manifest([task(policy_label="nope")], POLICIES)
        self.assertEqual(ctx.exception.code, ErrorCode.E_POLICY_NOT_FOUND)

    def test_digest_mismatch(self):
        with self.assertRaises(DQError) as ctx:
            parse_manifest([task(payload_digest="deadbeef")], POLICIES)
        self.assertEqual(
            ctx.exception.code, ErrorCode.E_PAYLOAD_DIGEST_MISMATCH)

    def test_policy_fields_must_be_positive(self):
        with self.assertRaises(DQError) as ctx:
            parse_policies([{"label": "x", "base_ms": 0, "cap_ms": 10}])
        self.assertEqual(ctx.exception.code, ErrorCode.E_POLICY_INVALID)


class BackoffTest(unittest.TestCase):
    def test_fixed_policy_is_pure_exponential(self):
        p = POLICIES["fixed"]
        self.assertEqual(backoff_delay_ms(p, 1, "t"), 500)
        self.assertEqual(backoff_delay_ms(p, 2, "t"), 1000)
        self.assertEqual(backoff_delay_ms(p, 3, "t"), 2000)
        self.assertEqual(backoff_delay_ms(p, 9, "t"), 4000)  # cap

    def test_jitter_is_deterministic_and_bounded(self):
        p = POLICIES["jittered"]
        first = [backoff_delay_ms(p, a, "task-9") for a in range(1, 6)]
        second = [backoff_delay_ms(p, a, "task-9") for a in range(1, 6)]
        self.assertEqual(first, second)
        raw = [200, 400, 800, 1600, 3200]
        for delay, raw_ms in zip(first, raw):
            self.assertGreaterEqual(delay, 1)
            self.assertLessEqual(delay, 3200)
            self.assertTrue(abs(delay - raw_ms) <= raw_ms * 0.25 + 1)

    def test_distinct_tasks_may_differ_but_are_stable(self):
        p = POLICIES["jittered"]
        a = next_fire_ms("alpha", p, 2, 1000)
        b = next_fire_ms("beta", p, 2, 1000)
        self.assertEqual(a, next_fire_ms("alpha", p, 2, 1000))
        self.assertEqual(b, next_fire_ms("beta", p, 2, 1000))


class LogicalClockTest(unittest.TestCase):
    def test_rollback_beyond_tolerance_is_rejected(self):
        clock = LogicalClock(1000, rollback_tolerance_ms=50)
        clock.advance(1000)
        clock.advance(960)  # within tolerance
        with self.assertRaises(DQError) as ctx:
            clock.advance(900)
        self.assertEqual(ctx.exception.code, ErrorCode.E_CLOCK_ROLLBACK)

    def test_non_integer_rejected(self):
        with self.assertRaises(DQError) as ctx:
            LogicalClock().check(1.5)
        self.assertEqual(ctx.exception.code, ErrorCode.E_CLOCK_INVALID)


if __name__ == "__main__":
    unittest.main()
