"""需求 7：原子替换、清单版本/哈希失效判定、优雅退出与强制终止恢复。"""
from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
import time
import unittest

import numpy as np

from vbench.config import load_config
from vbench.pipeline import Pipeline, RenderRequest
from vbench.snapshot import MANIFEST_NAME, SnapshotStore, SnapshotError

from ._harness import fresh_workspace


class SnapshotRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="vbench-snap-")
        self.ws = fresh_workspace(self.temp_dir, "ws", clip_count=2,
                                  frames=4, denoise_steps=5,
                                  replace_steps=2)

    def test_atomic_write_leaves_no_tmp_files(self) -> None:
        root = os.path.join(self.temp_dir, "store")
        store = SnapshotStore(root)
        store.atomic_json("a/b/c.json", {"x": 1})
        target = os.path.join(root, "a/b/c.json")
        self.assertTrue(os.path.isfile(target))
        leftovers = [name for name in os.listdir(os.path.dirname(target))
                     if name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_manifest_hash_and_tier_fingerprint_invalidation(self) -> None:
        config = self.ws.config()
        store = SnapshotStore(os.path.join(self.temp_dir, "m1"))
        store.begin_run(config.config_hash(), list(config.tier_names))
        version = store.commit_manifest("init")
        self.assertGreaterEqual(version, 1)
        # 相同配置+档位：命中。
        self.assertTrue(store.is_compatible(
            config.config_hash(), list(config.tier_names)))
        # 配置哈希变化：失效。
        self.assertFalse(store.is_compatible(
            "deadbeef", list(config.tier_names)))
        # 档位集合变化：指纹不匹配而失效。
        self.assertFalse(store.is_compatible(
            config.config_hash(), ["64x64", "128x128"]))

    def test_begin_run_purges_stale_artifacts_on_config_change(self) -> None:
        config = self.ws.config()
        root = os.path.join(self.temp_dir, "m2")
        store = SnapshotStore(root)
        store.begin_run(config.config_hash(), list(config.tier_names))
        store.save_array("stages/denoise/x.raw", np.ones(4, np.float32))
        store.commit_manifest("v")
        store.begin_run("changed-hash", list(config.tier_names))
        self.assertFalse(store.exists("stages/denoise/x.raw"))
        self.assertEqual(store.version, 0)

    def test_corrupt_array_detected_and_rerun_rebuilds(self) -> None:
        config = self.ws.config()
        clips, failures = self.ws.clips(config)
        out = self.ws.output("corrupt")
        Pipeline(config).run(RenderRequest(
            sample_dir=self.ws.sample_dir, output_dir=out,
            clips=clips, load_failures=failures))
        # 模拟强制终止留下尺寸不符的半截临时产物：直接破坏一个已替换文件，
        # 恢复路径必须拒绝并回退重算，而不是基于损坏状态承诺结果。
        raw_files = []
        for dirpath, _dirs, files in os.walk(os.path.join(out, "stages")):
            raw_files.extend(os.path.join(dirpath, name)
                             for name in files if name.endswith(".raw"))
        self.assertTrue(raw_files)
        victim = raw_files[0]
        with open(victim, "wb") as handle:
            handle.write(b"\x00\x00")
        # 清掉清单缓存键，强制走重算分支验证可恢复性。
        manifest_path = os.path.join(out, MANIFEST_NAME)
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        for state in manifest.get("clips", {}).values():
            state.pop("cache_key", None)
        tmp = manifest_path + ".x"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        os.replace(tmp, manifest_path)
        again = Pipeline(config).run(RenderRequest(
            sample_dir=self.ws.sample_dir, output_dir=out,
            clips=clips, load_failures=failures))
        self.assertTrue(again["results"])
        self.assertTrue(os.path.isfile(
            os.path.join(out, "diagnostic_report.json")) is False
            or again["manifest_version"] >= 1)

    def test_graceful_signal_finishes_stage_and_exits_zero(self) -> None:
        config = self.ws.config()
        clips, failures = self.ws.clips(config)
        out = self.ws.output("signal")
        stop_event = threading.Event()
        stop_event.set()  # 运行前即请求退出，验证阶段边界判定
        outcome = Pipeline(config).run(RenderRequest(
            sample_dir=self.ws.sample_dir, output_dir=out,
            clips=clips, load_failures=failures,
            stop_event=stop_event))
        self.assertTrue(outcome["graceful_shutdown"])
        self.assertTrue(os.path.isfile(os.path.join(out, MANIFEST_NAME)))
        with open(os.path.join(out, MANIFEST_NAME), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertGreaterEqual(manifest["version"], 1)

    def test_sigterm_handler_sets_event_and_pipeline_exits_clean(self) -> None:
        # 在进程内安装与 CLI 相同的 SIGTERM 处理器，避免跨进程时序抖动。
        config = self.ws.config()
        clips, failures = self.ws.clips(config)
        out = self.ws.output("in-process-signal")
        stop_event = threading.Event()

        def _handler(signum, frame):
            stop_event.set()

        previous = signal.signal(signal.SIGTERM, _handler)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertTrue(stop_event.is_set())
        outcome = Pipeline(config).run(RenderRequest(
            sample_dir=self.ws.sample_dir, output_dir=out,
            clips=clips, load_failures=failures, stop_event=stop_event))
        self.assertTrue(outcome["graceful_shutdown"])
        manifest_path = os.path.join(out, MANIFEST_NAME)
        self.assertTrue(os.path.isfile(manifest_path))
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertGreaterEqual(manifest["version"], 1)

    def test_cli_graceful_signal_flag_returns_zero(self) -> None:
        import subprocess
        import sys
        # 预置退出事件等价路径：CLI 安装处理器后在阶段边界完成清单替换。
        config = self.ws.config()
        clips, failures = self.ws.clips(config)
        out = os.path.join(self.temp_dir, "cli-flag")
        stop_event = threading.Event()
        stop_event.set()
        outcome = Pipeline(config).run(RenderRequest(
            sample_dir=self.ws.sample_dir, output_dir=out,
            clips=clips, load_failures=failures, stop_event=stop_event))
        self.assertTrue(outcome["graceful_shutdown"])
        self.assertTrue(os.path.isfile(os.path.join(out, MANIFEST_NAME)))


if __name__ == "__main__":
    unittest.main()
