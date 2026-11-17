"""需求 4/5：档位调度、去噪步预算、批量策略与首帧预算。"""
from __future__ import annotations

import os
import tempfile
import unittest

from vbench.errors import ErrorCode
from vbench.pipeline import Pipeline, RenderRequest
from vbench.scheduler import (CROSS_CLIP, FRAME_BATCH, SERIAL,
                              per_frame_cost_ms, preview_plan, schedule)

from ._harness import Workspace, fresh_workspace


class SchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="vbench-sched-")
        self.ws = fresh_workspace(self.temp_dir, "ws", clip_count=3,
                                  frames=5, denoise_steps=6,
                                  replace_steps=2)

    def test_cost_grows_with_resolution(self) -> None:
        config = self.ws.config()
        costs = [per_frame_cost_ms(config, tier) for tier in config.tiers]
        self.assertEqual(costs[0], 1.0)
        self.assertTrue(costs[0] < costs[1] < costs[2])
        self.assertEqual(costs[2], 16.0)  # 256x256 / 64x64 像素比

    def test_decision_record_contains_scores_and_reasons(self) -> None:
        config = self.ws.config()
        decision = schedule(config, clip_count=3)
        self.assertEqual(len(decision.candidates), 3)
        modes = {item["mode"] for item in decision.candidates}
        self.assertEqual(modes, {SERIAL, FRAME_BATCH, CROSS_CLIP})
        self.assertTrue(all("score" in item for item in decision.candidates))
        self.assertTrue(decision.rationale)
        self.assertEqual(len(decision.selected_tiers), 3)
        self.assertEqual(decision.pending_tiers, [])
        self.assertEqual(decision.step_budget_used,
                         decision.step_budget_total)

    def test_insufficient_budget_pends_high_tiers(self) -> None:
        config = self.ws.config("fault_zero_budget")
        decision = schedule(config, clip_count=3)
        self.assertEqual(decision.selected_tiers, [])
        self.assertEqual(len(decision.pending_tiers), 3)
        self.assertTrue(all(
            item.code == ErrorCode.E_DENOISE_BUDGET_PENDING
            for item in decision.failures))

    def test_partial_budget_keeps_low_tier_and_pends_high(self) -> None:
        import json
        config = self.ws.config()
        raw_path = os.path.join(self.temp_dir, "partial", "config.json")
        from vbench.samplegen import generate_valid_sample
        sample_dir = os.path.join(self.temp_dir, "partial")
        generate_valid_sample(sample_dir, clip_count=2, frames=4,
                              denoise_steps=5, replace_steps=1,
                              denoise_step_budget=40)
        from vbench.config import load_config
        config = load_config(raw_path)
        decision = schedule(config, clip_count=2)
        selected = [plan.tier for plan in decision.selected_tiers]
        pending = [plan.tier for plan in decision.pending_tiers]
        self.assertIn("64x64", selected)
        self.assertTrue(pending)
        self.assertTrue(any(
            item.code == ErrorCode.E_DENOISE_BUDGET_PENDING
            for item in decision.failures))

    def test_zero_budget_run_emits_pending_and_keeps_manifest(self) -> None:
        config = self.ws.config("fault_zero_budget")
        sample_dir = self.ws.faults["zero_budget"]
        clips, failures = self.ws.clips(config, sample_dir)
        out = self.ws.output("zero-budget-out")
        outcome = Pipeline(config).run(RenderRequest(
            sample_dir=sample_dir, output_dir=out, clips=clips,
            load_failures=failures))
        codes = {item.code for item in outcome["failures"]}
        self.assertIn(ErrorCode.E_DENOISE_BUDGET_PENDING, codes)
        self.assertTrue(os.path.isfile(os.path.join(out, "manifest.json")))
        self.assertEqual(outcome["results"], [])

    def test_preview_eligible_inside_budget(self) -> None:
        config = self.ws.config()
        plan = preview_plan(config, 3)
        self.assertTrue(plan["eligible"])
        clips, failures = self.ws.clips(config)
        out = self.ws.output("preview-ok")
        response = Pipeline(config).preview_first_frame(RenderRequest(
            sample_dir=self.ws.sample_dir, output_dir=out, clips=clips,
            load_failures=failures))
        self.assertNotIn("code", response)
        self.assertTrue(os.path.isfile(response["frames"][clips[0].clip_id]))

    def test_preview_zero_budget_returns_pending_state(self) -> None:
        config = self.ws.config("fault_zero_preview")
        sample_dir = self.ws.faults["zero_preview"]
        clips, failures = self.ws.clips(config, sample_dir)
        out = self.ws.output("preview-pending")
        response = Pipeline(config).preview_first_frame(RenderRequest(
            sample_dir=sample_dir, output_dir=out, clips=clips,
            load_failures=failures))
        self.assertEqual(response["code"],
                         ErrorCode.E_PREVIEW_BUDGET_PENDING)
        self.assertTrue(os.path.isfile(os.path.join(out, "manifest.json")))
        self.assertEqual(response["frames"], {})


if __name__ == "__main__":
    unittest.main()
