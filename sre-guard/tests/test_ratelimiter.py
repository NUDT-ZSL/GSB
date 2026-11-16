import unittest

from sre_guard.fixedpoint import Grid
from sre_guard.models import RateLimitPolicy
from sre_guard.ratelimiter import TokenBucket


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.grid = Grid.from_exponent(4)
        # 2 令牌/秒，突发 3，冷启动预填 3 令牌
        self.policy = RateLimitPolicy(rate_per_second=2, burst_capacity=3)

    def _bucket(self):
        return TokenBucket.create("svc", self.policy, self.grid)

    def test_burst_pre_fill_and_consume(self):
        bucket = self._bucket()
        decision = bucket.try_consume(0)
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["tokens_after_grid"], 2 * self.grid.scale)

    def test_refill_integer_carry_no_loss(self):
        bucket = self._bucket()
        # 在同一时刻消耗 3 个令牌至 0（同毫秒不补充）
        for _ in range(3):
            bucket.try_consume(0)
        self.assertEqual(bucket.tokens_grid, 0)
        # 推进到 250ms：精确补充 0.5 令牌（5000 格），无余数，仍不足 1 令牌
        refill = bucket.refill(250)
        self.assertEqual(refill["refill_grid"], 5000)
        self.assertEqual(bucket.tokens_grid, 5000)
        # 再推进 250ms：累计补充 1 令牌，可消耗
        decision = bucket.try_consume(500)
        self.assertTrue(decision["allowed"])
        self.assertEqual(bucket.tokens_grid, 0)

    def test_fractional_carry_accumulates(self):
        bucket = self._bucket()
        for _ in range(3):
            bucket.try_consume(0)
        # 速率 2/s => 每 100ms 补充 0.2 令牌；三次各 100ms 累计 0.6
        bucket.try_consume(100)
        bucket.try_consume(200)
        bucket.try_consume(300)
        self.assertEqual(bucket.carry_grid, 0)
        self.assertEqual(bucket.tokens_grid, 6000)

    def test_cap_overflow_never_over_issues(self):
        bucket = self._bucket()
        # 同毫秒连续消耗 3 个令牌至 0（同毫秒不补充），时间基准停在 0
        for _ in range(3):
            bucket.try_consume(0)
        self.assertEqual(bucket.tokens_grid, 0)
        # 从时间基准 0 长时间等待，补充量远超容量，必须封顶到突发容量
        decision = bucket.try_consume(1_000_000)
        self.assertTrue(decision["refill"]["capped"])
        # 封顶到容量 3 令牌后本次请求消耗 1 令牌，余量为 2 令牌
        self.assertTrue(decision["allowed"])
        self.assertEqual(bucket.tokens_grid, 2 * self.grid.scale)
        self.assertEqual(decision["error_bound"]["over_issue_ulp"], 0)

    def test_refill_below_capacity_not_capped(self):
        bucket = self._bucket()
        for _ in range(3):
            bucket.try_consume(0)
        # 经过 500ms 精确补充 1 令牌，未触及容量，不封顶
        refill = bucket.refill(500)
        self.assertFalse(refill["capped"])
        self.assertEqual(bucket.tokens_grid, 1 * self.grid.scale)

    def test_deterministic_bit_identical_trajectory(self):
        def run():
            bucket = self._bucket()
            trajectory = []
            for ts in [0, 0, 50, 120, 300, 340, 900, 1200]:
                result = bucket.try_consume(ts)
                trajectory.append((result["allowed"], bucket.tokens_grid,
                                   bucket.carry_grid))
            return trajectory
        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main()
