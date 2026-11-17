"""需求 8：线程池并发与串行结果一致、稳定排序、异常隔离。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import numpy as np

from vbench.config import load_config
from vbench.errors import ErrorCode
from vbench.pipeline import Pipeline, RenderRequest
from vbench.scheduler import CROSS_CLIP, FRAME_BATCH, SERIAL

from ._harness import (collect_frames, collect_raw_latents,
                       assert_frames_equal, assert_latents_equal,
                       fresh_workspace)


def render_with(config, sample_dir: str, output_dir: str) -> dict:
    from vbench.samples import load_clips
    clips, failures = load_clips(sample_dir, config)
    return Pipeline(config).run(RenderRequest(
        sample_dir=sample_dir, output_dir=output_dir,
        clips=clips, load_failures=failures))


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="vbench-conc-")
        self.ws = fresh_workspace(self.temp_dir, "ws", clip_count=4,
                                  frames=5, denoise_steps=6,
                                  replace_steps=2)

    def _write_concurrency_config(self, concurrency: int,
                                  memory_cap: float,
                                  tag: str = "mode") -> str:
        import shutil
        target = os.path.join(self.temp_dir, f"cfg-{tag}-{concurrency}-{memory_cap}")
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(self.ws.sample_dir, target)
        path = os.path.join(target, "config.json")
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
        config["concurrency"] = concurrency
        config["memory_cap_mb"] = memory_cap
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
        return path

    def test_serial_batch_and_cross_clip_modes_identical(self) -> None:
        # 大内存 -> 跨片段合并；中内存 -> 帧维批量；小内存 -> 逐帧串行。
        outputs = {}
        for name, cap in (("serial", 0.2), ("batch", 0.6), ("cross", 4096.0)):
            path = self._write_concurrency_config(4, cap, tag=name)
            sample_dir = os.path.dirname(path)
            config = load_config(path)
            outcome = render_with(
                config, sample_dir,
                os.path.join(self.temp_dir, f"out-{name}"))
            self.assertIn(outcome["decision"].batch_mode,
                          (SERIAL, FRAME_BATCH, CROSS_CLIP))
            outputs[name] = outcome
        modes = {name: item["decision"].batch_mode
                 for name, item in outputs.items()}
        self.assertEqual(len(set(modes.values())), 3, msg=modes)
        baseline = outputs["serial"]
        for name in ("batch", "cross"):
            assert_frames_equal(
                self, collect_frames(baseline["store"].root),
                collect_frames(outputs[name]["store"].root))
            assert_latents_equal(
                self, collect_raw_latents(baseline["store"].root),
                collect_raw_latents(outputs[name]["store"].root))

    def test_concurrency_level_does_not_change_outputs(self) -> None:
        results = {}
        for concurrency in (1, 2, 8):
            path = self._write_concurrency_config(concurrency, 4096.0,
                                                  tag=f"workers{concurrency}")
            sample_dir = os.path.dirname(path)
            config = load_config(path)
            outcome = render_with(
                config, sample_dir,
                os.path.join(self.temp_dir, f"workers-{concurrency}"))
            results[concurrency] = outcome
        for concurrency in (2, 8):
            assert_frames_equal(
                self, collect_frames(results[1]["store"].root),
                collect_frames(results[concurrency]["store"].root))
            assert_latents_equal(
                self, collect_raw_latents(results[1]["store"].root),
                collect_raw_latents(results[concurrency]["store"].root))

    def test_failure_list_order_is_stable_across_runs(self) -> None:
        divergent_dir = self.ws.faults["frame_mismatch"]
        config = self.ws.config()
        first = render_with(
            config, divergent_dir, os.path.join(self.temp_dir, "fail-a"))
        second = render_with(
            config, divergent_dir, os.path.join(self.temp_dir, "fail-b"))
        keys_a = [(item.code, item.clip_id, item.tier)
                  for item in first["failures"]]
        keys_b = [(item.code, item.clip_id, item.tier)
                  for item in second["failures"]]
        self.assertEqual(keys_a, keys_b)
        self.assertTrue(all(
            item.code == ErrorCode.E_TIMESERIES_INVALID
            for item in first["failures"]))

    def test_results_sorted_by_clip_order_regardless_of_threads(self) -> None:
        path = self._write_concurrency_config(8, 4096.0, tag="order")
        sample_dir = os.path.dirname(path)
        config = load_config(path)
        outcome = render_with(
            config, sample_dir,
            os.path.join(self.temp_dir, "order"))
        clip_ids = [result.clip_id for result in outcome["results"]]
        self.assertEqual(clip_ids, sorted(clip_ids))
        self.assertEqual(clip_ids,
                         [f"clip_{index:03d}" for index in range(4)])

    def test_path_divergence_failure_isolated_per_clip(self) -> None:
        config = load_config(os.path.join(
            self.ws.faults["path_divergence"], "config.json"))
        outcome = render_with(
            config, self.ws.faults["path_divergence"],
            os.path.join(self.temp_dir, "divergent"))
        # 注入影响全部片段，但每片段失败被独立记录且其余阶段继续完成。
        self.assertEqual(len(outcome["results"]), 4)
        divergence = [item for item in outcome["failures"]
                      if item.code == ErrorCode.E_PATH_DIVERGENCE]
        self.assertTrue(divergence)
        self.assertTrue(all(item.clip_id for item in divergence))
        # 失败清单顺序稳定。
        self.assertEqual(
            [(item.clip_id, item.tier, item.step) for item in divergence],
            sorted((item.clip_id, item.tier, item.step)
                   for item in divergence))


if __name__ == "__main__":
    unittest.main()


class StableKeyTest(unittest.TestCase):
    def test_task_keys_are_fully_sorted_tuples(self) -> None:
        from vbench.pipeline import _stable_key
        temp_dir = tempfile.mkdtemp(prefix="vbench-key-")
        ws = fresh_workspace(temp_dir, "ws", clip_count=3, frames=4,
                             denoise_steps=5, replace_steps=2)
        config = load_config(os.path.join(ws.sample_dir, "config.json"))
        outcome = render_with(
            config, ws.sample_dir, os.path.join(temp_dir, "out"))
        for result in outcome["results"]:
            self.assertTrue(result.task_keys)
            self.assertEqual(result.task_keys, sorted(result.task_keys))
            expected_len = (len(config.tiers) * 2
                            * config.denoise_steps * config.frames)
            self.assertEqual(len(result.task_keys), expected_len)
        # 键元组结构：(clip, tier, path, step, frame)
        sample_key = _stable_key(2, 1, 0, 3, 4)
        self.assertEqual(sample_key, (2, 1, 0, 3, 4))
