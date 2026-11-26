import unittest

from kfdiff.clipmodel import ClipTask
from kfdiff.tradeoff import adjudicate, consistency_risk
from tests.helpers import make_studio


class TradeoffTests(unittest.TestCase):
    def test_seeded_scenarios_trigger_expected_adjudication(self):
        studio, _ = make_studio()
        p3 = studio.build_plan("clip_0003")
        self.assertEqual(p3["selected_block_grid"], 3)
        self.assertTrue(any(r["code"] == "GRID_TOO_SMALL_RISK" for r in p3["reasons"]))

        p2 = studio.build_plan("clip_0002")
        self.assertEqual(p2["selected_block_grid"], 3)
        self.assertTrue(any(r["code"] == "GRID_TOO_LARGE_LATENCY" for r in p2["reasons"]))

        p4 = studio.build_plan("clip_0004")
        self.assertLess(p4["selected_strength"], 1.0)
        self.assertTrue(any(r["code"] == "DIFFUSION_APPROXIMATED" for r in p4["reasons"]))

    def test_plan_hash_reproduces(self):
        studio, _ = make_studio()
        first = studio.build_plan("clip_0004")
        second = studio.build_plan("clip_0004")
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(first["selected_block_grid"], second["selected_block_grid"])

    def test_priority_order_changes_resolution(self):
        studio, _ = make_studio()
        budget = 12.0
        budget_first = studio.build_plan(
            "clip_0004", budget_ms=budget,
            priority_order=["block_grid", "first_frame_budget", "diffusion_strength"],
        )
        strength_first = studio.build_plan(
            "clip_0004", budget_ms=budget,
            priority_order=["diffusion_strength", "block_grid", "first_frame_budget"],
        )
        self.assertLess(budget_first["selected_strength"], 1.0)
        self.assertEqual(strength_first["selected_strength"], 1.0)
        self.assertTrue(strength_first["residual_violations"])

    def test_invalid_priority_order_rejected(self):
        studio, _ = make_studio()
        task = studio.tasks["clip_0001"]
        with self.assertRaises(ValueError):
            adjudicate(task, studio.config, priority_override=["block_grid", "block_grid", "x"])

    def test_risk_monotonic_in_grid(self):
        studio, _ = make_studio()
        risks = [consistency_risk(11, g, studio.config)["risk"] for g in (1, 2, 3, 4, 6)]
        self.assertEqual(risks, sorted(risks, reverse=True))

    def test_synthetic_task_adjudication(self):
        task = ClipTask(
            clip_id="synthetic", total_frames=33, keyframe_interval=4,
            frame_shape=(24, 32, 3), noise_seed=7, prompt="x",
            diffusion_steps=12, block_grid=1, revision=1,
        )
        studio, _ = make_studio()
        plan = adjudicate(task, studio.config)
        self.assertIn("plan_hash", plan)
        self.assertGreaterEqual(plan["selected_block_grid"], 1)


if __name__ == "__main__":
    unittest.main()
