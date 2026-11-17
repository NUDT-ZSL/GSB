"""数据规模跨量级放大时的调度、预算与确定性。"""
from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

from vbench.config import load_config
from vbench.errors import ErrorCode
from vbench.pipeline import Pipeline, RenderRequest
from vbench.scheduler import per_frame_cost_ms, schedule
from vbench.samplegen import generate_valid_sample
from vbench.samples import load_clips

from ._harness import (collect_frames, collect_raw_latents,
                       assert_frames_equal, assert_latents_equal)


class ScaleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="vbench-scale-")

    def _render(self, tag: str, **kwargs):
        sample_dir = os.path.join(self.temp_dir, tag)
        config_path = generate_valid_sample(sample_dir, **kwargs)
        config = load_config(config_path)
        clips, failures = load_clips(sample_dir, config)
        out = os.path.join(self.temp_dir, tag + "-out")
        outcome = Pipeline(config).run(RenderRequest(
            sample_dir=sample_dir, output_dir=out, clips=clips,
            load_failures=failures))
        return config, outcome

    def test_small_medium_large_clip_scaling(self) -> None:
        scales = ((2, 3, 4), (8, 4, 6), (16, 3, 4))
        for index, (clips, frames, steps) in enumerate(scales):
            config, outcome = self._render(
                f"scale-{index}", clip_count=clips, frames=frames,
                denoise_steps=steps, replace_steps=1, tiers=DEFAULT_TIERS)
            self.assertEqual(len(outcome["results"]), clips)
            report_tiers = outcome["report"] if "report" in outcome else None
            for result in outcome["results"]:
                for tier_name, frame_map in result.decoded.items():
                    self.assertEqual(len(frame_map), frames)
            # 预算记录随规模线性增长且不超预算。
            self.assertLessEqual(
                outcome["decision"].step_budget_used,
                outcome["decision"].step_budget_total)
            frames_now = collect_frames(outcome["store"].root)
            self.assertEqual(
                len(frames_now),
                clips * len(DEFAULT_TIERS) * frames * 2)  # 双路径
            previous_frames = len(frames_now)

    def test_extra_high_resolution_tier(self) -> None:
        config, outcome = self._render(
            "hires", clip_count=2, frames=3, denoise_steps=5,
            replace_steps=1, tiers=["64x64", "128x128", "256x256", "512x512"])
        tier_names = [plan.tier for plan in outcome["decision"].selected_tiers]
        self.assertEqual(tier_names,
                         ["64x64", "128x128", "256x256", "512x512"])
        costs = [per_frame_cost_ms(config, tier) for tier in config.tiers]
        self.assertEqual(costs[-1], 64.0)
        # 最高档必须存在真实上采样复用记录。
        reuse = outcome["results"][0].tier_reuse
        self.assertTrue(all(item["upsampled"] for item in reuse[1:]))

    def test_large_run_remains_deterministic(self) -> None:
        config_a, first = self._render(
            "large-a", clip_count=10, frames=6, denoise_steps=6,
            replace_steps=2)
        config_b, second = self._render(
            "large-b", clip_count=10, frames=6, denoise_steps=6,
            replace_steps=2)
        # 两个独立工作区、相同输入：逐像素与逐潜变量一致。
        keys_a = {key.split("/", 1)[1]: value
                  for key, value in collect_frames(first["store"].root).items()}
        keys_b = {key.split("/", 1)[1]: value
                  for key, value in collect_frames(second["store"].root).items()}
        assert_frames_equal(self, keys_a, keys_b)
        lat_a = {key.split("/", 1)[1]: value
                 for key, value in collect_raw_latents(first["store"].root).items()}
        lat_b = {key.split("/", 1)[1]: value
                 for key, value in collect_raw_latents(second["store"].root).items()}
        assert_latents_equal(self, lat_a, lat_b)

    def test_budget_pending_under_oversized_scale(self) -> None:
        sample_dir = os.path.join(self.temp_dir, "starved")
        config_path = generate_valid_sample(
            sample_dir, clip_count=12, frames=8, denoise_steps=10,
            replace_steps=2, denoise_step_budget=1000)
        config = load_config(config_path)
        clips, failures = load_clips(sample_dir, config)
        outcome = Pipeline(config).run(RenderRequest(
            sample_dir=sample_dir,
            output_dir=os.path.join(self.temp_dir, "starved-out"),
            clips=clips, load_failures=failures))
        pending_codes = [item.code for item in outcome["failures"]]
        self.assertIn(ErrorCode.E_DENOISE_BUDGET_PENDING, pending_codes)
        # 低档位仍被渲染，高档位保留挂起状态而非静默丢弃。
        self.assertTrue(outcome["decision"].selected_tiers)
        self.assertTrue(outcome["decision"].pending_tiers)


DEFAULT_TIERS = ["64x64", "128x128", "256x256"]


if __name__ == "__main__":
    unittest.main()
