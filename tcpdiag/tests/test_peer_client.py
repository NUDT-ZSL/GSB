"""本地回环集成测试：真实 socket + 确定性虚拟时钟。"""

import json
import unittest
from pathlib import Path

from workbench.client import SessionClient
from workbench.clock import DeterministicClock
from workbench.core import SessionCore
from workbench.peer import ScriptedPeer

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def run_session(script_name, config, drive):
    script = json.loads((SCRIPTS / script_name).read_text(encoding="utf-8"))
    peer = ScriptedPeer(script)
    peer.start()
    peer.ready.wait(2.0)
    core = SessionCore(config=config, clock=DeterministicClock(),
                       script_name=script["name"])
    client = SessionClient(core, port=peer.port)
    try:
        client.connect()
        client.pump()
        drive(core, client)
    finally:
        client.shutdown()
        peer.stop()
    return core, peer


class TestLoopbackSession(unittest.TestCase):
    def test_normal_session_over_loopback(self):
        def drive(core, client):
            remaining = 4000
            while remaining:
                remaining = client.send_data(remaining)
                client.pump()
            client.close()
            client.pump()

        core, peer = run_session(
            "peer_normal.json",
            {"init_cwnd_mss": 2, "init_ssthresh": 4000}, drive)
        self.assertEqual(core.sm.state, "CLOSED")
        self.assertEqual(core.send_una, 4001)
        self.assertEqual(core.next_seq - core.send_una, 0)
        self.assertTrue(peer.truth_log)  # 真值来源非空
        states = [e["new"] for e in core.events if e["kind"] == "state"]
        self.assertEqual(states, ["SYN_SENT", "ESTABLISHED", "FIN_WAIT",
                                  "CLOSE_WAIT", "CLOSED"])

    def test_loss_and_dup_ack_over_loopback(self):
        def drive(core, client):
            client.send_data(4000)
            client.pump()  # 3 次重复确认 -> 快速重传
            client.pump()  # 累计确认 4001
            client.send_data(2000)
            client.pump()  # 两段均被丢弃
            core.clock.advance(core.rtt.rto_ms + 1)
            client.tick()
            client.pump()
            core.clock.advance(core.rtt.rto_ms + 1)
            client.tick()
            client.pump()
            client.close()
            client.pump()

        core, _ = run_session(
            "peer_loss_dupack.json",
            {"init_cwnd_mss": 4, "init_ssthresh": 16000}, drive)
        kinds = [e["kind"] for e in core.events]
        self.assertIn("fast_retransmit", kinds)
        self.assertEqual(kinds.count("timeout"), 2)
        self.assertIn("karn_excluded", kinds)
        self.assertEqual(core.sm.state, "CLOSED")
        self.assertEqual(core.send_una, 6001)


if __name__ == "__main__":
    unittest.main()
