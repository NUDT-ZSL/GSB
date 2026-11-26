import unittest

from gsb.engine import Engine
from gsb.errors import GsbError
from tests._util import fresh_state_dir, samples_dir


class PolicyEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.state = fresh_state_dir("evo")
        self.engine = Engine(samples_dir(), self.state)
        self.engine.cmd_load()
        self.engine.cmd_merge()
        self.engine.cmd_judge()

    def tearDown(self):
        self.engine.close()

    def test_patch_closure_and_carry(self):
        patch_path = samples_dir() / "patches" / "p1_theta.json"
        result = self.engine.cmd_patch(patch_path)
        self.assertEqual(result["closure"]["dirty_segments"], [2, 3, 4])
        self.assertEqual(result["closure"]["reusable_segments"], [1, 5, 6])
        self.assertEqual(result["carried_decisions"], [1, 5, 6])

        judged = self.engine.cmd_judge()
        self.assertEqual(judged["committed"], [2, 3, 4])
        self.assertEqual(judged["idempotent"], [1, 5, 6])
        verify = self.engine.cmd_verify()
        self.assertTrue(verify["ok"], verify["checks"])

    def test_snapshot_replace_carry_and_rollback(self):
        good = samples_dir() / "snapshots" / "snapshot_v3_good.json"
        result = self.engine.cmd_replace(good)
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["version"], "3.0.0")
        self.assertIn(1, result["carried_decisions"])
        self.engine.cmd_judge()
        self.assertTrue(self.engine.cmd_verify()["ok"])

        # 相同快照 + 相同幂等键重复提交 -> 幂等
        again = self.engine.cmd_replace(good)
        self.assertTrue(again["idempotent"])

        # 版本回退 -> E_VERSION_ROLLBACK
        rollback = samples_dir() / "snapshots" / "snapshot_v2_rollback.json"
        with self.assertRaises(GsbError) as ctx:
            self.engine.cmd_replace(rollback)
        self.assertEqual(ctx.exception.code, "E_VERSION_ROLLBACK")

    def test_rollback_rejected_before_v3(self):
        # 初始活动版本 1.0.0 时，2.4.0 不是回退（是升级），可被接受
        rollback = samples_dir() / "snapshots" / "snapshot_v2_rollback.json"
        result = self.engine.cmd_replace(rollback)
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["version"], "2.4.0")


if __name__ == "__main__":
    unittest.main()
