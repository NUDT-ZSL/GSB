"""需求 3：图生替代步、共享段逐元素相同、分歧段必须不同。"""
from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

from vbench.errors import ErrorCode
from vbench.pipeline import Pipeline, RenderRequest

from ._harness import Workspace, fresh_workspace


def render_workspace(ws: Workspace, name: str, fault_name: str | None = None):
    config = ws.config(fault_name) if fault_name else ws.config()
    sample_dir = os.path.join(ws.fault_dir, fault_name) if fault_name else ws.sample_dir
    clips, failures = load = ws.clips(config, sample_dir)
    out = ws.output(name)
    request = RenderRequest(sample_dir=sample_dir, output_dir=out,
                            clips=clips, load_failures=failures)
    outcome = Pipeline(config).run(request)
    return config, outcome


class DualPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="vbench-dual-")
        self.ws = fresh_workspace(self.temp_dir, "ws", clip_count=2,
                                  frames=4, denoise_steps=6,
                                  replace_steps=2)

    def test_shared_and_divergent_segments_listed(self) -> None:
        _config, outcome = render_workspace(self.ws, "valid")
        result = outcome["results"][0]
        self.assertEqual(result.segments["divergent_steps"], [0, 1])
        self.assertEqual(result.segments["shared_steps"], [2, 3, 4, 5])
        self.assertEqual(outcome["failures"], [])

    def test_shared_latents_elementwise_identical(self) -> None:
        config, outcome = render_workspace(self.ws, "valid")
        clip_id = outcome["results"][0].clip_id
        store_root = outcome["store"].root
        for tier in config.tier_names:
            text_dir = os.path.join(
                store_root, "stages/denoise", clip_id, tier, "text")
            for file_name in sorted(os.listdir(text_dir)):
                step = int(file_name.split("_")[1].split(".")[0])
                if step < 2:
                    continue
                text_latent = np.fromfile(
                    os.path.join(text_dir, file_name), dtype=np.float32)
                image_latent = np.fromfile(
                    os.path.join(text_dir.replace("/text", "/image"),
                                 file_name), dtype=np.float32)
                np.testing.assert_array_equal(
                    text_latent, image_latent,
                    err_msg=f"共享段 {tier} step {step} 不一致")

    def test_divergent_latents_must_differ(self) -> None:
        config, outcome = render_workspace(self.ws, "valid")
        clip_id = outcome["results"][0].clip_id
        store_root = outcome["store"].root
        tier = config.tiers[0].name
        for step in (0,):
            text_latent = np.fromfile(os.path.join(
                store_root, "stages/denoise", clip_id, tier,
                "text", f"step_{step:04d}.raw"), dtype=np.float32)
            image_latent = np.fromfile(os.path.join(
                store_root, "stages/denoise", clip_id, tier,
                "image", f"step_{step:04d}.raw"), dtype=np.float32)
            self.assertFalse(np.array_equal(text_latent, image_latent))

    def test_injected_shared_corruption_emits_business_code(self) -> None:
        _config, outcome = render_workspace(
            self.ws, "diverge", "fault_path_divergence")
        codes = {item.code for item in outcome["failures"]}
        self.assertIn(ErrorCode.E_PATH_DIVERGENCE, codes)
        divergence = [item for item in outcome["failures"]
                      if item.code == ErrorCode.E_PATH_DIVERGENCE]
        self.assertTrue(all(item.step is not None for item in divergence))

    def test_zero_replace_steps_means_fully_shared(self) -> None:
        import json
        custom_dir = os.path.join(self.temp_dir, "no-replace")
        from vbench.samplegen import generate_valid_sample
        generate_valid_sample(custom_dir, clip_count=2, frames=4,
                              denoise_steps=5, replace_steps=0)
        from vbench.config import load_config
        config = load_config(os.path.join(custom_dir, "config.json"))
        from vbench.samples import load_clips
        clips, failures = load_clips(custom_dir, config)
        outcome = Pipeline(config).run(RenderRequest(
            sample_dir=custom_dir,
            output_dir=os.path.join(self.temp_dir, "no-replace-out"),
            clips=clips, load_failures=failures))
        self.assertEqual(outcome["results"][0].segments["divergent_steps"], [])
        self.assertEqual(outcome["failures"], [])


if __name__ == "__main__":
    unittest.main()
