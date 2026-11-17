#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_samples import generate_all
from video_workbench.config import load_config_file
from video_workbench.interactive import InteractiveController
from video_workbench.pipeline import Renderer
from video_workbench.util import atomic_write_json, decode_npz, json_safe


VERIFICATION = ROOT / "verification"
DIAGNOSTICS = VERIFICATION / "diagnostics"
OUT = ROOT / "out" / "verification"


def load_raw(name: str) -> tuple[dict[str, Any], Path]:
    raw, base = load_config_file(ROOT / "samples" / name, ROOT)
    return copy.deepcopy(raw), base


def config_with_output(raw: Mapping[str, Any], output_dir: Path, *, workers: int | None = None) -> dict[str, Any]:
    config = copy.deepcopy(dict(raw))
    config["output_dir"] = str(output_dir)
    if workers is not None:
        config["max_workers"] = workers
    return config


def artifact_catalog(output_dir: Path) -> dict[tuple[str, str], np.ndarray]:
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    result: dict[tuple[str, str], np.ndarray] = {}
    for stage, record in sorted(manifest["artifacts"].items()):
        if stage != "init" and not stage.startswith("denoise_"):
            continue
        _, arrays = decode_npz((output_dir / record["path"]).read_bytes())
        for name, value in sorted(arrays.items()):
            result[(stage, name)] = value
    return result


def compare_catalogs(left: Mapping[tuple[str, str], np.ndarray], right: Mapping[tuple[str, str], np.ndarray]) -> dict[str, Any]:
    keys_equal = sorted(left) == sorted(right)
    mismatches = [list(key) for key in sorted(left) if key not in right or not np.array_equal(left[key], right[key])]
    return {"keys_equal": keys_equal, "array_count": len(left), "mismatches": mismatches, "passed": keys_equal and not mismatches}


def copy_report(result: Any, name: str) -> str | None:
    if result is None or result.report_path is None:
        return None
    destination = DIAGNOSTICS / f"{name}_diagnostic_report.json"
    shutil.copy2(result.report_path, destination)
    return str(destination)


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def run_unit_tests() -> dict[str, Any]:
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    duration = time.perf_counter() - started
    log_path = VERIFICATION / "unittest.log"
    log_path.write_text(completed.stdout, encoding="utf-8")
    return {
        "name": "automated_unit_tests",
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "duration_seconds": round(duration, 6),
        "log": str(log_path),
        "tail": completed.stdout.strip().splitlines()[-2:],
    }


def run_consistency_checks() -> tuple[list[dict[str, Any]], list[str]]:
    checks: list[dict[str, Any]] = []
    reports: list[str] = []
    renderer = Renderer(base_dir=ROOT)
    for mode in ("t2v", "i2v"):
        raw, _ = load_raw(f"{mode}.json")
        parallel = renderer.render(
            config_with_output(raw, OUT / f"{mode}_parallel", workers=4),
            allow_reuse=False,
        )
        serial = renderer.render(
            config_with_output(raw, OUT / f"{mode}_serial", workers=1),
            allow_reuse=False,
        )
        catalog = compare_catalogs(artifact_catalog(OUT / f"{mode}_parallel"), artifact_catalog(OUT / f"{mode}_serial"))
        frame_png_count = len(list((OUT / f"{mode}_parallel" / "frames").glob("frame_*.png")))
        passed = (
            parallel.status == "completed"
            and serial.status == "completed"
            and np.array_equal(parallel.frames, serial.frames)
            and np.array_equal(parallel.frame_valid_mask, serial.frame_valid_mask)
            and parallel.summary == serial.summary
            and catalog["passed"]
            and frame_png_count == int(raw["frames"])
        )
        checks.append(
            {
                "name": f"{mode}_parallel_vs_serial",
                "passed": bool(passed),
                "business_codes": parallel.business_codes,
                "frame_equal": bool(np.array_equal(parallel.frames, serial.frames)),
                "frame_fingerprint": parallel.summary["frame_fingerprint"],
                "diagnostic_summary_equal": parallel.summary == serial.summary,
                "latent_and_init_arrays": catalog,
                "parallel_report": str(parallel.report_path),
                "serial_report": str(serial.report_path),
                "per_frame_png_count": frame_png_count,
            }
        )
        reports.extend([path for path in (copy_report(parallel, f"{mode}_parallel"), copy_report(serial, f"{mode}_serial")) if path])
    return checks, reports


def run_incremental_check() -> tuple[dict[str, Any], list[str]]:
    raw, _ = load_raw("t2v.json")
    renderer = Renderer(base_dir=ROOT)
    reports: list[str] = []
    config = config_with_output(raw, OUT / "incremental")
    patch_values = [
        [[0.25, -0.15, 0.40, 0.05], [0.35, -0.05, 0.30, 0.15]],
        [[0.45, 0.05, 0.20, 0.25], [0.55, 0.15, 0.10, 0.35]],
    ]
    edits = [
        {"prompt": "a blue bird flies over a calm lake at dawn"},
        {"seed": 2026},
        {"guidance": 2.75},
        {"frames": 3},
        {"steps": 3, "noise_table_path": "samples/noise_table_3.npy"},
    ]
    edit_results = []
    t2v_incremental = None
    for update in edits:
        config.update(update)
        t2v_incremental = renderer.render(copy.deepcopy(config))
        edit_results.append(
            {
                "set": update,
                "status": t2v_incremental.status,
                "snapshot_version": t2v_incremental.snapshot_version,
                "reused": t2v_incremental.reused_stages,
                "recomputed": t2v_incremental.recomputed_stages,
            }
        )
    t2v_full = renderer.render(config_with_output(config, OUT / "incremental_t2v_full", workers=1), allow_reuse=False)
    t2v_compare = compare_catalogs(artifact_catalog(OUT / "incremental"), artifact_catalog(OUT / "incremental_t2v_full"))
    t2v_png_count = len(list((OUT / "incremental" / "frames").glob("frame_*.png")))
    t2v_pass = (
        t2v_incremental is not None
        and np.array_equal(t2v_incremental.frames, t2v_full.frames)
        and t2v_compare["passed"]
        and t2v_png_count == 3
    )
    reports.append(copy_report(t2v_incremental, "incremental_t2v_checkpoint"))

    config.update(
        {
            "mode": "i2v",
            "init_image_path": "samples/init_image.png",
            "i2v_replacement_steps": 1,
        }
    )
    i2v_base = renderer.render(copy.deepcopy(config))
    config["patch"] = {"step": 1, "frame": 1, "x": 1, "y": 2, "values": patch_values}
    patched = renderer.render(copy.deepcopy(config))
    patch_expected_reused = {"encode", "init", "denoise_000"}
    patch_expected_invalidated = {"denoise_001", "denoise_002", "decode_frame_000", "decode_frame_001", "decode_frame_002", "preview"}
    patch_pass = patch_expected_reused.issubset(set(patched.reused_stages)) and patch_expected_invalidated.issubset(
        set(patched.invalidated_stages)
    )
    patched_full = renderer.render(config_with_output(config, OUT / "incremental_patched_full", workers=1), allow_reuse=False)
    patched_compare = compare_catalogs(artifact_catalog(OUT / "incremental"), artifact_catalog(OUT / "incremental_patched_full"))
    patched_full_pass = np.array_equal(patched.frames, patched_full.frames) and patched_compare["passed"]
    patch_pass = bool(patch_pass and patched_full_pass)
    reports.append(copy_report(patched, "patch_invalidation"))
    reports.append(copy_report(patched_full, "patched_full_serial"))
    config["patch"] = None
    renderer.render(copy.deepcopy(config))
    config.update({"steps": 4, "noise_table_path": "samples/noise_table_4.npy"})
    final_incremental = renderer.render(copy.deepcopy(config))
    final_full = renderer.render(config_with_output(config, OUT / "incremental_i2v_full", workers=1), allow_reuse=False)
    final_compare = compare_catalogs(artifact_catalog(OUT / "incremental"), artifact_catalog(OUT / "incremental_i2v_full"))
    final_pass = (
        np.array_equal(final_incremental.frames, final_full.frames)
        and np.array_equal(final_incremental.frame_valid_mask, final_full.frame_valid_mask)
        and final_compare["passed"]
    )
    reports.append(copy_report(final_incremental, "incremental_i2v_final"))
    reports.append(copy_report(final_full, "full_i2v_final_serial"))
    return (
        {
            "name": "incremental_equals_full_both_modes",
            "passed": bool(t2v_pass and patch_pass and final_pass),
            "t2v_checkpoint_passed": bool(t2v_pass),
            "i2v_final_passed": bool(final_pass),
            "patch_invalidation_passed": bool(patch_pass),
            "patched_full_serial_passed": bool(patched_full_pass),
            "edits": edit_results,
            "t2v_arrays": t2v_compare,
            "i2v_arrays": final_compare,
            "t2v_per_frame_png_count": t2v_png_count,
            "patch_reused_stages": patched.reused_stages,
            "patch_invalidated_stages": patched.invalidated_stages,
            "patched_arrays": patched_compare,
            "final_frame_fingerprint": final_incremental.summary["frame_fingerprint"],
            "final_reused_stages": final_incremental.reused_stages,
            "final_recomputed_stages": final_incremental.recomputed_stages,
            "i2v_base_snapshot_version": i2v_base.snapshot_version,
        },
        [path for path in reports if path],
    )


def run_injection_checks() -> tuple[dict[str, Any], list[str]]:
    payload = json.loads((ROOT / "samples" / "injection_cases.json").read_text(encoding="utf-8"))
    raw, base = load_raw("t2v.json")
    renderer = Renderer(base_dir=base)
    results = []
    reports: list[str] = []
    stage_failure_result = None
    for index, case in enumerate(payload["cases"]):
        config = copy.deepcopy(raw)
        config.update(case["set"])
        config["output_dir"] = str(OUT / "injections" / f"case_{index:02d}_{case['id']}")
        result = renderer.render(config, allow_reuse=False)
        expected = case["expected_business_code"]
        passed = expected == result.primary_code or expected in result.business_codes
        results.append(
            {
                "id": case["id"],
                "expected": expected,
                "actual": result.business_codes,
                "status": result.status,
                "passed": passed,
                "failure_count": len(result.failures),
                "failure_order": [[item["stage"], item["step"], item["frame"], item["error_type"]] for item in result.failures],
            }
        )
        if case["id"] == "stage-failure-isolated":
            stage_failure_result = result
            reports.append(copy_report(result, "stage_failure_isolated"))
    return (
        {
            "name": "validation_and_failure_isolation",
            "passed": all(item["passed"] for item in results),
            "cases": results,
            "isolated_failure_status": None if stage_failure_result is None else stage_failure_result.status,
            "isolated_valid_frames": None if stage_failure_result is None else int(stage_failure_result.frame_valid_mask.sum()),
        },
        reports,
    )


def run_interactive_check() -> tuple[dict[str, Any], list[str]]:
    raw, base = load_raw("t2v.json")
    raw = config_with_output(raw, OUT / "interactive")
    raw["steps"] = 2
    raw["frames"] = 2
    raw["noise_table_path"] = "samples/noise_table_2.npy"
    raw["stage_delay_ms"] = 100
    controller = InteractiveController(raw, base_dir=base)
    responses = []
    reports: list[str] = []
    try:
        first = controller.submit({"set": {"seed": 7}}, preview_budget_ms=5)
        responses.append(first)
        first_result = controller.wait_job(first["job_id"], timeout=20)
        second = controller.submit({"set": {"seed": 8}}, preview_budget_ms=5)
        responses.append(second)
        second_result = controller.wait_job(second["job_id"], timeout=20)
        response_path = VERIFICATION / "interactive_responses.json"
        atomic_write_json(response_path, responses)
        reports.append(copy_report(second_result, "interactive_background_final"))
        passed = (
            first["business_code"] == "E_PREVIEW_BUDGET_PENDING"
            and first["background"]["continues"]
            and not first["background"]["render_terminated"]
            and not first["background"]["discarded"]
            and second["business_code"] == "E_PREVIEW_BUDGET_PENDING"
            and second["preview_stale"]
            and second["preview"]["snapshot_version"] == first_result.preview["snapshot_version"]
            and first_result.status == "completed"
            and second_result.status == "completed"
        )
        return (
            {
                "name": "preview_budget_and_background_completion",
                "passed": bool(passed),
                "first_response": {
                    "business_code": first["business_code"],
                    "latency_ms": first["response_latency_ms"],
                    "budget_ms": first["preview_budget_ms"],
                    "background": first["background"],
                },
                "second_response": {
                    "business_code": second["business_code"],
                    "latency_ms": second["response_latency_ms"],
                    "budget_ms": second["preview_budget_ms"],
                    "preview_stale": second["preview_stale"],
                    "stale_preview_snapshot_version": second["preview"]["snapshot_version"],
                    "background": second["background"],
                },
                "first_final_status": first_result.status,
                "second_final_status": second_result.status,
                "responses_path": str(response_path),
            },
            reports,
        )
    finally:
        controller.close()


def run_signal_checks() -> tuple[list[dict[str, Any]], list[str]]:
    checks: list[dict[str, Any]] = []
    reports: list[str] = []

    graceful_out = OUT / "signal_graceful"
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

    ready = wait_until(graceful_ready)
    if ready:
        process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=30)
    graceful_report_path = graceful_out / "diagnostic_report.json"
    graceful_report = json.loads(graceful_report_path.read_text(encoding="utf-8")) if graceful_report_path.exists() else None
    graceful_passed = bool(
        ready
        and process.returncode == 0
        and graceful_report is not None
        and graceful_report["status"] == "interrupted"
        and "E_INTERRUPTED" in graceful_report["business_codes"]
    )
    checks.append(
        {
            "name": "graceful_sigterm",
            "passed": graceful_passed,
            "ready": ready,
            "exit_code": process.returncode,
            "stdout": stdout.strip().splitlines()[-1:] if stdout.strip() else [],
            "stderr": stderr.strip().splitlines(),
            "report_status": None if graceful_report is None else graceful_report["status"],
            "business_codes": None if graceful_report is None else graceful_report["business_codes"],
        }
    )
    if graceful_report_path.exists():
        destination = DIAGNOSTICS / "graceful_sigterm_diagnostic_report.json"
        shutil.copy2(graceful_report_path, destination)
        reports.append(str(destination))

    killed_out = OUT / "signal_kill"
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

    ready = wait_until(kill_ready)
    if ready:
        os.kill(process.pid, signal.SIGKILL)
    process.communicate(timeout=10)
    no_report_after_kill = not (killed_out / "diagnostic_report.json").exists()
    killed_manifest = json.loads((killed_out / "manifest.json").read_text(encoding="utf-8")) if (killed_out / "manifest.json").exists() else {}
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
    resumed = subprocess.run(resume_command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    resumed_payload = json.loads(resumed.stdout.strip().splitlines()[-1]) if resumed.stdout.strip() else {}
    recovery = resumed_payload.get("recovery", {})
    recovery_report_path = killed_out / "diagnostic_report.json"
    recovery_report = json.loads(recovery_report_path.read_text(encoding="utf-8")) if recovery_report_path.exists() else None
    kill_passed = bool(
        ready
        and process.returncode == -9
        and no_report_after_kill
        and killed_manifest.get("status") == "running"
        and resumed.returncode == 0
        and resumed_payload.get("status") == "completed"
        and recovery.get("kind") == "crash_recovery"
        and {"encode", "init"}.issubset(set(resumed_payload.get("reused_stages", [])))
        and recovery_report is not None
    )
    checks.append(
        {
            "name": "sigkill_restart_recovery",
            "passed": kill_passed,
            "kill_ready": ready,
            "killed_exit_code": process.returncode,
            "report_absent_after_sigkill": no_report_after_kill,
            "manifest_status_after_sigkill": killed_manifest.get("status"),
            "manifest_progress_after_sigkill": killed_manifest.get("progress"),
            "resume_exit_code": resumed.returncode,
            "resume_status": resumed_payload.get("status"),
            "recovery_point": recovery,
            "reused_after_recovery": resumed_payload.get("reused_stages"),
        }
    )
    if recovery_report_path.exists():
        destination = DIAGNOSTICS / "sigkill_recovery_diagnostic_report.json"
        shutil.copy2(recovery_report_path, destination)
        reports.append(str(destination))
    return checks, reports


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    VERIFICATION.mkdir(parents=True, exist_ok=True)
    if DIAGNOSTICS.exists():
        shutil.rmtree(DIAGNOSTICS)
    DIAGNOSTICS.mkdir(parents=True)
    # Ensure diagnostics directory exists before any copy_report call.
    generate_all()

    checks: list[dict[str, Any]] = [run_unit_tests()]
    reports: list[str] = []
    consistency, consistency_reports = run_consistency_checks()
    checks.extend(consistency)
    reports.extend(consistency_reports)
    incremental, incremental_reports = run_incremental_check()
    checks.append(incremental)
    reports.extend(incremental_reports)
    injections, injection_reports = run_injection_checks()
    checks.append(injections)
    reports.extend(injection_reports)
    interactive, interactive_reports = run_interactive_check()
    checks.append(interactive)
    reports.extend(interactive_reports)
    signals, signal_reports = run_signal_checks()
    checks.extend(signals)
    reports.extend(signal_reports)

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workspace": str(ROOT),
        "environment": {
            "python": sys.version,
            "numpy": np.__version__,
            "network_required_at_runtime": False,
            "container_runtime_used": False,
        },
        "passed": all(check.get("passed", False) for check in checks),
        "checks": checks,
        "diagnostic_reports": reports,
    }
    summary_path = VERIFICATION / "verification_summary.json"
    atomic_write_json(summary_path, json_safe(summary))
    print(json.dumps({"passed": summary["passed"], "summary": str(summary_path), "checks": len(checks)}, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
