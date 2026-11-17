from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from recon_workbench.caching import feature_cache_key
from recon_workbench.config import load_and_validate_config
from recon_workbench.features import extract_features
from recon_workbench.geometry import match_and_triangulate_pair
from recon_workbench.injection import EMPTY_INJECTIONS
from recon_workbench.io_views import load_view_manifest
from recon_workbench.pipeline import ReconstructionWorkbench
from recon_workbench.samples import generate_sample_dataset


ROOT = Path(__file__).resolve().parents[1]


class ReconstructionWorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_root_handle = tempfile.TemporaryDirectory(prefix="recon-tests-")
        cls.temp_root = Path(cls.temp_root_handle.name)
        cls.sample_dir = cls.temp_root / "sample"
        generate_sample_dataset(
            cls.sample_dir,
            seed=12345,
            view_count=8,
            width=320,
            height=240,
            marker_count=48,
        )
        default_config = json.loads((ROOT / "configs" / "default.json").read_text(encoding="utf-8"))
        default_config["executor"]["max_workers"] = 4
        cls.config_path = cls.temp_root / "config4.json"
        cls.config_path.write_text(json.dumps(default_config, indent=2), encoding="utf-8")
        serial_config = json.loads(json.dumps(default_config))
        serial_config["executor"]["max_workers"] = 1
        cls.serial_config_path = cls.temp_root / "config1.json"
        cls.serial_config_path.write_text(json.dumps(serial_config, indent=2), encoding="utf-8")
        cls.injection_path = ROOT / "configs" / "injection.json"

        cls.baseline_state = cls.temp_root / "baseline" / "state"
        cls.baseline_report_path = cls.temp_root / "baseline" / "report.json"
        cls.baseline_cloud_path = cls.temp_root / "baseline" / "cloud.json"
        cls.baseline_depth_path = cls.temp_root / "baseline" / "depth_layers.json"
        cls.baseline = cls._run(
            sample=cls.sample_dir,
            config=cls.config_path,
            state=cls.baseline_state,
            report=cls.baseline_report_path,
            cloud=cls.baseline_cloud_path,
            depth=cls.baseline_depth_path,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_root_handle.cleanup()

    @classmethod
    def _run(
        cls,
        *,
        sample: Path,
        config: Path,
        state: Path,
        report: Path,
        cloud: Path,
        depth: Path,
        injection: Path | None = None,
        profile: str | None = None,
        cache: Path | None = None,
        resume: bool = True,
    ):
        workbench = ReconstructionWorkbench(
            input_dir=sample,
            config_path=config,
            injection_path=injection,
            injection_profile=profile,
            state_root=state,
            cache_root=cache,
            report_path=report,
            cloud_out_path=cloud,
            depth_out_path=depth,
            resume=resume,
        )
        return workbench.run()

    def test_01_end_to_end_and_resume_reuse(self) -> None:
        outcome = self.baseline
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.exit_code, 0)
        result = outcome.report["result"]
        self.assertEqual(result["business_codes"], [])
        self.assertEqual(len(result["views"]), 8)
        self.assertTrue(all(view["feature_count"] > 0 for view in result["views"]))
        self.assertGreater(result["match_pair_count"], 0)
        self.assertGreater(result["reconstructed_point_count"], 0)
        self.assertGreater(result["voxel_count"], 0)
        self.assertEqual(result["failures"], [])

        resume_report = self.temp_root / "baseline" / "report_resume.json"
        resumed = self._run(
            sample=self.sample_dir,
            config=self.config_path,
            state=self.baseline_state,
            report=resume_report,
            cloud=self.temp_root / "baseline" / "cloud_resume.json",
            depth=self.temp_root / "baseline" / "depth_layers_resume.json",
        )
        self.assertEqual(resumed.report["result"], result)
        self.assertTrue(resumed.report["run"]["resumed"])
        for kind in ("features", "pairs", "depth", "cloud"):
            self.assertEqual(resumed.report["run"]["cache"][kind]["recomputed"], 0, kind)
        self.assertGreater(resumed.report["run"]["cache"]["features"]["reused"], 0)
        latest = json.loads(
            sorted((self.baseline_state / "snapshots").glob("snapshot-*.json"))[-1].read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(latest["stage"], "merge")
        self.assertTrue(latest["stage_complete"])
        for field in (
            "stage",
            "config_hash",
            "camera_hash",
            "completed_views",
            "partial_pointcloud_summary",
            "snapshot_version",
        ):
            self.assertIn(field, latest)

    def test_02_serial_and_concurrent_results_identical(self) -> None:
        state = self.temp_root / "serial" / "state"
        report = self.temp_root / "serial" / "report.json"
        cloud = self.temp_root / "serial" / "cloud.json"
        depth = self.temp_root / "serial" / "depth_layers.json"
        serial = self._run(
            sample=self.sample_dir,
            config=self.serial_config_path,
            state=state,
            report=report,
            cloud=cloud,
            depth=depth,
        )
        self.assertEqual(serial.report["result"], self.baseline.report["result"])
        self.assertEqual(cloud.read_bytes(), self.baseline_cloud_path.read_bytes())
        self.assertEqual(depth.read_bytes(), self.baseline_depth_path.read_bytes())

    def test_03_reversed_pair_is_identical(self) -> None:
        loaded = load_and_validate_config(self.config_path)
        self.assertIsNotNone(loaded.config)
        manifest = load_view_manifest(self.sample_dir, EMPTY_INJECTIONS)
        views = sorted(manifest.views, key=lambda item: item.view_index)[:2]
        self.assertEqual(len(views), 2)
        features_a = extract_features(
            views[0], loaded.config["feature_detection"], loaded.config["seed"]
        )
        features_b = extract_features(
            views[1], loaded.config["feature_detection"], loaded.config["seed"]
        )
        key_a = feature_cache_key(
            image_hash=views[0].image_hash,
            camera_hash=views[0].camera.hash,
            feature_params=loaded.config["feature_detection"],
            seed=loaded.config["seed"],
            view_index=views[0].view_index,
            injection_hash=EMPTY_INJECTIONS.hash,
        )
        key_b = feature_cache_key(
            image_hash=views[1].image_hash,
            camera_hash=views[1].camera.hash,
            feature_params=loaded.config["feature_detection"],
            seed=loaded.config["seed"],
            view_index=views[1].view_index,
            injection_hash=EMPTY_INJECTIONS.hash,
        )
        common = {
            "matching_params": loaded.config["view_matching"],
            "triangulation_params": loaded.config["triangulation"],
        }
        forward = match_and_triangulate_pair(
            feature_a=features_a,
            feature_b=features_b,
            camera_a=views[0].camera,
            camera_b=views[1].camera,
            feature_key_a=key_a,
            feature_key_b=key_b,
            **common,
        )
        reverse = match_and_triangulate_pair(
            feature_a=features_b,
            feature_b=features_a,
            camera_a=views[1].camera,
            camera_b=views[0].camera,
            feature_key_a=key_b,
            feature_key_b=key_a,
            **common,
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(forward["view_indices"], [views[0].view_index, views[1].view_index])
        self.assertTrue(all("inlier" in match for match in forward["matches"]))

    def test_04_image_change_invalidates_only_dependencies(self) -> None:
        changed_sample = self.temp_root / "image_changed"
        shutil.copytree(self.sample_dir, changed_sample)
        image_path = changed_sample / "views" / "view_0004.png"
        with Image.open(image_path) as image:
            array = np.asarray(image.convert("RGB")).copy()
        array[0, 0] = np.array([255, 0, 255], dtype=np.uint8)
        Image.fromarray(array, mode="RGB").save(image_path, format="PNG", compress_level=6)
        (changed_sample / "sample_manifest.json").unlink()
        outcome = self._run(
            sample=changed_sample,
            config=self.config_path,
            state=self.temp_root / "image_changed_state" / "state",
            cache=self.baseline_state / "cache",
            report=self.temp_root / "image_changed_state" / "report.json",
            cloud=self.temp_root / "image_changed_state" / "cloud.json",
            depth=self.temp_root / "image_changed_state" / "depth_layers.json",
            resume=False,
        )
        self.assertEqual(outcome.status, "completed")
        cache = outcome.report["run"]["cache"]
        self.assertEqual(cache["features"]["recomputed"], 1)
        self.assertEqual(cache["features"]["reused"], 7)
        self.assertEqual(cache["pairs"]["recomputed"], 3)
        self.assertEqual(cache["pairs"]["reused"], 6)
        self.assertEqual(cache["depth"]["recomputed"], 4)
        self.assertEqual(cache["depth"]["reused"], 4)
        self.assertEqual(cache["cloud"]["recomputed"], 1)

    def test_05_depth_parameter_change_invalidates_depth_and_cloud_only(self) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["depth_map"]["smoothing_iterations"] = 3
        changed_config = self.temp_root / "depth_param_config.json"
        changed_config.write_text(json.dumps(config, indent=2), encoding="utf-8")
        outcome = self._run(
            sample=self.sample_dir,
            config=changed_config,
            state=self.temp_root / "depth_param_state" / "state",
            cache=self.baseline_state / "cache",
            report=self.temp_root / "depth_param_state" / "report.json",
            cloud=self.temp_root / "depth_param_state" / "cloud.json",
            depth=self.temp_root / "depth_param_state" / "depth_layers.json",
            resume=False,
        )
        cache = outcome.report["run"]["cache"]
        self.assertEqual(cache["features"]["recomputed"], 0)
        self.assertEqual(cache["pairs"]["recomputed"], 0)
        self.assertEqual(cache["depth"]["recomputed"], 8)
        self.assertEqual(cache["cloud"]["recomputed"], 1)

    def test_06_injection_codes_and_hit_ids(self) -> None:
        recoverable = self._run(
            sample=self.sample_dir,
            config=self.config_path,
            injection=self.injection_path,
            profile="recoverable",
            state=self.temp_root / "injection" / "state",
            report=self.temp_root / "injection" / "report.json",
            cloud=self.temp_root / "injection" / "cloud.json",
            depth=self.temp_root / "injection" / "depth_layers.json",
        )
        result = recoverable.report["result"]
        self.assertEqual(
            set(result["business_codes"]),
            {"E_VIEW_INVALID", "E_MATCH_INSUFFICIENT", "E_NONFINITE_FEATURE"},
        )
        failures = result["failures"]
        self.assertTrue(any(item.get("view_index") == 2 and item["code"] == "E_VIEW_INVALID" for item in failures))
        self.assertTrue(any(item.get("view_index") == 3 and item["code"] == "E_VIEW_INVALID" for item in failures))
        self.assertTrue(any(item.get("view_index") == 6 and item["code"] == "E_NONFINITE_FEATURE" for item in failures))
        self.assertTrue(any(item.get("pair") == [4, 5] and item["code"] == "E_MATCH_INSUFFICIENT" for item in failures))
        hit_ids = {item["id"] for item in recoverable.report["injection"]["hits"]}
        self.assertEqual(hit_ids, {"INJ-PNG-001", "INJ-CAM-002", "INJ-PAR-003", "INJ-NAN-004"})
        self.assertEqual(
            recoverable.report["status"],
            "completed",
            "recoverable per-view errors must not abort the run",
        )

        stage_exceptions = self._run(
            sample=self.sample_dir,
            config=self.config_path,
            injection=self.injection_path,
            profile="stage_exceptions",
            state=self.temp_root / "stage_exception" / "state",
            report=self.temp_root / "stage_exception" / "report.json",
            cloud=self.temp_root / "stage_exception" / "cloud.json",
            depth=self.temp_root / "stage_exception" / "depth_layers.json",
        )
        self.assertEqual(stage_exceptions.report["status"], "completed")
        self.assertIn("E_STAGE_EXCEPTION", stage_exceptions.report["result"]["business_codes"])
        self.assertEqual(
            {item["id"] for item in stage_exceptions.report["injection"]["hits"]},
            {"INJ-EXC-F-005", "INJ-EXC-M-006", "INJ-EXC-D-007"},
        )

        config_invalid = self._run(
            sample=self.sample_dir,
            config=self.config_path,
            injection=self.injection_path,
            profile="config_invalid",
            state=self.temp_root / "config_invalid" / "state",
            report=self.temp_root / "config_invalid" / "report.json",
            cloud=self.temp_root / "config_invalid" / "cloud.json",
            depth=self.temp_root / "config_invalid" / "depth_layers.json",
        )
        self.assertEqual(config_invalid.status, "config_invalid")
        self.assertEqual(config_invalid.exit_code, 2)
        self.assertEqual(config_invalid.report["result"]["business_codes"], ["E_CONFIG_INVALID"])
        self.assertEqual(
            {item["id"] for item in config_invalid.report["injection"]["hits"]},
            {"INJ-VOX-008", "INJ-WRK-009"},
        )

    def test_07_snapshot_config_mismatch_is_rejected(self) -> None:
        self.assertIsNotNone(self.baseline.report["run"]["latest_snapshot_sequence"])
        rejected = self._run(
            sample=self.sample_dir,
            config=self.serial_config_path,
            state=self.baseline_state,
            report=self.temp_root / "mismatch" / "report.json",
            cloud=self.temp_root / "mismatch" / "cloud.json",
            depth=self.temp_root / "mismatch" / "depth_layers.json",
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.exit_code, 3)
        self.assertEqual(rejected.report["result"]["business_codes"], ["E_CONFIG_MISMATCH"])

    def test_08_graceful_sigint_exits_zero_and_resumes(self) -> None:
        run_root = self.temp_root / "sigint"
        state = run_root / "state"
        report = run_root / "interrupt.json"
        command = [
            sys.executable,
            "-m",
            "recon_workbench",
            "run",
            "--input",
            str(self.sample_dir),
            "--config",
            str(self.config_path),
            "--state",
            str(state),
            "--report",
            str(report),
            "--cloud-out",
            str(run_root / "cloud.json"),
            "--depth-out",
            str(run_root / "depth.json"),
            "--test-delay-ms",
            "80",
        ]
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            snapshots = (
                sorted((state / "snapshots").glob("snapshot-*.json"))
                if (state / "snapshots").exists()
                else []
            )
            if len(snapshots) >= 2:
                break
            if process.poll() is not None:
                break
            time.sleep(0.01)
        if process.poll() is None:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertTrue(report.exists())
        interrupted = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(interrupted["status"], "interrupted")
        resume_report = run_root / "resumed.json"
        resumed = self._run(
            sample=self.sample_dir,
            config=self.config_path,
            state=state,
            report=resume_report,
            cloud=run_root / "cloud_resumed.json",
            depth=run_root / "depth_resumed.json",
        )
        self.assertEqual(resumed.report["result"], self.baseline.report["result"])

    def test_09_forced_kill_resumes_from_atomic_snapshot(self) -> None:
        run_root = self.temp_root / "forced_kill"
        state = run_root / "state"
        report = run_root / "killed.json"
        command = [
            sys.executable,
            "-m",
            "recon_workbench",
            "run",
            "--input",
            str(self.sample_dir),
            "--config",
            str(self.config_path),
            "--state",
            str(state),
            "--report",
            str(report),
            "--cloud-out",
            str(run_root / "cloud_killed.json"),
            "--depth-out",
            str(run_root / "depth_killed.json"),
            "--test-delay-ms",
            "150",
        ]
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        observed: tuple[int, str, bool] | None = None
        deadline = time.time() + 20
        while time.time() < deadline:
            snapshots = (
                sorted((state / "snapshots").glob("snapshot-*.json"))
                if (state / "snapshots").exists()
                else []
            )
            if snapshots:
                snapshot = json.loads(snapshots[-1].read_text(encoding="utf-8"))
                observed = (
                    int(snapshot["sequence"]),
                    str(snapshot["stage"]),
                    bool(snapshot["stage_complete"]),
                )
                if snapshot["stage"] == "matching" and not snapshot["stage_complete"]:
                    break
            if process.poll() is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(observed)
        self.assertEqual(observed[1], "matching")
        self.assertFalse(observed[2])
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(report.exists(), "a forcibly killed process must not promise a report")
        resumed = self._run(
            sample=self.sample_dir,
            config=self.config_path,
            state=state,
            report=run_root / "resumed.json",
            cloud=run_root / "cloud_resumed.json",
            depth=run_root / "depth_resumed.json",
        )
        self.assertEqual(resumed.report["result"], self.baseline.report["result"])
        self.assertTrue(resumed.report["run"]["recovery_point"]["mode"] == "resume")
        self.assertEqual(
            resumed.report["run"]["recovery_point"]["stage"], "matching"
        )
        self.assertGreater(
            resumed.report["run"]["cache"]["pairs"]["reused"], 0
        )

    def test_10_view_validation_continues_after_bad_views(self) -> None:
        broken = self.temp_root / "broken_views"
        shutil.copytree(self.sample_dir, broken)
        # Duplicate view index under a different filename that still parses.
        shutil.copyfile(broken / "views" / "view_0001.png", broken / "views" / "view_1.png")
        # Inconsistent resolution relative to the common sample resolution.
        with Image.open(broken / "views" / "view_0005.png") as source:
            reduced = source.convert("RGB").resize((160, 120))
        reduced.save(broken / "views" / "view_0005.png", format="PNG")
        # Missing camera field.
        camera_path = broken / "cameras" / "view_0006.json"
        camera = json.loads(camera_path.read_text(encoding="utf-8"))
        camera.pop("translation")
        camera_path.write_text(json.dumps(camera, indent=2), encoding="utf-8")
        manifest = load_view_manifest(broken, EMPTY_INJECTIONS)
        invalid_views = {
            failure.view_index
            for failure in manifest.failures
            if failure.code == "E_VIEW_INVALID"
        }
        self.assertIn(1, invalid_views)
        self.assertIn(5, invalid_views)
        self.assertIn(6, invalid_views)
        self.assertEqual(
            {view.view_index for view in manifest.views},
            {2, 3, 4, 7, 8},
        )
        outcome = self._run(
            sample=broken,
            config=self.config_path,
            state=self.temp_root / "broken_views_state" / "state",
            report=self.temp_root / "broken_views_state" / "report.json",
            cloud=self.temp_root / "broken_views_state" / "cloud.json",
            depth=self.temp_root / "broken_views_state" / "depth_layers.json",
        )
        self.assertEqual(outcome.status, "completed")
        self.assertIn("E_VIEW_INVALID", outcome.report["result"]["business_codes"])
        self.assertEqual(
            sorted(
                {
                    failure["view_index"]
                    for failure in outcome.report["result"]["failures"]
                    if failure["code"] == "E_VIEW_INVALID"
                }
            ),
            [1, 5, 6],
        )


if __name__ == "__main__":
    unittest.main()
