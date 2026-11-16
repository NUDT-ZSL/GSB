import json
import os
import tempfile
import unittest

from sre_guard import (
    GovernanceEngine,
    Grid,
    load_events,
    load_policy,
    restore_snapshot,
    replay_snapshot,
    save_snapshot,
)
from sre_guard.errors import E_GRID, E_REPLAY, GovernanceError
from sre_guard.policy import bump_version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SnapshotReplayTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(os.path.join(ROOT, "config", "policy.json"))
        self.events = load_events(os.path.join(ROOT, "data", "events.ndjson"))
        self.grid = Grid.from_exponent(4)
        self.tmp = tempfile.mkdtemp()
        self.snapshot_path = os.path.join(self.tmp, "snapshot.json")
        engine = GovernanceEngine(self.policy, self.grid)
        engine.run(self.events)
        save_snapshot(self.snapshot_path, engine, self.events)

    def test_restore_and_replay_matches(self):
        result = restore_snapshot(self.snapshot_path, self.policy)
        self.assertFalse(result.version_replaced)
        report = replay_snapshot(self.snapshot_path)
        self.assertTrue(report.matched)
        self.assertEqual(report.events_compared, len(self.events))

    def test_version_mismatch_triggers_full_replacement(self):
        policy_v2 = bump_version(self.policy,
                                 {"payment-api": {"failure_threshold_percent": 25}})
        result = restore_snapshot(self.snapshot_path, policy_v2)
        self.assertTrue(result.version_replaced)
        self.assertTrue(any(d["field"] == "breaker" for d in result.policy_diffs))
        # 重放仍按快照内嵌 v1 策略复现
        report = replay_snapshot(self.snapshot_path)
        self.assertTrue(report.matched)

    def test_missing_grid_raises_e_grid(self):
        with open(self.snapshot_path, encoding="utf-8") as h:
            data = json.load(h)
        data.pop("grid")
        broken = os.path.join(self.tmp, "no_grid.json")
        with open(broken, "w", encoding="utf-8") as h:
            json.dump(data, h)
        with self.assertRaises(GovernanceError) as ctx:
            restore_snapshot(broken, self.policy)
        self.assertEqual(ctx.exception.code, E_GRID)

    def test_grid_exponent_change_raises_e_replay(self):
        with self.assertRaises(GovernanceError) as ctx:
            replay_snapshot(self.snapshot_path, expected_grid_exponent=6)
        self.assertEqual(ctx.exception.code, E_REPLAY)

    def test_tampered_decision_reports_first_mismatch(self):
        with open(self.snapshot_path, encoding="utf-8") as h:
            data = json.load(h)
        # 篡改第 3 条决策的熔断状态字段
        data["decisions"][2]["breaker_state"] = "tampered"
        broken = os.path.join(self.tmp, "tampered.json")
        with open(broken, "w", encoding="utf-8") as h:
            json.dump(data, h)
        report = replay_snapshot(broken)
        self.assertFalse(report.matched)
        self.assertEqual(report.first_mismatch_index, 2)
        self.assertIn("breaker_state", report.mismatched_fields)
        self.assertIsNotNone(report.mismatch_detail["fixed_point_inputs"])


if __name__ == "__main__":
    unittest.main()
