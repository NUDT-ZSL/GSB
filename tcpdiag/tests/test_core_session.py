import unittest

from workbench.clock import DeterministicClock
from workbench.core import SessionCore
from workbench.errors import E_SEQ, E_STATE, BizError


def make_core(**over):
    config = {"init_cwnd_mss": 4, "init_ssthresh": 16000}
    config.update(over)
    return SessionCore(config=config, clock=DeterministicClock())


def established_core():
    core = make_core()
    core.open()
    core.on_syn_ack(delay_ms=10)
    return core


class TestCoreSession(unittest.TestCase):
    def test_handshake_and_first_rtt_sample(self):
        core = make_core()
        core.open()
        self.assertEqual(core.sm.state, "SYN_SENT")
        core.on_syn_ack(delay_ms=10)
        self.assertEqual(core.sm.state, "ESTABLISHED")
        self.assertEqual(core.rtt.srtt_ms, 10)

    def test_send_and_cumulative_ack(self):
        core = established_core()
        core.send_data(2000)
        self.assertEqual(core.next_seq, 2001)
        core.on_ack(2001, delay_ms=10)  # 累计确认覆盖两段
        self.assertEqual(core.send_una, 2001)
        self.assertEqual(len(core.segments), 0)
        self.assertEqual(core.cc.cwnd, 6000)  # 慢启动：4000 + 2000

    def test_dup_ack_triggers_fast_retransmit(self):
        core = established_core()
        core.send_data(3000)
        for _ in range(3):
            core.on_ack(1, dup=True, delay_ms=5)
        kinds = [e["kind"] for e in core.events]
        self.assertIn("fast_retransmit", kinds)
        retrans = [m for m in core.outbox if m.get("retrans")]
        self.assertEqual(len(retrans), 1)
        self.assertEqual(retrans[0]["seq"], 1)
        self.assertEqual(core.cc.ssthresh, 2000)
        self.assertEqual(core.cc.cwnd, 2000)
        self.assertEqual(core.send_una, 1)  # 重复确认不推进确认点

    def test_timeout_retransmit_and_slow_start(self):
        core = established_core()
        core.send_data(1000)
        core.clock.advance(core.rtt.rto_ms + 1)
        self.assertTrue(core.tick())
        self.assertEqual(core.cc.cwnd, 1000)
        self.assertEqual(core.segments[1]["retrans"], 1)

    def test_karn_excludes_retransmitted_sample(self):
        core = established_core()
        core.send_data(1000)
        srtt_before = core.rtt.srtt_q8
        core.clock.advance(core.rtt.rto_ms + 1)
        core.tick()
        core.on_ack(1001, delay_ms=5)  # 重传段的确认
        kinds = [e["kind"] for e in core.events]
        self.assertIn("karn_excluded", kinds)
        self.assertEqual(core.rtt.srtt_q8, srtt_before)

    def test_ack_out_of_range_raises_e_seq(self):
        core = established_core()
        core.send_data(1000)
        with self.assertRaises(BizError) as ctx:
            core.on_ack(999999, delay_ms=1)
        self.assertEqual(ctx.exception.code, E_SEQ)

    def test_send_before_established_raises_e_state(self):
        core = make_core()
        with self.assertRaises(BizError) as ctx:
            core.send_data(1000)
        self.assertEqual(ctx.exception.code, E_STATE)

    def test_non_positive_rtt_sample_rejected(self):
        core = established_core()
        core.send_data(1000)  # 发送与确认同一虚拟时刻
        core.on_ack(1001, delay_ms=0)
        kinds = [e["kind"] for e in core.events]
        self.assertIn("rtt_rejected", kinds)

    def test_close_path(self):
        core = established_core()
        core.close()
        self.assertEqual(core.sm.state, "FIN_WAIT")
        core.on_fin(delay_ms=5)
        self.assertEqual(core.sm.state, "CLOSE_WAIT")
        core.on_fin_ack(delay_ms=5)
        self.assertEqual(core.sm.state, "CLOSED")


if __name__ == "__main__":
    unittest.main()
