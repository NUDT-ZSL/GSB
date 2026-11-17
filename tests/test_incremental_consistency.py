"""需求 6：编辑序列下增量渲染与空白工作区全量渲染逐像素一致。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from vbench.config import load_config
from vbench.pipeline import LatentPatch, Pipeline, RenderRequest
from vbench.samplegen import generate_valid_sample

from ._harness import (collect_frames, collect_raw_latents,
                       assert_frames_equal, assert_latents_equal,
                       full_render, fresh_workspace, Workspace)


def run_into(ws: Workspace, name: str, config, sample_dir: str,
             prompt_overrides=None, latent_patches=None) -> str:
    clips, failures = ws.clips(config, sample_dir)
    out = ws.output(name)
    Pipeline(config).run(RenderRequest(
        sample_dir=sample_dir, output_dir=out, clips=clips,
        load_failures=failures,
        prompt_overrides=prompt_overrides or {},
        latent_patches=latent_patches or []))
    return out


class IncrementalConsistencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="vbench-inc-")
        self.ws = fresh_workspace(self.temp_dir, "ws", clip_count=2,
                                  frames=4, denoise_steps=6,
                                  replace_steps=2)

    def _mutate_config(self, sample_dir: str, **changes) -> str:
        path = os.path.join(sample_dir, "config.json")
        with open(path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        config.update(changes)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
        return path

    def test_repeated_run_reuses_snapshot_and_identical_outputs(self) -> None:
        config = self.ws.config()
        first = run_into(self.ws, "inc", config, self.ws.sample_dir)
        manifest_before = json.load(
            open(os.path.join(first, "manifest.json"), encoding="utf-8"))
        run_into(self.ws, "inc", config, self.ws.sample_dir)
        manifest_after = json.load(
            open(os.path.join(first, "manifest.json"), encoding="utf-8"))
        # 输入未变时清单版本不再增长（命中快照，无新阶段替换）。
        self.assertEqual(manifest_before["version"], manifest_after["version"])
        full = full_render(self.ws, "full")
        assert_frames_equal(self, collect_frames(first),
                            collect_frames(full["store"].root))
        assert_latents_equal(self, collect_raw_latents(first),
                             collect_raw_latents(full["store"].root))

    def test_edit_sequence_matches_clean_full_run(self) -> None:
        config = self.ws.config()
        inc = run_into(self.ws, "inc-edit", config, self.ws.sample_dir)

        # 编辑 1：提示词覆盖。
        config = self.ws.config()
        clips, _ = self.ws.clips(config)
        override = {clips[0].clip_id: "red triangle rotates fast"}
        run_into(self.ws, "inc-edit", config, self.ws.sample_dir,
                 prompt_overrides=override)

        # 编辑 2：局部潜变量补丁。
        patch = LatentPatch(clip_id=clips[1].clip_id, tier="128x128",
                            path="text", step=5, frame=0, channel=1,
                            delta=0.375)
        run_into(self.ws, "inc-edit", config, self.ws.sample_dir,
                 prompt_overrides=override, latent_patches=[patch])

        # 从空白工作区按相同最终输入一次性全量。
        clean_root = os.path.join(self.temp_dir, "clean")
        shutil.rmtree(clean_root, ignore_errors=True)
        clean_ws = Workspace(clean_root, clip_count=2, frames=4,
                             denoise_steps=6, replace_steps=2)
        clean_out = run_into(clean_ws, "full", config,
                             clean_ws.sample_dir,
                             prompt_overrides=override,
                             latent_patches=[patch])
        assert_frames_equal(self, collect_frames(inc),
                            collect_frames(clean_out))
        assert_latents_equal(self, collect_raw_latents(inc),
                             collect_raw_latents(clean_out))

    def test_tier_narrow_then_expand(self) -> None:
        config = self.ws.config()
        inc = run_into(self.ws, "inc-tier", config, self.ws.sample_dir)

        # 收窄为两档：快照整体失效后重建。
        self._mutate_config(self.ws.sample_dir,
                            tiers=["64x64", "128x128"])
        narrow_config = load_config(os.path.join(
            self.ws.sample_dir, "config.json"))
        run_into(self.ws, "inc-tier", narrow_config, self.ws.sample_dir)

        # 再扩张回三档。
        self._mutate_config(self.ws.sample_dir,
                            tiers=["64x64", "128x128", "256x256"])
        wide_config = load_config(os.path.join(
            self.ws.sample_dir, "config.json"))
        run_into(self.ws, "inc-tier", wide_config, self.ws.sample_dir)

        clean = full_render(self.ws, "clean-tier")
        assert_frames_equal(self, collect_frames(inc),
                            collect_frames(clean["store"].root))
        assert_latents_equal(self, collect_raw_latents(inc),
                             collect_raw_latents(clean["store"].root))
        reuse = clean["report"]["tier_reuse_records"]
        self.assertTrue(any(item["upsampled"] for item in reuse))

    def test_step_and_guidance_edits_stay_consistent(self) -> None:
        self._mutate_config(self.ws.sample_dir,
                            denoise_steps=7, guidance_scale=9.0)
        # 表长度随步数变化，重新生成本档位表后再加载配置。
        import numpy as np
        from vbench.samplegen import _table
        with open(os.path.join(self.ws.sample_dir, "config.json"),
                  encoding="utf-8") as handle:
            raw_config = json.load(handle)
        for tier_name in raw_config["tiers"]:
            np.save(os.path.join(self.ws.sample_dir, "tables",
                                 f"noise_{tier_name}.npy"),
                    _table("noise", tier_name, 7, raw_config["seed"]))
            np.save(os.path.join(self.ws.sample_dir, "tables",
                                 f"guidance_{tier_name}.npy"),
                    _table("guidance", tier_name, 7, raw_config["seed"]))
        config = load_config(os.path.join(
            self.ws.sample_dir, "config.json"))
        inc = run_into(self.ws, "inc-step", config, self.ws.sample_dir)
        clean = full_render(self.ws, "clean-step", config=config)
        assert_frames_equal(self, collect_frames(inc),
                            collect_frames(clean["store"].root))
        assert_latents_equal(self, collect_raw_latents(inc),
                             collect_raw_latents(clean["store"].root))


if __name__ == "__main__":
    unittest.main()
