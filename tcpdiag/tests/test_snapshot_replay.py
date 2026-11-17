import copy
import unittest

from workbench.clock import DeterministicClock
from workbench.core import SessionCore
from workbench.errors import E_REPLAY, E_VERSION, BizError
from workbench.replay import verify_replay
from workbench.snapshot import restore_into, restore_session


def build_session():
    core = SessionCore(config={"init_cwnd_mss": 4},
                       clock=DeterministicClock(), script_name="t")
    core.open()
    core.on_syn_ack(delay_ms=10)
    core.send_data(2000)
    core.on_ack(1001, delay_ms=10)
    core.on_ack(2001, delay_ms=10)
    return core


class TestSnapshotReplay(unittest.TestCase):
    def test_replay_ok(self):
        snap = build_session().snapshot()
        result = verify_replay(snap)
        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], len(snap["inputs"]))

    def test_replay_detects_tampering_with_e_replay(self):
        snap = build_session().snapshot()
        tampered = copy.deepcopy(snap)
        tampered["checkpoints"][3]["cwnd"] += 1
        result = verify_replay(tampered)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], E_REPLAY)
        self.assertEqual(result["field"], "cwnd")
        self.assertEqual(result["input_index"], 3)

    def test_restore_and_continue(self):
        snap = build_session().snapshot()
        core = restore_session(snap, clock=DeterministicClock())
        self.assertEqual(core.sm.state, "ESTABLISHED")
        self.assertEqual(core.send_una, 2001)
        core.send_data(1000)  # 恢复后继续推进新报文
        core.on_ack(3001, delay_ms=5)
        self.assertEqual(core.send_una, 3001)
        self.assertEqual(len(core.segments), 0)

    def test_version_mismatch_wholesale_replace(self):
        snap = build_session().snapshot()
        foreign = SessionCore(config={"config_version": 999, "mss": 7},
                              clock=DeterministicClock())
        restore_into(foreign, snap)
        # 整体替换：配置与状态全部采用快照内容
        self.assertEqual(foreign.config["config_version"],
                         snap["session_config"]["config_version"])
        self.assertEqual(foreign.config["mss"],
                         snap["session_config"]["mss"])
        kinds = [e["kind"] for e in foreign.events]
        self.assertIn("config_replaced", kinds)
        self.assertIn("snapshot_restored", kinds)

    def test_snapshot_version_rejected(self):
        snap = build_session().snapshot()
        snap["snapshot_version"] = 999
        core = SessionCore(clock=DeterministicClock())
        with self.assertRaises(BizError) as ctx:
            restore_into(core, snap)
        self.assertEqual(ctx.exception.code, E_VERSION)


if __name__ == "__main__":
    unittest.main()
