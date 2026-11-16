import unittest

from sre_guard.breaker import STATE_CLOSED, STATE_HALF_OPEN, STATE_OPEN, CircuitBreaker
from sre_guard.fixedpoint import Grid
from sre_guard.models import BreakerPolicy


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.grid = Grid.from_exponent(4)
        self.policy = BreakerPolicy(
            window_minutes=1, failure_threshold_percent=50,
            min_request_count=4, half_open_probe=2, cooldown_ms=30_000)
        self.breaker = CircuitBreaker("svc", self.grid, self.policy)

    def test_opens_when_threshold_reached(self):
        results = ["success", "failure", "timeout", "failure"]  # 3/4 失败
        for index, result in enumerate(results):
            admitted, _ = self.breaker.admit(index * 1000)
            self.assertTrue(admitted)
            self.breaker.record_result(index * 1000, result)
        self.assertEqual(self.breaker.state, STATE_OPEN)
        transition = self.breaker.transitions[-1]
        self.assertEqual(transition.failure_count, 3)
        self.assertEqual(transition.threshold_percent, 50)
        # 定点失败率 0.75，误差上界 1 ulp
        self.assertEqual(transition.failure_rate_fixed, 7500)

    def test_below_min_request_count_keeps_closed(self):
        for index, result in enumerate(["failure", "failure"]):
            self.breaker.admit(index * 1000)
            self.breaker.record_result(index * 1000, result)
        self.assertEqual(self.breaker.state, STATE_CLOSED)

    def test_open_rejects_then_half_open_probes(self):
        for index, result in enumerate(["failure", "failure", "failure", "failure"]):
            self.breaker.admit(index * 1000)
            self.breaker.record_result(index * 1000, result)
        self.assertEqual(self.breaker.state, STATE_OPEN)
        opened_at = self.breaker.opened_at_ms  # 3000ms
        # 冷却期内拒绝
        admitted, _ = self.breaker.admit(opened_at + 10_000)
        self.assertFalse(admitted)
        self.assertEqual(self.breaker.state, STATE_OPEN)
        # 冷却结束进入半开，限制试探数
        admitted, _ = self.breaker.admit(opened_at + 31_000)
        self.assertTrue(admitted)
        self.assertEqual(self.breaker.state, STATE_HALF_OPEN)
        first_probe_ts = opened_at + 31_000
        self.breaker.record_result(first_probe_ts, "success")
        admitted, _ = self.breaker.admit(first_probe_ts + 1_000)
        self.assertTrue(admitted)
        self.breaker.record_result(first_probe_ts + 1_000, "success")
        self.assertEqual(self.breaker.state, STATE_CLOSED)  # 试探成功 -> 关闭

    def test_half_open_failure_reopens(self):
        for index, result in enumerate(["failure", "failure", "failure", "failure"]):
            self.breaker.admit(index * 1000)
            self.breaker.record_result(index * 1000, result)
        probe_ts = self.breaker.opened_at_ms + 31_000
        self.breaker.admit(probe_ts)
        self.breaker.record_result(probe_ts, "failure")
        self.assertEqual(self.breaker.state, STATE_OPEN)
        self.assertEqual(self.breaker.transitions[-1].to_state, STATE_OPEN)

    def test_whole_minute_bucket_eviction(self):
        # 分钟 0 写入失败样本
        for i in range(4):
            self.breaker.admit(i * 1000)
            self.breaker.record_result(i * 1000, "failure")
        self.assertEqual(self.breaker.state, STATE_OPEN)
        # 窗口 1 分钟，分钟 2 时分钟 0 的桶被整体移除
        admitted, detail = self.breaker.admit(2 * 60_000)
        self.assertIn(0, detail["evicted_minutes"])
        self.assertNotIn(0, self.breaker.buckets)

    def test_timeout_and_failure_distinguished(self):
        self.breaker.admit(0)
        self.breaker.record_result(0, "timeout")
        stats = self.breaker.record_result(1000, "failure")
        self.assertEqual(stats["timeout"], 1)
        self.assertEqual(stats["failure"], 1)
        self.assertEqual(stats["total"], 2)


if __name__ == "__main__":
    unittest.main()
