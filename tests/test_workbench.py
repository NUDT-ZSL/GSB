from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from incremental_diffusion.canonical import atomic_write_json
from incremental_diffusion.config import load_config
from incremental_diffusion.errors import (
    E_CONFIG_INVALID,
    E_NOISE_MISMATCH,
    E_PATCH_OUT_OF_RANGE,
    E_SNAPSHOT_VERSION_CONFLICT,
    E_STAGE_FAILURE,
    WorkbenchError,
)
from incremental_diffusion.samples import build_sample_inputs
from incremental_diffusion.workbench import Workbench


STAGES = [
    "prompt_encode",
    "latent_init",
    "img2img_encode",
    "denoise_00",
    "denoise_01",
    "denoise_02",
    "denoise_03",
    "decode",
]


def _generate(root: Path, *, tag: str) -> tuple[Path, Path, Path, Path]:
    input_dir = root / f"{tag}-inputs"
    workspace = root / f"{tag}-workspace"
    outputs = root / f"{tag}-outputs"
    build_sample_inputs(input_dir, workspace=workspace, outputs=outputs)
    return input_dir, workspace, outputs, input_dir / "sample_config.json"


def _copy_config(input_dir: Path, source_name: str, target_name: str, **changes: Any) -> Path:
    with open(input_dir / source_name, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    config.update(changes)
    target = input_dir / target_name
    atomic_write_json(target, config)
    return target


def _cli_render(
    configs: list[Path],
    *,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    if env_extra:
        env.update(env_extra)
    command = [sys.executable, "-m", "incremental_diffusion", "render"]
    for config in configs:
        command.extend(["--config", str(config)])
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )


def _load_image_pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8).copy()


def _load_intermediates(outputs: Path) -> dict[str, np.ndarray]:
    directory = outputs / "intermediates"
    return {
        path.stem: np.load(path, allow_pickle=False)
        for path in sorted(directory.glob("*.npy"))
    }


def _assert_intermediates_equal(test: unittest.TestCase, left: Path, right: Path) -> None:
    left_data = _load_intermediates(left)
    right_data = _load_intermediates(right)
    test.assertEqual(set(left_data), set(right_data))
    for stage_id in sorted(left_data):
        lhs = left_data[stage_id]
        rhs = right_data[stage_id]
        test.assertEqual(lhs.shape, rhs.shape, stage_id)
        test.assertEqual(lhs.dtype, rhs.dtype, stage_id)
        test.assertTrue(np.array_equal(lhs, rhs), f"stage {stage_id} differs")
        if np.issubdtype(lhs.dtype, np.floating):
            test.assertEqual(float(np.max(np.abs(lhs - rhs))), 0.0, stage_id)


class WorkbenchAcceptanceTests(unittest.TestCase):
    def test_incremental_sequence_matches_clean_full_run_and_query_coverage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imgdiff-test-seq-") as temp:
            root = Path(temp)
            input_dir, workspace, outputs, _ = _generate(root, tag="inc")
            configs = [
                input_dir / "sample_config.json",
                input_dir / "edit_prompt.json",
                input_dir / "edit_seed.json",
                input_dir / "edit_guidance.json",
                input_dir / "edit_patch.json",
                input_dir / "edit_image.json",
            ]
            completed = _cli_render(configs)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads((outputs / "diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["snapshot_version"], 6)
            self.assertEqual(len(report["edits"]), 6)

            first, second, third, fourth, fifth, sixth = report["edits"]
            self.assertEqual(first["invalidated_stages"], STAGES)
            self.assertEqual(first["reused_stages"], [])
            self.assertEqual(second["reused_stages"], ["img2img_encode"])
            self.assertEqual(third["reused_stages"], ["prompt_encode", "img2img_encode"])
            self.assertEqual(
                fourth["reused_stages"],
                ["prompt_encode", "latent_init", "img2img_encode"],
            )
            self.assertEqual(
                fifth["reused_stages"],
                [
                    "prompt_encode",
                    "latent_init",
                    "img2img_encode",
                    "denoise_00",
                    "denoise_01",
                ],
            )
            self.assertEqual(
                fifth["invalidated_stages"], ["denoise_02", "denoise_03", "decode"]
            )
            self.assertEqual(
                sixth["reused_stages"], ["prompt_encode", "latent_init"]
            )
            self.assertEqual(
                sixth["invalidated_stages"],
                ["img2img_encode", "denoise_00", "denoise_01", "denoise_02", "denoise_03", "decode"],
            )
            for edit in report["edits"]:
                self.assertEqual(
                    set(edit["reused_stages"]).union(edit["invalidated_stages"]),
                    set(STAGES),
                )
                self.assertEqual(
                    len(edit["reused_stages"]) + len(edit["invalidated_stages"]),
                    len(STAGES),
                )
                self.assertEqual(set(edit["stage_timings_ms"]), set(STAGES))

            self.assertTrue((outputs / "dependency_graph.png").is_file())
            with Image.open(outputs / "dependency_graph.png") as graph:
                self.assertGreater(graph.width, 800)
                self.assertGreater(graph.height, 300)

            query = Workbench(workspace).query(input_dir / "edit_image.json")
            self.assertTrue(query["coverage_ok"])
            self.assertTrue(query["no_duplicates"])
            self.assertEqual(query["recompute_stages"], [])
            self.assertEqual(query["reuse_stages"], STAGES)

            clean_input, clean_workspace, clean_outputs, _ = _generate(root, tag="clean")
            clean_outcome = Workbench(clean_workspace).run(
                clean_input / "edit_image.json", write_diagnostics=False
            )
            self.assertEqual(clean_outcome.status, "ok")
            self.assertTrue(
                np.array_equal(
                    _load_image_pixels(outputs / "final.png"),
                    _load_image_pixels(clean_outputs / "final.png"),
                )
            )
            _assert_intermediates_equal(self, outputs, clean_outputs)

    def test_patch_invalidates_exactly_its_step_and_distinct_patch_does_not_hit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imgdiff-test-patch-") as temp:
            root = Path(temp)
            input_dir, workspace, _outputs, sample = _generate(root, tag="patch")
            workbench = Workbench(workspace)
            self.assertEqual(workbench.run(sample, write_diagnostics=False).status, "ok")

            patch_two = _copy_config(input_dir, "sample_config.json", "same_pixels_2.json", snapshot_version=2)
            patch_three = _copy_config(input_dir, "sample_config.json", "same_pixels_3.json", snapshot_version=3)
            for path, value in ((patch_two, 1_000_000.0), (patch_three, 2_000_000.0)):
                raw = json.loads(path.read_text(encoding="utf-8"))
                raw["patches"] = [
                    {
                        "step": 3,
                        "x": 0,
                        "y": 0,
                        "width": 32,
                        "height": 32,
                        "values": np.full((32, 32, 4), value, dtype=float).tolist(),
                    }
                ]
                atomic_write_json(path, raw)

            outcome_two = workbench.run(patch_two, write_diagnostics=False)
            outcome_three = workbench.run(patch_three, write_diagnostics=False)
            expected_reuse = ["prompt_encode", "latent_init", "img2img_encode", "denoise_00", "denoise_01", "denoise_02"]
            for outcome in (outcome_two, outcome_three):
                self.assertEqual(outcome.entry["reused_stages"], expected_reuse)
                self.assertEqual(outcome.entry["invalidated_stages"], ["denoise_03", "decode"])
            self.assertTrue(np.array_equal(outcome_two.final_pixels, outcome_three.final_pixels))
            self.assertEqual(int(np.unique(outcome_two.final_pixels)[0]), 255)

            store = workbench.store
            snapshot_two = store._load_snapshot_dir(
                store.target_dir(2),
                strict=True,
                expected_version=2,
                expected_config_hash=load_config(patch_two).config_hash,
            )
            snapshot_three = store.load_current()
            self.assertIsNotNone(snapshot_three)
            assert snapshot_three is not None
            fingerprint_two = snapshot_two.artifacts["denoise_03"].content_fingerprint
            fingerprint_three = snapshot_three.artifacts["denoise_03"].content_fingerprint
            self.assertNotEqual(fingerprint_two, fingerprint_three)
            self.assertNotEqual(
                snapshot_two.artifacts["decode"].content_fingerprint,
                snapshot_three.artifacts["decode"].content_fingerprint,
            )

    def test_conflicts_validation_stage_failure_and_existing_snapshot_preservation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imgdiff-test-errors-") as temp:
            root = Path(temp)
            input_dir, workspace, outputs, sample = _generate(root, tag="errors")
            workbench = Workbench(workspace)
            self.assertEqual(workbench.run(sample, write_diagnostics=False).status, "ok")
            original = workbench.store.load_current()
            assert original is not None
            original_fingerprint = original.manifest["global_content_fingerprint"]
            original_target = workbench.store.current_link.resolve()

            conflict = _copy_config(
                input_dir,
                "sample_config.json",
                "conflict_same_version.json",
                prompt="different prompt with sunset",
            )
            conflict_result = _cli_render([conflict])
            self.assertEqual(conflict_result.returncode, 2)
            self.assertIn(E_SNAPSHOT_VERSION_CONFLICT, conflict_result.stderr)
            conflict_report = json.loads((outputs / "diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(conflict_report["business_code"], E_SNAPSHOT_VERSION_CONFLICT)
            current_after_conflict = workbench.store.load_current()
            assert current_after_conflict is not None
            self.assertEqual(
                current_after_conflict.manifest["global_content_fingerprint"],
                original_fingerprint,
            )
            self.assertEqual(workbench.store.current_link.resolve(), original_target)

            missing = _copy_config(input_dir, "sample_config.json", "missing_vocab.json", snapshot_version=2)
            missing_raw = json.loads(missing.read_text(encoding="utf-8"))
            missing_raw.pop("vocab")
            atomic_write_json(missing, missing_raw)
            with self.assertRaises(WorkbenchError) as missing_error:
                load_config(missing)
            self.assertEqual(missing_error.exception.code, E_CONFIG_INVALID)
            missing_result = _cli_render([missing])
            self.assertEqual(missing_result.returncode, 2)
            self.assertIn(E_CONFIG_INVALID, missing_result.stderr)
            missing_report = json.loads((outputs / "diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(missing_report["business_code"], E_CONFIG_INVALID)

            out_of_range = _copy_config(
                input_dir,
                "sample_config.json",
                "patch_out_of_range.json",
                snapshot_version=2,
                patches=[
                    {"step": 1, "x": 30, "y": 0, "width": 5, "height": 2, "values": np.zeros((2, 5, 4)).tolist()}
                ],
            )
            with self.assertRaises(WorkbenchError) as patch_error:
                load_config(out_of_range)
            self.assertEqual(patch_error.exception.code, E_PATCH_OUT_OF_RANGE)

            shortened_noise = np.load(input_dir / "noise_table.npy", allow_pickle=False)[:3]
            np.save(input_dir / "short_noise.npy", shortened_noise, allow_pickle=False)
            noise_mismatch = _copy_config(
                input_dir,
                "sample_config.json",
                "noise_mismatch.json",
                snapshot_version=2,
                noise_table="short_noise.npy",
            )
            with self.assertRaises(WorkbenchError) as noise_error:
                load_config(noise_mismatch)
            self.assertEqual(noise_error.exception.code, E_NOISE_MISMATCH)

            seeded = _copy_config(
                input_dir,
                "edit_seed.json",
                "stage_failure_seed.json",
            )
            failed = _cli_render([seeded], env_extra={"IMGDIFF_FAIL_STAGE": "denoise_02"})
            self.assertEqual(failed.returncode, 3, failed.stderr)
            self.assertIn(E_STAGE_FAILURE, failed.stderr)
            failure_report = json.loads((outputs / "diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(failure_report["business_code"], E_STAGE_FAILURE)
            self.assertEqual(failure_report["errors"][0]["stage"], "denoise_02")
            self.assertEqual(failure_report["errors"][0]["exception_type"], "RuntimeError")
            current_after_failure = workbench.store.load_current()
            assert current_after_failure is not None
            self.assertEqual(current_after_failure.version, 1)
            self.assertEqual(
                current_after_failure.manifest["global_content_fingerprint"],
                original_fingerprint,
            )

            resumed = _cli_render([seeded])
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            resumed_report = json.loads((outputs / "diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(resumed_report["status"], "ok")
            self.assertEqual(resumed_report["snapshot_version"], 3)
            current_after_resume = workbench.store.load_current()
            assert current_after_resume is not None
            self.assertEqual(current_after_resume.version, 3)

    def test_graceful_signal_and_hard_kill_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="imgdiff-test-recovery-") as temp:
            root = Path(temp)
            input_dir, workspace, outputs, sample = _generate(root, tag="recovery")
            base = _cli_render([sample])
            self.assertEqual(base.returncode, 0, base.stderr)

            reference_two_input, reference_two_workspace, reference_two_outputs, _ = _generate(
                root, tag="reference2"
            )
            reference_two = Workbench(reference_two_workspace).run(
                reference_two_input / "edit_prompt.json", write_diagnostics=False
            )
            self.assertEqual(reference_two.status, "ok")

            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["IMGDIFF_DELAY_STAGE"] = "denoise_01"
            env["IMGDIFF_STAGE_DELAY_SECONDS"] = "1.0"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "incremental_diffusion",
                    "render",
                    "--config",
                    str(input_dir / "edit_prompt.json"),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.time() + 10.0
            observed_denoise_zero = False
            while time.time() < deadline:
                self.assertIsNone(process.poll(), "renderer exited before SIGTERM was sent")
                manifests = list((workspace / "staging").glob("*/manifest.json"))
                if manifests:
                    try:
                        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        manifest = {}
                    if "denoise_00" in manifest.get("stages", {}):
                        observed_denoise_zero = True
                        break
                time.sleep(0.01)
            self.assertTrue(observed_denoise_zero)
            # Wait until the next stage has entered its deliberately slow body;
            # otherwise the signal can land in the gap before the stage starts.
            time.sleep(0.2)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            interrupted_report = json.loads((outputs / "diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(interrupted_report["status"], "interrupted")
            current_after_interrupt = Workbench(workspace).store.load_current()
            assert current_after_interrupt is not None
            self.assertEqual(current_after_interrupt.version, 1)

            resumed_two = _cli_render([input_dir / "edit_prompt.json"])
            self.assertEqual(resumed_two.returncode, 0, resumed_two.stderr)
            resumed_two_report = json.loads((outputs / "diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(resumed_two_report["status"], "ok")
            self.assertIn("denoise_01", resumed_two_report["edits"][0]["reused_stages"])
            self.assertTrue(
                np.array_equal(
                    _load_image_pixels(outputs / "final.png"),
                    _load_image_pixels(reference_two_outputs / "final.png"),
                )
            )
            _assert_intermediates_equal(self, outputs, reference_two_outputs)

            reference_three_input, reference_three_workspace, reference_three_outputs, _ = _generate(
                root, tag="reference3"
            )
            reference_three = Workbench(reference_three_workspace).run(
                reference_three_input / "edit_seed.json", write_diagnostics=False
            )
            self.assertEqual(reference_three.status, "ok")

            crashed = _cli_render(
                [input_dir / "edit_seed.json"],
                env_extra={"IMGDIFF_CRASH_AFTER_STAGE": "denoise_02"},
            )
            self.assertEqual(crashed.returncode, 137, crashed.stderr)
            current_after_crash = Workbench(workspace).store.load_current()
            assert current_after_crash is not None
            self.assertEqual(current_after_crash.version, 2)

            resumed_three = _cli_render([input_dir / "edit_seed.json"])
            self.assertEqual(resumed_three.returncode, 0, resumed_three.stderr)
            resumed_three_report = json.loads((outputs / "diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(resumed_three_report["status"], "ok")
            self.assertIn("denoise_02", resumed_three_report["edits"][0]["reused_stages"])
            self.assertTrue(
                np.array_equal(
                    _load_image_pixels(outputs / "final.png"),
                    _load_image_pixels(reference_three_outputs / "final.png"),
                )
            )
            _assert_intermediates_equal(self, outputs, reference_three_outputs)


if __name__ == "__main__":
    unittest.main()
