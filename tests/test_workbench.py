from __future__ import annotations

import copy
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from PIL import Image

from scripts.generate_samples import generate_all
from video_workbench.config import load_config_file
from video_workbench.interactive import InteractiveController
from video_workbench.pipeline import Renderer
from video_workbench.util import decode_npz


ROOT = Path(__file__).resolve().parents[1]


def load_raw(name: str) -> tuple[dict[str, Any], Path]:
    raw, base = load_config_file(ROOT / "samples" / name, ROOT)
    return copy.deepcopy(raw), base


def with_output(raw: Mapping[str, Any], output_dir: Path, *, workers: int | None = None) -> dict[str, Any]:
    updated = copy.deepcopy(dict(raw))
    updated["output_dir"] = str(output_dir)
    if workers is not None:
        updated["max_workers"] = workers
    return updated


def artifact_arrays(output_dir: Path, prefixes: tuple[str, ...] = ("init", "denoise_")) -> dict[tuple[str, str], np.ndarray]:
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    result: dict[tuple[str, str], np.ndarray] = {}
    for stage, record in sorted(manifest["artifacts"].items()):
        if not stage.startswith(prefixes):
            continue
        _, arrays = decode_npz((output_dir / record["path"]).read_bytes())
        for name, value in sorted(arrays.items()):
            result[(stage, name)] = value
    return result


def assert_array_catalogs_equal(testcase: unittest.TestCase, left: dict[tuple[str, str], np.ndarray], right: dict[tuple[str, str], np.ndarray]) -> None:
    testcase.assertEqual(sorted(left), sorted(right))
    for key in sorted(left):
        testcase.assertTrue(np.array_equal(left[key], right[key]), msg=f"array mismatch for {key}")


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition was not reached before timeout")


class WorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        generate_all()

    def test_validation_business_codes_and_no_i2v_fallback(self) -> None:
        raw, base = load_raw("t2v.json")
        renderer = Renderer(base_dir=base)
        with tempfile.TemporaryDirectory(prefix="wb-validation-") as temporary:
            root = Path(temporary)

            cases: list[tuple[str, dict[str, Any], str]] = []
            case = copy.deepcopy(raw)
            case["output_dir"] = str(root / "seed")
            case["seed"] = 2**32
            cases.append(("seed", case, "E_CONFIG_INVALID"))

            case = copy.deepcopy(raw)
            case["output_dir"] = str(root / "guidance")
            case["guidance"] = math.nan
            cases.append(("guidance", case, "E_CONFIG_INVALID"))

            case = copy.deepcopy(raw)
            case["output_dir"] = str(root / "missing")
            case.pop("prompt")
            cases.append(("missing", case, "E_CONFIG_INVALID"))

            case = copy.deepcopy(raw)
            case["output_dir"] = str(root / "noise")
            case["steps"] = 5
            cases.append(("noise", case, "E_NOISE_MISMATCH"))

            case = copy.deepcopy(raw)
            case["output_dir"] = str(root / "patch")
            case["patch"] = {
                "step": 1,
                "frame": 1,
                "x": 7,
                "y": 2,
                "values": [[[0.0] * 4, [0.0] * 4], [[0.0] * 4, [0.0] * 4]],
            }
            cases.append(("patch", case, "E_PATCH_OUT_OF_RANGE"))

            case = copy.deepcopy(raw)
            case["output_dir"] = str(root / "patch-negative")
            case["patch"] = {
                "step": 0,
                "frame": 0,
                "x": -1,
                "y": 0,
                "values": [[[0.0] * 4, [0.0] * 4]],
            }
            cases.append(("patch-negative", case, "E_PATCH_OUT_OF_RANGE"))

            case = copy.deepcopy(raw)
            case["output_dir"] = str(root / "init-missing")
            case["mode"] = "i2v"
            case["init_image_path"] = str(root / "missing.png")
            case["i2v_replacement_steps"] = 1
            cases.append(("init-missing", case, "E_INITIMAGE_INVALID"))

            case = copy.deepcopy(raw)
            case["output_dir"] = str(root / "init-field-missing")
            case["mode"] = "i2v"
            case["i2v_replacement_steps"] = 1
            cases.append(("init-field-missing", case, "E_INITIMAGE_INVALID"))

            wrong_image = root / "wrong-size.png"
            Image.fromarray(np.zeros((16, 16, 3), dtype=np.uint8), mode="RGB").save(wrong_image)
            case = copy.deepcopy(raw)
            case["output_dir"] = str(root / "init-size")
            case["mode"] = "i2v"
            case["init_image_path"] = str(wrong_image)
            case["i2v_replacement_steps"] = 1
            cases.append(("init-size", case, "E_INITIMAGE_INVALID"))

            for name, config, expected in cases:
                with self.subTest(name=name):
                    result = renderer.render(config, allow_reuse=False)
                    self.assertEqual(result.primary_code, expected)
                    self.assertEqual(result.status, "failed")
                    self.assertIn(expected, result.business_codes)
                    if expected == "E_INITIMAGE_INVALID":
                        self.assertEqual(result.frames.size, 0)
                        self.assertNotEqual(result.primary_code, "OK")

    def test_concurrent_and_serial_results_are_identical(self) -> None:
        raw, base = load_raw("t2v.json")
        raw["inject_failures"] = [
            {"stage": "denoise", "step": 1, "frame": 2, "error_type": "RuntimeError"},
            {"stage": "decode", "frame": 3, "error_type": "ValueError"},
        ]
        with tempfile.TemporaryDirectory(prefix="wb-concurrency-") as temporary:
            root = Path(temporary)
            renderer = Renderer(base_dir=base)
            parallel = renderer.render(with_output(raw, root / "parallel", workers=4), allow_reuse=False)
            serial = renderer.render(with_output(raw, root / "serial", workers=1), allow_reuse=False)
            self.assertEqual(parallel.status, "partial")
            self.assertEqual(serial.status, "partial")
            self.assertEqual(parallel.business_codes, ["E_STAGE_FAILED"])
            self.assertTrue(np.array_equal(parallel.frames, serial.frames))
            self.assertTrue(np.array_equal(parallel.frame_valid_mask, serial.frame_valid_mask))
            self.assertEqual(parallel.summary, serial.summary)
            self.assertEqual(parallel.failures, serial.failures)
            assert_array_catalogs_equal(self, artifact_arrays(root / "parallel"), artifact_arrays(root / "serial"))
            self.assertTrue((root / "parallel" / "frames" / "frame_000.png").exists())
            self.assertTrue((root / "serial" / "frames" / "frame_003.png").exists())
            self.assertEqual(
                [(item["stage"], item["step"], item["frame"], item["error_type"]) for item in parallel.failures],
                [(item["stage"], item["step"], item["frame"], item["error_type"]) for item in serial.failures],
            )

    def test_incremental_full_equivalence_both_modes_and_patch_invalidation(self) -> None:
        raw, base = load_raw("t2v.json")
        renderer = Renderer(base_dir=base)
        patch_values = [
            [[0.25, -0.15, 0.40, 0.05], [0.35, -0.05, 0.30, 0.15]],
            [[0.45, 0.05, 0.20, 0.25], [0.55, 0.15, 0.10, 0.35]],
        ]
        with tempfile.TemporaryDirectory(prefix="wb-incremental-") as temporary:
            root = Path(temporary)
            config = with_output(raw, root / "incremental")
            sequence = [
                {"prompt": "a blue bird flies over a calm lake at dawn"},
                {"seed": 2026},
                {"guidance": 2.75},
                {"frames": 3},
                {"steps": 3, "noise_table_path": "samples/noise_table_3.npy"},
            ]
            for update in sequence:
                config.update(update)
                incremental_result = renderer.render(copy.deepcopy(config))
                self.assertEqual(incremental_result.status, "completed")

            t2v_full_config = with_output(config, root / "full-t2v", workers=1)
            t2v_full = renderer.render(t2v_full_config, allow_reuse=False)
            self.assertTrue(np.array_equal(incremental_result.frames, t2v_full.frames))
            assert_array_catalogs_equal(self, artifact_arrays(root / "incremental"), artifact_arrays(root / "full-t2v"))

            config.update(
                {
                    "mode": "i2v",
                    "init_image_path": "samples/init_image.png",
                    "i2v_replacement_steps": 1,
                }
            )
            i2v_base = renderer.render(copy.deepcopy(config))
            self.assertEqual(i2v_base.status, "completed")

            config["patch"] = {"step": 1, "frame": 1, "x": 1, "y": 2, "values": patch_values}
            patched = renderer.render(copy.deepcopy(config))
            self.assertEqual(patched.status, "completed")
            self.assertTrue({"encode", "init", "denoise_000"}.issubset(set(patched.reused_stages)))
            self.assertIn("denoise_001", patched.invalidated_stages)
            self.assertIn("denoise_002", patched.invalidated_stages)
            self.assertIn("decode_frame_000", patched.invalidated_stages)
            self.assertIn("preview", patched.invalidated_stages)

            patched_full = renderer.render(
                with_output(config, root / "full-patched", workers=1),
                allow_reuse=False,
            )
            self.assertTrue(np.array_equal(patched.frames, patched_full.frames))
            assert_array_catalogs_equal(
                self,
                artifact_arrays(root / "incremental"),
                artifact_arrays(root / "full-patched"),
            )

            config["patch"] = None
            renderer.render(copy.deepcopy(config))
            config.update({"steps": 4, "noise_table_path": "samples/noise_table_4.npy"})
            final_incremental = renderer.render(copy.deepcopy(config))
            final_full = renderer.render(
                with_output(config, root / "full-i2v", workers=1),
                allow_reuse=False,
            )
            self.assertTrue(np.array_equal(final_incremental.frames, final_full.frames))
            self.assertTrue(np.array_equal(final_incremental.frame_valid_mask, final_full.frame_valid_mask))
            assert_array_catalogs_equal(self, artifact_arrays(root / "incremental"), artifact_arrays(root / "full-i2v"))

    def test_corrupt_artifact_recomputes_stage_and_downstream(self) -> None:
        raw, base = load_raw("t2v.json")
        renderer = Renderer(base_dir=base)
        with tempfile.TemporaryDirectory(prefix="wb-corrupt-") as temporary:
            output = Path(temporary) / "out"
            config = with_output(raw, output)
            baseline = renderer.render(config, allow_reuse=False)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            artifact_path = output / manifest["artifacts"]["denoise_001"]["path"]
            with artifact_path.open("r+b") as handle:
                handle.seek(32)
                handle.write(b"CORRUPTED")
            recovered = renderer.render(config)
            self.assertEqual(recovered.status, "completed")
            self.assertTrue(np.array_equal(baseline.frames, recovered.frames))
            self.assertTrue({"encode", "init", "denoise_000"}.issubset(set(recovered.reused_stages)))
            self.assertIn("denoise_001", recovered.invalidated_stages)
            self.assertIn("denoise_002", recovered.invalidated_stages)
            self.assertIn("decode_frame_000", recovered.invalidated_stages)
            report = json.loads(Path(recovered.report_path).read_text(encoding="utf-8"))
            self.assertEqual(
                report["invalidation_reasons"]["denoise_001"],
                "artifact_file_fingerprint_mismatch",
            )

    def test_preview_budget_pending_reuses_stale_preview_and_keeps_background(self) -> None:
        raw, base = load_raw("t2v.json")
        with tempfile.TemporaryDirectory(prefix="wb-interactive-") as temporary:
            root = Path(temporary)
            raw = with_output(raw, root / "interactive")
            raw["steps"] = 2
            raw["frames"] = 2
            raw["noise_table_path"] = "samples/noise_table_2.npy"
            raw["stage_delay_ms"] = 100
            controller = InteractiveController(raw, base_dir=base)
            try:
                start = time.monotonic()
                first = controller.submit({"set": {"seed": 7}}, preview_budget_ms=5)
                first_elapsed = time.monotonic() - start
                self.assertEqual(first["business_code"], "E_PREVIEW_BUDGET_PENDING")
                self.assertTrue(first["background"]["continues"])
                self.assertFalse(first["background"]["render_terminated"])
                self.assertFalse(first["background"]["discarded"])
                self.assertLess(first_elapsed, 0.15)
                first_result = controller.wait_job(first["job_id"], timeout=20)
                self.assertEqual(first_result.status, "completed")
                self.assertIsNotNone(first_result.preview)
                self.assertTrue(Path(first_result.preview["path"]).exists())
                first_preview_version = first_result.preview["snapshot_version"]

                second = controller.submit({"set": {"seed": 8}}, preview_budget_ms=5)
                self.assertEqual(second["business_code"], "E_PREVIEW_BUDGET_PENDING")
                self.assertTrue(second["preview_stale"])
                self.assertIsNotNone(second["preview"])
                self.assertEqual(second["preview"]["snapshot_version"], first_preview_version)
                self.assertTrue(second["background"]["continues"])
                second_result = controller.wait_job(second["job_id"], timeout=20)
                self.assertEqual(second_result.status, "completed")
                status = controller.status()
                self.assertEqual(status["latest_preview"]["config_fingerprint"], second_result.config_fingerprint)
            finally:
                controller.close()

    def test_graceful_signal_and_sigkill_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wb-signals-") as temporary:
            root = Path(temporary)
            graceful_out = root / "graceful"
            graceful_command = [
                sys.executable,
                "-m",
                "video_workbench.cli",
                "render",
                "--config",
                "samples/t2v.json",
                "--output-dir",
                str(graceful_out),
                "--no-reuse",
                "--set-json",
                '{"stage_delay_ms":180}',
            ]
            process = subprocess.Popen(
                graceful_command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def graceful_ready() -> bool:
                try:
                    manifest = json.loads((graceful_out / "manifest.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return False
                return manifest.get("progress", {}).get("completed_stages", 0) >= 2

            wait_until(graceful_ready)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, msg=stderr)
            self.assertIn("graceful_exit_requested", stderr)
            report = json.loads((graceful_out / "diagnostic_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "interrupted")
            self.assertIn("E_INTERRUPTED", report["business_codes"])
            manifest = json.loads((graceful_out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "interrupted")
            parsed = json.loads(stdout.strip().splitlines()[-1])
            self.assertEqual(parsed["status"], "interrupted")

            killed_out = root / "killed"
            killed_command = [
                sys.executable,
                "-m",
                "video_workbench.cli",
                "render",
                "--config",
                "samples/t2v.json",
                "--output-dir",
                str(killed_out),
                "--no-reuse",
                "--set-json",
                '{"stage_delay_ms":180}',
            ]
            process = subprocess.Popen(
                killed_command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def kill_ready() -> bool:
                try:
                    manifest = json.loads((killed_out / "manifest.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return False
                return manifest.get("progress", {}).get("completed_stages", 0) >= 2

            wait_until(kill_ready)
            os.kill(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, -9)
            self.assertFalse((killed_out / "diagnostic_report.json").exists())
            killed_manifest = json.loads((killed_out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(killed_manifest["status"], "running")

            resume_command = [
                sys.executable,
                "-m",
                "video_workbench.cli",
                "render",
                "--config",
                "samples/t2v.json",
                "--output-dir",
                str(killed_out),
                "--set-json",
                '{"stage_delay_ms":0}',
            ]
            resumed = subprocess.run(
                resume_command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            self.assertEqual(resumed.returncode, 0, msg=resumed.stderr)
            payload = json.loads(resumed.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["recovery"]["kind"], "crash_recovery")
            self.assertTrue({"encode", "init"}.issubset(set(payload["reused_stages"])))
            self.assertTrue((killed_out / "diagnostic_report.json").exists())

            raw, base = load_raw("t2v.json")
            full = Renderer(base_dir=base).render(
                with_output(raw, root / "full", workers=1),
                allow_reuse=False,
            )
            resumed_frames = np.load(killed_out / "frames.npy", allow_pickle=False)
            self.assertTrue(np.array_equal(resumed_frames, full.frames))


if __name__ == "__main__":
    unittest.main()
