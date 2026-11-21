"""latent_diag 验收测试：确定性、分块/整块一致性、精度档、注入归因、
快照整体替换、资源降级、门限稳定性。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from latent_diag.config import (
    WorkbenchConfig, ThresholdPolicy, STRATEGY_STRICT, STRATEGY_TOLERANCE_BAND,
)
from latent_diag.errors import (
    StageError, E_SNAPSHOT_ROLLBACK, E_SNAPSHOT_CORRUPT, E_RESOURCE_LIMIT,
    E_BAD_PROMPT_SEED, E_BAD_LATENT_SHAPE, E_BLOCK_GRID_INDIVISIBLE,
    E_INVALID_QUANT_SCALE, E_UNKNOWN_ROUNDING_MODE,
)
from latent_diag.pipeline import run_task_pipeline
from latent_diag.resources import apply_resource_limits, DEGRADE_BLOCK_GRID_REDUCED
from latent_diag.runner import run_workbench
from latent_diag.snapshots import SnapshotStore
from latent_diag.tasks import TaskSpec, write_manifest
from latent_diag.thresholds import (
    exceeds_threshold, subnormal_guard, classify_residuals, top_k_candidates,
)


def make_task(**over):
    base = dict(task_id="t1", prompt="unit test prompt", prompt_seed=42,
                latent_shape=[4, 32, 32], block_grid=[2, 2], sampler_steps=4,
                noise_scale=1.0, decode_mode="tanh", quant_scale=1000.0,
                rounding_mode="half_even", dtype_profile="float64",
                ref_mode="whole")
    base.update(over)
    return TaskSpec(**base)


def make_cfg(root: Path) -> WorkbenchConfig:
    cfg = WorkbenchConfig()
    cfg.sample_dir = str(root / "samples")
    cfg.snapshot_dir = str(root / "snapshots")
    cfg.cache_dir = str(root / "cache")
    cfg.report_dir = str(root / "reports")
    return cfg


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="latent_diag_test_"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def run_manifest(self, tasks, anomalies=None, cfg=None):
        cfg = cfg or make_cfg(self.root)
        for d in (cfg.sample_dir, cfg.snapshot_dir, cfg.cache_dir, cfg.report_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        manifest = Path(cfg.sample_dir) / "tasks.jsonl"
        write_manifest(tasks, manifest)
        anomalies_path = None
        if anomalies:
            anomalies_path = Path(cfg.sample_dir) / "anomalies.json"
            anomalies_path.write_text(json.dumps(anomalies), encoding="utf-8")
        report = run_workbench(cfg, str(manifest), anomalies_path and str(anomalies_path))
        return cfg, report


class TestDeterminism(WorkspaceCase):
    def test_byte_identical_rerun(self):
        tasks = [make_task(task_id="d1", prompt_seed=7),
                 make_task(task_id="d2", prompt_seed=8, dtype_profile="fixed",
                           quant_scale=250.0, rounding_mode="floor")]
        cfg1, rep1 = self.run_manifest(tasks)
        img1 = (Path(cfg1.cache_dir) / "images" / "d1.png").read_bytes()
        # 清空快照后重跑，版本号与全部输出必须逐字节一致
        shutil.rmtree(cfg1.snapshot_dir)
        cfg2, rep2 = self.run_manifest(tasks)
        img2 = (Path(cfg2.cache_dir) / "images" / "d1.png").read_bytes()
        self.assertEqual(rep1["report_hash"], rep2["report_hash"])
        self.assertEqual(img1, img2)
        self.assertEqual(rep1["snapshots"]["config"]["version"], 1)


class TestChunkVsWhole(WorkspaceCase):
    def test_float64_chunked_equals_whole(self):
        task = make_task(block_grid=[4, 8])
        cfg = make_cfg(self.root)
        active, reference, metrics = run_task_pipeline(task, cfg)
        self.assertTrue(metrics["elementwise_equal"])
        self.assertEqual(metrics["boundary_max_abs_dev"], 0.0)
        self.assertEqual(metrics["boundary_max_rel_dev"], 0.0)
        np.testing.assert_array_equal(active["normed"], reference["normed"])

    def test_norm_stats_whole_frame_semantics(self):
        task = make_task(block_grid=[2, 2])
        cfg = make_cfg(self.root)
        active, reference, _ = run_task_pipeline(task, cfg)
        self.assertEqual(active["norm_stats"], reference["norm_stats"])

    def test_chunk_mismatch_reports_first_block(self):
        from latent_diag.pipeline import compare_chunked_vs_whole
        from latent_diag.errors import E_CHUNK_MISMATCH
        task = make_task(block_grid=[2, 2])
        whole = np.zeros(task.latent_shape)
        chunked = whole.copy()
        chunked[:, 20, 20] = 1e-3  # 第二行第二列块内注入偏差
        with self.assertRaises(StageError) as ctx:
            compare_chunked_vs_whole(chunked, whole, task, tolerance=0.0)
        self.assertEqual(ctx.exception.code, E_CHUNK_MISMATCH)
        self.assertEqual(ctx.exception.detail["first_block"], [1, 1])


class TestDtypeProfiles(WorkspaceCase):
    def test_profiles_share_inputs_and_rng(self):
        # 同一 seed 不同精度档：参考路径（float64）输出完全一致，
        # 即切换精度档不改变 task 集合与随机数消费顺序
        cfg = make_cfg(self.root)
        refs = []
        for profile in ("float64", "float32", "fixed"):
            task = make_task(dtype_profile=profile, quant_scale=100.0)
            _, reference, _ = run_task_pipeline(task, cfg)
            refs.append(reference)
        np.testing.assert_array_equal(refs[0]["cond"], refs[1]["cond"])
        np.testing.assert_array_equal(refs[0]["cond"], refs[2]["cond"])
        np.testing.assert_array_equal(refs[0]["latent0"], refs[1]["latent0"])
        np.testing.assert_array_equal(refs[0]["normed"], refs[1]["normed"])

    def test_profile_differences_are_precision_only(self):
        cfg = make_cfg(self.root)
        _, ref, _ = run_task_pipeline(make_task(dtype_profile="float64"), cfg)
        act32, _, _ = run_task_pipeline(make_task(dtype_profile="float32"), cfg)
        actfx, _, _ = run_task_pipeline(
            make_task(dtype_profile="fixed", quant_scale=100.0), cfg)
        err32 = np.abs(act32["normed"] - ref["normed"]).max()
        errfx = np.abs(actfx["quantized"] - ref["quantized"]).max()
        self.assertLess(err32, 1e-5)       # float32 精度量级
        self.assertLessEqual(errfx, 0.01)  # 1/quant_scale 网格量级
        self.assertGreater(errfx, 0.0)


class TestInjection(WorkspaceCase):
    def test_all_injection_points_attributed(self):
        tasks = [
            make_task(task_id="ok1"),
            make_task(task_id="inj-seed"),
            make_task(task_id="inj-shape"),
            make_task(task_id="inj-grid"),
            make_task(task_id="inj-scale", dtype_profile="fixed"),
            make_task(task_id="inj-round", dtype_profile="fixed"),
        ]
        anomalies = [
            {"task_id": "inj-seed", "overrides": {"prompt_seed": -1}},
            {"task_id": "inj-shape", "overrides": {"latent_shape": [4, 0, 32]}},
            {"task_id": "inj-grid", "overrides": {"block_grid": [3, 3]}},
            {"task_id": "inj-scale", "overrides": {"quant_scale": -5.0}},
            {"task_id": "inj-round", "overrides": {"rounding_mode": "magic"}},
        ]
        _, report = self.run_manifest(tasks, anomalies)
        records = {r["task_id"]: r for r in report["error_records"]}
        self.assertEqual(records["inj-seed"]["code"], E_BAD_PROMPT_SEED)
        self.assertEqual(records["inj-seed"]["stage"], "cond_embed")
        self.assertEqual(records["inj-shape"]["code"], E_BAD_LATENT_SHAPE)
        self.assertEqual(records["inj-shape"]["stage"], "noise_construct")
        self.assertEqual(records["inj-grid"]["code"], E_BLOCK_GRID_INDIVISIBLE)
        self.assertEqual(records["inj-grid"]["stage"], "chunk_denoise")
        self.assertEqual(records["inj-scale"]["code"], E_INVALID_QUANT_SCALE)
        self.assertEqual(records["inj-scale"]["stage"], "decode")
        self.assertEqual(records["inj-round"]["code"], E_UNKNOWN_ROUNDING_MODE)
        self.assertEqual(records["inj-round"]["stage"], "decode")
        # 正常任务不受注入影响
        self.assertEqual(report["ok_count"], 1)
        self.assertEqual(report["unattributed_errors"], [])
        # 每条记录带阶段名、业务码、task_id
        for rec in report["error_records"]:
            self.assertTrue(rec["stage"] and rec["code"] and rec["task_id"])


class TestSnapshots(WorkspaceCase):
    def test_version_monotonic_and_whole_replace(self):
        store = SnapshotStore(self.root / "snap")
        s1 = store.update("config", {"a": 1})
        self.assertEqual(s1["version"], 1)
        s2 = store.update("config", {"a": 2, "b": 3})
        self.assertEqual(s2["version"], 2)
        loaded = store.load("config")
        self.assertEqual(loaded["payload"], {"a": 2, "b": 3})  # 整体替换

    def test_rollback_rejected(self):
        store = SnapshotStore(self.root / "snap")
        store.update("config", {"a": 1})
        store.update("config", {"a": 2})
        with self.assertRaises(StageError) as ctx:
            store.save("config", {"a": 1}, version=1)
        self.assertEqual(ctx.exception.code, E_SNAPSHOT_ROLLBACK)
        self.assertEqual(store.load("config")["payload"], {"a": 2})

    def test_corrupt_detected(self):
        store = SnapshotStore(self.root / "snap")
        store.update("quant", {"x": 1})
        path = self.root / "snap" / "quant.snapshot.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["payload"]["x"] = 999  # 篡改内容，校验和不变
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(StageError) as ctx:
            store.load("quant")
        self.assertEqual(ctx.exception.code, E_SNAPSHOT_CORRUPT)


class TestResources(WorkspaceCase):
    def test_block_grid_degraded(self):
        cfg = make_cfg(self.root)
        cfg.resource_limits.max_blocks_per_task = 4
        task = make_task(block_grid=[8, 8])
        records = apply_resource_limits(task, cfg.resource_limits)
        self.assertTrue(task.degraded)
        self.assertEqual(task.degradation_category, DEGRADE_BLOCK_GRID_REDUCED)
        self.assertLessEqual(task.block_grid[0] * task.block_grid[1], 4)
        self.assertEqual(task.original_block_grid, [8, 8])
        self.assertEqual(len(records), 1)

    def test_memory_limit_raises(self):
        cfg = make_cfg(self.root)
        cfg.resource_limits.max_latent_bytes = 8
        with self.assertRaises(StageError) as ctx:
            apply_resource_limits(make_task(), cfg.resource_limits)
        self.assertEqual(ctx.exception.code, E_RESOURCE_LIMIT)

    def test_degradation_in_report(self):
        cfg = make_cfg(self.root)
        cfg.resource_limits.max_blocks_per_task = 4
        _, report = self.run_manifest([make_task(block_grid=[8, 8])], cfg=cfg)
        self.assertEqual(report["ok_count"], 1)
        self.assertEqual(report["degradations"][0]["category"],
                         DEGRADE_BLOCK_GRID_REDUCED)
        self.assertTrue(report["tasks"][0]["degraded"])


class TestThresholds(unittest.TestCase):
    def test_tolerance_band_absorbs_near_threshold(self):
        policy = ThresholdPolicy(compare_strategy=STRATEGY_TOLERANCE_BAND,
                                 tolerance_band=1e-9)
        thr = policy.boundary_residual_threshold
        self.assertFalse(exceeds_threshold(thr, thr, policy))
        self.assertFalse(exceeds_threshold(thr + 5e-10, thr, policy))
        self.assertTrue(exceeds_threshold(thr + 1e-6, thr, policy))

    def test_strict_strategy_raw_compare(self):
        policy = ThresholdPolicy(compare_strategy=STRATEGY_STRICT)
        thr = policy.boundary_residual_threshold
        self.assertFalse(exceeds_threshold(thr, thr, policy))
        self.assertTrue(exceeds_threshold(thr * 1.0000001, thr, policy))

    def test_subnormal_guard(self):
        tiny = np.finfo(np.float64).tiny
        self.assertEqual(subnormal_guard(tiny / 2), 0.0)
        self.assertEqual(subnormal_guard(-tiny / 2), 0.0)
        self.assertEqual(subnormal_guard(1.0), 1.0)

    def test_classification_repeatable(self):
        policy = ThresholdPolicy(compare_strategy=STRATEGY_TOLERANCE_BAND)
        vals = np.linspace(0.0, 2e-8, 17)
        r1 = classify_residuals(vals, 1e-8, policy)
        r2 = classify_residuals(vals, 1e-8, policy)
        self.assertEqual(r1, r2)

    def test_top_k_stable_with_ties(self):
        policy = ThresholdPolicy(compare_strategy=STRATEGY_TOLERANCE_BAND,
                                 tolerance_band=1e-6)
        vals = np.array([0.5, 0.5 + 1e-9, 0.25, 0.5 - 1e-9, 0.1])
        top = top_k_candidates(vals, 3, policy)
        keys = [c["key"] for c in top]
        self.assertEqual(keys, sorted(keys, reverse=True))
        # 并列键按索引升序决胜，结果确定
        self.assertEqual(top_k_candidates(vals, 3, policy), top)


class TestErrorSpectrum(WorkspaceCase):
    def test_fixed_profile_attribution_quantization(self):
        cfg = make_cfg(self.root)
        task = make_task(dtype_profile="fixed", quant_scale=50.0,
                         rounding_mode="half_even")
        _, report = self.run_manifest([task], cfg=cfg)
        att = report["tasks"][0]["attribution"]
        self.assertEqual(att["dominant_source"], "quantization")
        # 归因可由偏差明细复算
        stages = report["tasks"][0]["error_spectrum"]
        recompute = max(0.0, stages["quantize"]["max_abs_dev"]
                        - stages["boundary_norm"]["max_abs_dev"])
        self.assertAlmostEqual(att["source_scores"]["quantization"], recompute)

    def test_float64_spectrum_is_zero(self):
        _, report = self.run_manifest([make_task()])
        stages = report["tasks"][0]["error_spectrum"]
        for name, m in stages.items():
            self.assertEqual(m["max_abs_dev"], 0.0, name)
        self.assertEqual(report["tasks"][0]["attribution"]["dominant_source"],
                         "none")


if __name__ == "__main__":
    unittest.main()
