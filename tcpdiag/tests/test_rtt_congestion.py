import unittest

from workbench.congestion import CongestionController
from workbench.config import DEFAULT_CONFIG
from workbench.errors import E_CLOCK, BizError
from workbench.rtt import RTTEstimator


def cfg(**over):
    c = dict(DEFAULT_CONFIG)
    c.update(over)
    return c


class TestRTT(unittest.TestCase):
    def test_initial_rto_before_any_sample(self):
        rtt = RTTEstimator(cfg())
        self.assertEqual(rtt.rto_ms, 500)  # 样本不足 -> initial_rto_ms

    def test_first_sample(self):
        rtt = RTTEstimator(cfg())
        rtt.add_sample(100)
        self.assertEqual(rtt.srtt_ms, 100)
        self.assertEqual(rtt.rttvar_ms, 50)
        self.assertEqual(rtt.rto_ms, 300)  # 100 + max(1, 4*50)

    def test_second_sample_fixed_point(self):
        rtt = RTTEstimator(cfg())
        rtt.add_sample(100)
        rtt.add_sample(120)
        self.assertEqual(rtt.srtt_q8, 820)    # (7*800 + 960) >> 3
        self.assertEqual(rtt.rttvar_q4, 170)  # (3*200 + 80) >> 2
        self.assertEqual(rtt.rto_ms, 272)     # 102 + max(1, 170)

    def test_non_positive_sample_rejected(self):
        rtt = RTTEstimator(cfg())
        with self.assertRaises(BizError) as ctx:
            rtt.add_sample(0)
        self.assertEqual(ctx.exception.code, E_CLOCK)
        self.assertIsNone(rtt.srtt_q8)

    def test_rto_bounds(self):
        rtt = RTTEstimator(cfg(min_rto_ms=100))
        rtt.add_sample(1)
        self.assertEqual(rtt.rto_ms, 100)  # 下界夹取


class TestCongestion(unittest.TestCase):
    def test_slow_start_grows_per_acked_byte(self):
        cc = CongestionController(cfg(init_cwnd_mss=1, init_ssthresh=65535))
        cc.on_new_ack(1000)
        self.assertEqual(cc.cwnd, 2000)
        cc.on_new_ack(500)
        self.assertEqual(cc.cwnd, 2500)
        self.assertEqual(cc.phase, "slow_start")

    def test_congestion_avoidance_integer_rule(self):
        cc = CongestionController(cfg(init_cwnd_mss=8, init_ssthresh=4000))
        cc.on_new_ack(1000)
        self.assertEqual(cc.cwnd, 8125)  # 8000 + (1000*1000)//8000
        self.assertEqual(cc.phase, "congestion_avoidance")

    def test_fast_retransmit_on_third_dup(self):
        cc = CongestionController(cfg(init_cwnd_mss=8, init_ssthresh=65535))
        self.assertFalse(cc.on_dup_ack())
        self.assertFalse(cc.on_dup_ack())
        self.assertTrue(cc.on_dup_ack())
        self.assertEqual(cc.ssthresh, 4000)
        self.assertEqual(cc.cwnd, 4000)

    def test_timeout_returns_to_slow_start(self):
        cc = CongestionController(cfg(init_cwnd_mss=8, init_ssthresh=65535))
        cc.on_timeout()
        self.assertEqual(cc.cwnd, 1000)
        self.assertEqual(cc.ssthresh, 4000)
        self.assertEqual(cc.phase, "slow_start")

    def test_new_ack_resets_dup_count(self):
        cc = CongestionController(cfg(init_cwnd_mss=4, init_ssthresh=65535))
        cc.on_dup_ack()
        cc.on_dup_ack()
        cc.on_new_ack(1000)
        self.assertEqual(cc.dup_ack_count, 0)


if __name__ == "__main__":
    unittest.main()
