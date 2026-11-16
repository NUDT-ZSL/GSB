"""End-to-end and unit tests for the multimodal reasoning pipeline."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from mm_pipeline import ReasoningEngine, generate_dataset
from mm_pipeline.config import PipelineConfig
from mm_pipeline.features import FORCE_FAILURE, extract_visual_features
from mm_pipeline.persistence import (E_CONFIG_MISMATCH, SnapshotError,
                                     load_snapshot, snapshot_path)
from mm_pipeline.png_codec import decode_png, encode_png
from mm_pipeline.sample import discover_samples

REPO_ROOT = Path(__file__).resolve().parents[1]


class TempWorkspace:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="mm_pipeline_test_"))
        self.input_dir = self.root / "input"
        self.snapshot_dir = self.root / "snapshots"
        self.report_path = self.root / "report.json"

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        generate_dataset(self.ws.input_dir)

    def tearDown(self):
        FORCE_FAILURE.clear()
        self.ws.cleanup()

    def run_engine(self, exit_after_round=None):
        engine = ReasoningEngine(
            input_dir=self.ws.input_dir,
            snapshot_dir=self.ws.snapshot_dir,
            report_path=self.ws.report_path,
            exit_after_round=exit_after_round,
        )
        return engine.run()

    def read_report(self):
        return json.loads(self.ws.report_path.read_text(encoding="utf-8"))


class TestPngCodec(unittest.TestCase):
    def test_roundtrip_grayscale_rgb_rgba(self):
        rng = np.random.default_rng(7)
        rgb = rng.integers(0, 256, (11, 19, 3), dtype=np.uint8)
        gray = rgb[:, :, 0]
        rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, dtype=np.uint8)])
        for image in (rgb, gray, rgba):
            decoded = decode_png(encode_png(image))
            self.assertTrue(np.array_equal(decoded, image))

    def test_rejects_non_png_header(self):
        with self.assertRaises(Exception):
            decode_png(b"not a png")


class TestVisualDeterminism(WorkspaceTestCase):
    def test_feature_vectors_identical_across_runs_and_quantization(self):
        samples, failures = discover_samples(self.ws.input_dir)
        self.assertTrue(samples)
        cfg = PipelineConfig.load(self.ws.input_dir / "config.json")
        first = extract_visual_features(samples[0], cfg)
        second = extract_visual_features(samples[0], cfg)
        for region_a, region_b in zip(first["regions"], second["regions"]):
            np.testing.assert_array_equal(
                region_a["quantized"], region_b["quantized"])
            np.testing.assert_array_equal(region_a["vector"],
                                          region_b["vector"])
            self.assertEqual(len(region_a["bbox"]), 4)

        other_raw = json.loads((self.ws.input_dir / "config.json").read_text())
        other_raw["quantization_levels"] = cfg.quantization_levels * 2
        other_path = self.ws.root / "other_config.json"
        other_path.write_text(json.dumps(other_raw))
        other_cfg = PipelineConfig.load(other_path)
        changed = extract_visual_features(samples[0], other_cfg)
        self.assertFalse(np.array_equal(
            first["regions"][0]["quantized"],
            changed["regions"][0]["quantized"]))


class TestLoadingAndRelations(WorkspaceTestCase):
    def test_invalid_samples_recorded_and_processing_continues(self):
        samples, failures = discover_samples(self.ws.input_dir)
        names = {sample.name for sample in samples}
        self.assertIn("sample_fast", names)
        invalid = {item["sample_name"]: item for item in failures}
        self.assertEqual(
            {name for name in invalid if name.startswith("sample_bad")},
            {"sample_bad_header", "sample_bad_json", "sample_bad_missing"})
        for item in invalid.values():
            self.assertEqual(item["code"], "E_SAMPLE_INVALID")
            self.assertTrue(item["stage"])

    def test_dangling_relations_skipped_but_others_remain(self):
        result = self.run_engine()
        report = result["report"]
        dangling = [w for w in report["relation_warnings"]
                    if w["sample_name"] == "sample_dangling"]
        self.assertEqual(len(dangling), 2)
        for warning in dangling:
            self.assertEqual(warning["code"], "E_RELATION_DANGLING")
        ids = {w["relation_id"] for w in dangling}
        self.assertEqual(
            ids, {"rel-dangling-region", "rel-dangling-assertion"})
        dangling_result = next(
            s for s in report["sample_results"]
            if s["sample_name"] == "sample_dangling")
        active_relation_ids = {
            c["relation_id"]
            for rnd in dangling_result["evidence_contribution_breakdown"]
            for c in rnd["evidence_contributions"]}
        self.assertIn("rel-color", active_relation_ids)
        self.assertNotIn("rel-dangling-region", active_relation_ids)


class TestFusionBehavior(WorkspaceTestCase):
    def test_fast_and_slow_convergence_and_forced_max_rounds(self):
        report = self.run_engine()["report"]
        by_name = {s["sample_name"]: s for s in report["sample_results"]}
        self.assertEqual(
            len(by_name["sample_fast"]["round_score_trajectory"]), 1)
        self.assertLessEqual(
            len(by_name["sample_slow"]["round_score_trajectory"]),
            PipelineConfig.load(
                self.ws.input_dir / "config.json").max_evidence_rounds)
        self.assertEqual(
            len(by_name["sample_conflict"]["round_score_trajectory"]), 4)
        self.assertEqual(by_name["sample_conflict"]["joint_conclusion"],
                         "mismatch")
        self.assertEqual(by_name["sample_fast"]["joint_conclusion"], "match")

    def test_evidence_contributions_keep_relation_order(self):
        report = self.run_engine()["report"]
        fast = next(s for s in report["sample_results"]
                    if s["sample_name"] == "sample_fast")
        first_round = fast["evidence_contribution_breakdown"][0]
        orders = [c["source_order"]
                  for c in first_round["evidence_contributions"]]
        self.assertEqual(orders, sorted(orders))
        for contribution in first_round["evidence_contributions"]:
            self.assertTrue(contribution["relation_id"])


class TestSnapshotRecovery(WorkspaceTestCase):
    def test_resume_matches_uninterrupted_field_by_field(self):
        full = self.run_engine()["report"]

        interrupted_ws = TempWorkspace()
        try:
            generate_dataset(interrupted_ws.input_dir)
            engine = ReasoningEngine(
                interrupted_ws.input_dir, interrupted_ws.snapshot_dir,
                interrupted_ws.root / "partial.json", exit_after_round=2)
            partial = engine.run()
            self.assertTrue(partial["graceful_exit"])
            snapshot = load_snapshot(interrupted_ws.snapshot_dir)
            self.assertEqual(snapshot["current_round"], 2)
            self.assertTrue(snapshot_path(
                interrupted_ws.snapshot_dir).exists())

            resumed_engine = ReasoningEngine(
                interrupted_ws.input_dir, interrupted_ws.snapshot_dir,
                interrupted_ws.report_path)
            resumed = resumed_engine.run()["report"]
            self.assertTrue(resumed["resumed"])
        finally:
            name = interrupted_ws
            interrupted_ws.cleanup()

        for field in ("sample_results", "failures", "relation_warnings",
                      "config_hash", "relations_hash", "snapshot_version",
                      "current_round"):
            self.assertEqual(resumed[field], full[field],
                             f"field differs after resume: {field}")

    def test_config_change_rejected_with_e_config_mismatch(self):
        self.run_engine(exit_after_round=1)
        cfg_path = self.ws.input_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["fusion_temperature"] = cfg["fusion_temperature"] + 0.5
        cfg_path.write_text(json.dumps(cfg))
        with self.assertRaises(SnapshotError) as ctx:
            self.run_engine()
        self.assertIn(E_CONFIG_MISMATCH, str(ctx.exception))

    def test_relation_change_rejected_with_e_config_mismatch(self):
        self.run_engine(exit_after_round=1)
        relation_path = (self.ws.input_dir / "sample_fast" /
                         "relations.json")
        relations = json.loads(relation_path.read_text())
        relations[0]["weight"] = 0.123
        relation_path.write_text(json.dumps(relations))
        with self.assertRaises(SnapshotError) as ctx:
            self.run_engine()
        self.assertIn(E_CONFIG_MISMATCH, str(ctx.exception))

    def test_no_partial_temp_snapshot_left_behind(self):
        self.run_engine()
        leftovers = list(self.ws.snapshot_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class TestConcurrencyConsistency(WorkspaceTestCase):
    def _run_with_concurrency(self, concurrency: int, workspaces):
        cfg_path = self.ws.input_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["concurrency"] = concurrency
        cfg_path.write_text(json.dumps(cfg))
        run_ws = TempWorkspace()
        workspaces.append(run_ws)
        shutil.copytree(self.ws.input_dir, run_ws.input_dir,
                        dirs_exist_ok=True)
        engine = ReasoningEngine(
            run_ws.input_dir, run_ws.snapshot_dir, run_ws.report_path)
        report = engine.run()["report"]
        report["_snapshot_dir"] = str(run_ws.snapshot_dir)
        return report

    def test_threadpool_matches_single_thread_output(self):
        workspaces = []
        try:
            serial = self._run_with_concurrency(1, workspaces)
            parallel = self._run_with_concurrency(4, workspaces)
            self.assertEqual(
                [s["sample_name"] for s in serial["sample_results"]],
                [s["sample_name"] for s in parallel["sample_results"]])
            for serial_sample, parallel_sample in zip(
                    serial["sample_results"], parallel["sample_results"]):
                self.assertEqual(
                    serial_sample["evidence_contribution_breakdown"],
                    parallel_sample["evidence_contribution_breakdown"])
                self.assertEqual(
                    serial_sample["round_score_trajectory"],
                    parallel_sample["round_score_trajectory"])
            self.assertEqual(serial["failures"], parallel["failures"])
            self.assertEqual(serial["relation_warnings"],
                             parallel["relation_warnings"])
            snapshot = load_snapshot(parallel["_snapshot_dir"])
            self.assertEqual(
                [s["sample_name"] for s in snapshot["sample_states"]],
                [s["sample_name"] for s in parallel["sample_results"]])
        finally:
            for run_ws in workspaces:
                run_ws.cleanup()


class TestExceptionChain(WorkspaceTestCase):
    def test_sample_failure_excludes_later_conclusions(self):
        FORCE_FAILURE["sample_fast"] = "fusion"
        report = self.run_engine()["report"]
        failure = next(f for f in report["failures"]
                       if f["sample_name"] == "sample_fast")
        self.assertEqual(failure["stage"], "fusion")
        self.assertEqual(failure["code"], "E_FUSION_FAILED")
        self.assertTrue(failure["exception_type"])
        fast = next(s for s in report["sample_results"]
                    if s["sample_name"] == "sample_fast")
        self.assertEqual(fast["status"], "failed")
        self.assertIsNone(fast["joint_conclusion"])
        self.assertEqual(fast["round_score_trajectory"], [])
        others = [s for s in report["sample_results"]
                  if s["sample_name"] != "sample_fast"]
        self.assertTrue(others)
        self.assertTrue(all(s["status"] == "concluded" for s in others))


class TestGracefulSignal(unittest.TestCase):
    def test_sigterm_finishes_round_and_exits_zero(self):
        ws = TempWorkspace()
        try:
            generate_dataset(ws.input_dir)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO_ROOT)
            cmd = [sys.executable, "-m", "mm_pipeline.cli", "run",
                   "--input-dir", str(ws.input_dir),
                   "--snapshot-dir", str(ws.snapshot_dir),
                   "--report", str(ws.report_path),
                   "--exit-after-round", "1"]
            proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            snapshot = load_snapshot(ws.snapshot_dir)
            self.assertEqual(snapshot["current_round"], 1)
            self.assertTrue(snapshot["graceful_exit"])
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
