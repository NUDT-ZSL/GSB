from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
OUTPUT_FILES = (
    "candidates.jsonl",
    "scored.jsonl",
    "retained.jsonl",
    "removed.jsonl",
    "failures.jsonl",
    "diagnostics.jsonl",
)
CHILD_ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")


def run_cli(*arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "workbench", *arguments],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=check,
        timeout=120,
        env=CHILD_ENV,
    )


def spawn(output: Path, *extra: str, fresh: bool = True) -> subprocess.Popen[str]:
    args = [
        sys.executable,
        "-m",
        "workbench",
        "run",
        "--input",
        str(SAMPLES),
        "--output",
        str(output),
        "--checkpoint-delay-ms",
        "30",
    ]
    if fresh:
        args.append("--fresh")
    args.extend(extra)
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        args,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=CHILD_ENV,
        **kwargs,
    )


def wait_for_stage(
    output: Path,
    stage: str = "scoring",
    timeout: float = 60.0,
    process: subprocess.Popen[str] | None = None,
) -> dict[str, Any]:
    snapshot = output / "state" / "latest.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if snapshot.exists():
            try:
                state = json.loads(snapshot.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            else:
                if state.get("snapshot_stage") == stage and state.get("cursor", {}).get(stage, 0) >= 1:
                    return state
        if process is not None and process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"child exited with {process.returncode} before stage {stage}; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
        time.sleep(0.02)
    raise TimeoutError(f"snapshot did not reach stage {stage}")


def terminate_gracefully(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGTERM)


def terminate_forcefully(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        process.kill()


def normalized_report(output: Path) -> dict[str, Any]:
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    report.pop("stage_timings", None)
    return report


def equivalent(left: Path, right: Path) -> tuple[bool, dict[str, Any]]:
    details: dict[str, Any] = {}
    all_equal = True
    for name in OUTPUT_FILES:
        left_bytes = (left / name).read_bytes()
        right_bytes = (right / name).read_bytes()
        equal = left_bytes == right_bytes
        details[name] = equal
        all_equal = all_equal and equal
    left_report = normalized_report(left)
    right_report = normalized_report(right)
    report_equal = left_report == right_report
    details["report_without_timings"] = report_equal
    details["canonical_report_fingerprint"] = {
        "left": left_report.get("canonical_report_fingerprint"),
        "right": right_report.get("canonical_report_fingerprint"),
    }
    all_equal = all_equal and report_equal
    return all_equal, details


def main() -> int:
    results: dict[str, Any] = {
        "os": os.name,
        "platform": sys.platform,
        "python": sys.version,
        "checks": {},
    }
    try:
        import numpy

        results["numpy"] = numpy.__version__
    except Exception as exc:  # pragma: no cover - verification environment guard
        results["numpy"] = f"unavailable: {exc}"

    with tempfile.TemporaryDirectory(prefix="workbench-verify-") as directory:
        root = Path(directory)
        baseline = root / "baseline"
        serial = root / "serial"
        graceful = root / "graceful"
        forced = root / "forced"

        normal = run_cli("run", "--input", str(SAMPLES), "--output", str(baseline), "--fresh")
        serial_run = run_cli(
            "run", "--input", str(SAMPLES), "--output", str(serial), "--fresh", "--workers", "1"
        )
        results["checks"]["baseline_exit_0"] = normal.returncode == 0
        results["checks"]["serial_exit_0"] = serial_run.returncode == 0
        concurrent_equal, details = equivalent(baseline, serial)
        results["checks"]["serial_concurrent_equivalent"] = concurrent_equal
        results["serial_concurrent_details"] = details

        graceful_process = spawn(graceful)
        wait_for_stage(graceful, "scoring", process=graceful_process)
        terminate_gracefully(graceful_process)
        graceful_stdout, graceful_stderr = graceful_process.communicate(timeout=30)
        results["checks"]["graceful_signal_exit_0"] = graceful_process.returncode == 0
        results["graceful_stdout"] = graceful_stdout.strip()
        results["graceful_stderr"] = graceful_stderr.strip()
        graceful_resume = run_cli(
            "run", "--input", str(SAMPLES), "--output", str(graceful), "--workers", "4"
        )
        results["checks"]["graceful_resume_exit_0"] = graceful_resume.returncode == 0
        graceful_equal, details = equivalent(baseline, graceful)
        results["checks"]["graceful_resume_equivalent"] = graceful_equal
        results["graceful_resume_details"] = details

        forced_process = spawn(forced)
        wait_for_stage(forced, "scoring", process=forced_process)
        terminate_forcefully(forced_process)
        forced_process.wait(timeout=30)
        results["forced_returncode"] = forced_process.returncode
        forced_resume = run_cli(
            "run", "--input", str(SAMPLES), "--output", str(forced), "--workers", "4"
        )
        results["checks"]["forced_resume_exit_0"] = forced_resume.returncode == 0
        forced_equal, details = equivalent(baseline, forced)
        results["checks"]["forced_kill_resume_equivalent"] = forced_equal
        results["forced_resume_details"] = details

        mismatch_input = root / "mismatch-input"
        shutil.copytree(SAMPLES, mismatch_input)
        config_path = mismatch_input / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["precision_error_threshold"] = 2e-8
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        mismatch = run_cli(
            "run", "--input", str(mismatch_input), "--output", str(baseline)
        )
        mismatch_payload = {}
        try:
            mismatch_payload = json.loads(mismatch.stderr)
        except json.JSONDecodeError:
            pass
        results["checks"]["config_mismatch_exit_2"] = mismatch.returncode == 2
        results["checks"]["config_mismatch_business_code"] = (
            mismatch_payload.get("business_code") == "E_CONFIG_MISMATCH"
        )

        baseline_report = json.loads((baseline / "report.json").read_text(encoding="utf-8"))
        results["baseline_summary"] = baseline_report["summary"]
        results["baseline_canonical_report_fingerprint"] = baseline_report[
            "canonical_report_fingerprint"
        ]
        results["baseline_stage_timings"] = baseline_report["stage_timings"]
        results["baseline_divergence_ids"] = baseline_report[
            "divergence_diagnostic_ids"
        ]
        results["baseline_injection_ids"] = [
            item["injection_id"] for item in baseline_report["injection_hits"]
        ]
        results["baseline_precision_sensitive_ids"] = [
            item["sample_id"] for item in baseline_report["precision_sensitive"]
        ]

    checks = results["checks"]
    passed = all(bool(value) for value in checks.values())
    results["passed"] = passed
    platform_slug = "windows" if os.name == "nt" else "linux"
    results_path = ROOT / "verification" / f"results-{platform_slug}.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"passed": passed, "checks": checks, "results": str(results_path)}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
