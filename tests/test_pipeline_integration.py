"""端到端集成测试：完整运行、四阶段中断恢复、配置哈希不一致与快照原子性。"""

import copy
import json
import os
import shutil
import unittest

from xling_pipeline.config import load_config
from xling_pipeline.errors import E_CONFIG_MISMATCH, ConfigMismatch
from xling_pipeline.runner import ExitController, PipelineRunner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "configs", "pipeline_config.json")
WORK_ROOT = os.path.join(ROOT, "data", "output", "_unittest_work")


def _work_config(name, fault=None):
    config = copy.deepcopy(load_config(CONFIG_PATH))
    snap_dir = os.path.join(WORK_ROOT, name, "snapshots")
    report_path = os.path.join(WORK_ROOT, name, "report.json")
    config["paths"]["snapshot_dir"] = snap_dir
    config["paths"]["report_path"] = report_path
    config.pop("fault_injection", None)
    if fault:
        config["fault_injection"] = fault
    return config


class PipelineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(WORK_ROOT, ignore_errors=True)
        os.makedirs(WORK_ROOT, exist_ok=True)

    def _run_to_completion(self, config):
        runner = PipelineRunner(config, ExitController())
        outcome = runner.run()
        self.assertEqual(outcome["status"], "complete")
        with open(config["paths"]["report_path"], encoding="utf-8") as handle:
            return json.load(handle)

    def test_baseline_completes_with_expected_invalid_records(self):
        config = _work_config("baseline")
        report = self._run_to_completion(config)
        self.assertEqual(report["failure_count"], 5)
        self.assertEqual(report["invalid_items"], 5)
        self.assertEqual(len(report["retrieval_results"]), 14)
        codes = {f["code"] for f in report["failures"]}
        self.assertEqual(codes, {"E_RECORD_INVALID"})
        # 翻译抑制路径被触发
        suppressed_queries = [
            q for q in report["retrieval_results"] if q["suppressed_translations"]
        ]
        self.assertGreater(len(suppressed_queries), 0)
        # 确定性：再跑一次结果完全一致
        rerun = self._run_to_completion(config)
        self.assertEqual(
            rerun["retrieval_results"], report["retrieval_results"]
        )
        self.assertEqual(rerun["metrics"], report["metrics"])

    def test_interrupt_resume_every_stage_matches_baseline(self):
        baseline = self._run_to_completion(_work_config("baseline_ref"))
        for stage in ("normalize", "index", "retrieval", "evaluation"):
            with self.subTest(stage=stage):
                config = _work_config(
                    f"interrupt_{stage}",
                    fault={"exit_after_stage": stage, "exit_after_batches": 1},
                )
                first = PipelineRunner(config, ExitController()).run()
                self.assertEqual(first["status"], "interrupted")
                self.assertFalse(os.path.exists(config["paths"]["report_path"]))
                # 续跑直到完成（注入标记已持久化，不会重复触发）
                second = PipelineRunner(config, ExitController()).run()
                self.assertEqual(second["status"], "complete")
                with open(config["paths"]["report_path"], encoding="utf-8") as handle:
                    report = json.load(handle)
                self.assertEqual(
                    report["retrieval_results"], baseline["retrieval_results"]
                )
                self.assertEqual(report["metrics"], baseline["metrics"])
                self.assertEqual(report["failures"], baseline["failures"])
                self.assertEqual(
                    report["stage_batches"], baseline["stage_batches"]
                )
                # 批次号无重复无遗漏
                for stage_name, batches in report["stage_batches"].items():
                    nos = [b["batch_no"] for b in batches]
                    self.assertEqual(nos, sorted(nos))
                    self.assertEqual(len(nos), len(set(nos)))

    def test_config_mismatch_rejects_resume(self):
        config = _work_config(
            "mismatch",
            fault={"exit_after_stage": "retrieval", "exit_after_batches": 1},
        )
        PipelineRunner(config, ExitController()).run()
        altered = copy.deepcopy(config)
        altered["bm25"]["k1"] = 2.2
        with self.assertRaises(ConfigMismatch) as context:
            PipelineRunner(altered, ExitController()).resume_if_possible()
        self.assertEqual(context.exception.code, E_CONFIG_MISMATCH)

    def test_translation_hash_mismatch_rejects_resume(self):
        config = _work_config(
            "translation_mismatch",
            fault={"exit_after_stage": "normalize", "exit_after_batches": 1},
        )
        PipelineRunner(config, ExitController()).run()
        altered = copy.deepcopy(config)
        # 直接篡改内存中的翻译表哈希校验路径
        runner = PipelineRunner(altered, ExitController())
        runner.trans_hash = "0" * 64
        with self.assertRaises(ConfigMismatch):
            runner.resume_if_possible()

    def test_snapshot_files_are_versioned_and_atomic(self):
        config = _work_config("snapshot_files")
        PipelineRunner(config, ExitController()).run()
        snap_dir = config["paths"]["snapshot_dir"]
        files = sorted(f for f in os.listdir(snap_dir) if f.startswith("snapshot_v"))
        self.assertGreater(len(files), 1)
        with open(os.path.join(snap_dir, "latest.json"), encoding="utf-8") as handle:
            latest = json.load(handle)
        self.assertTrue(
            os.path.exists(
                os.path.join(
                    snap_dir, f"snapshot_v{latest['latest_version']:06d}.json"
                )
            )
        )
        # 不存在残留临时文件
        leftovers = [f for f in os.listdir(snap_dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        # 每个快照具备必备信封字段
        for name in files:
            with open(os.path.join(snap_dir, name), encoding="utf-8") as handle:
                payload = json.load(handle)
            for field in (
                "snapshot_version", "config_hash", "translation_hash",
                "stage", "completed_batches", "version", "state", "summary",
            ):
                self.assertIn(field, payload)


if __name__ == "__main__":
    unittest.main()
