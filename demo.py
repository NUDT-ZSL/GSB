from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "samples"


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    allow_codes: tuple[int, ...] = (0,),
) -> dict[str, Any]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "batchdiag", *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in allow_codes:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    if not result.stdout.strip() and result.returncode != 0:
        return {
            "ok": False,
            "code": "E_FAULT_EXIT",
            "returncode": result.returncode,
        }
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command did not emit JSON: {result.stdout!r}") from exc


def load(db: Path, requests: str) -> dict[str, Any]:
    return run_cli(
        "load",
        "--model",
        str(SAMPLES / "model.json"),
        "--replicas",
        str(SAMPLES / "replicas.json"),
        "--requests",
        str(SAMPLES / requests),
        "--config",
        str(SAMPLES / "precision.json"),
        "--db",
        str(db),
        "--force",
    )


def spectrum(db: Path, output: Path) -> dict[str, Any]:
    return run_cli("spectrum", "--db", str(db), "--output", str(output))["data"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Repeatable local workbench demonstration")
    parser.add_argument(
        "--workdir",
        default=str(ROOT / "artifacts" / "demo-run"),
        help="directory for generated SQLite databases and reports",
    )
    args = parser.parse_args()
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    base_db = workdir / "base.sqlite"
    long_db = workdir / "long.sqlite"
    near_db = workdir / "near.sqlite"
    crash_db = workdir / "crash.sqlite"
    concurrent_db = workdir / "concurrent.sqlite"
    baseline_db = workdir / "concurrent-baseline.sqlite"

    load_result = load(base_db, "requests.json")
    run_cli("advance", "--db", str(base_db), "--steps", "15")
    base_report = spectrum(base_db, workdir / "base-report.json")
    run_cli(
        "verify",
        "--db",
        str(base_db),
        "--report",
        str(workdir / "base-report.json"),
    )

    load(long_db, "requests_long_backlog.json")
    run_cli("advance", "--db", str(long_db), "--steps", "12")
    long_report = spectrum(long_db, workdir / "long-backlog-report.json")

    load(near_db, "requests_near_tolerance.json")
    run_cli("advance", "--db", str(near_db), "--steps", "8")
    near_report = spectrum(near_db, workdir / "near-tolerance-report.json")

    load(crash_db, "requests_long_backlog.json")
    crashed = run_cli(
        "advance",
        "--db",
        str(crash_db),
        "--steps",
        "12",
        env={
            "BATCHDIAG_FAULT": "torn_spectrum",
            "BATCHDIAG_FAULT_STEP": "3",
        },
        allow_codes=(138,),
    )
    status_before_resume = run_cli("status", "--db", str(crash_db))["data"]
    resume_result = run_cli("resume", "--db", str(crash_db), "--steps", "9")
    crash_report_path = workdir / "recovered-report.json"
    spectrum(crash_db, crash_report_path)
    resume_matches = crash_report_path.read_bytes() == (
        workdir / "long-backlog-report.json"
    ).read_bytes()
    verify_resume = run_cli(
        "verify",
        "--db",
        str(crash_db),
        "--report",
        str(workdir / "long-backlog-report.json"),
    )

    load(concurrent_db, "requests_long_backlog.json")
    load(baseline_db, "requests_long_backlog.json")
    run_cli("advance", "--db", str(baseline_db), "--steps", "20")
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "batchdiag",
                "advance",
                "--db",
                str(concurrent_db),
                "--steps",
                "5",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        if process.returncode != 0:
            raise RuntimeError(f"concurrent process failed: {stdout}\n{stderr}")
    concurrent_status = run_cli("status", "--db", str(concurrent_db))["data"]
    concurrent_report_path = workdir / "concurrent-report.json"
    spectrum(concurrent_db, concurrent_report_path)
    baseline_report_path = workdir / "concurrent-baseline-report.json"
    spectrum(baseline_db, baseline_report_path)
    concurrent_matches = (
        concurrent_report_path.read_bytes() == baseline_report_path.read_bytes()
    )
    concurrent_verify = run_cli("verify", "--db", str(concurrent_db))

    verification = {
        "schema_version": 1,
        "sample_sha256": load_result["data"]["sample_sha256"],
        "base": {
            "batch_count": base_report["summary"]["batch_count"],
            "report_digest": base_report["report_digest"],
        },
        "amplification": {
            "marker_count": long_report["summary"]["amplification_marker_count"],
            "metrics": sorted(
                marker["metric"] for marker in long_report["amplification"]
            ),
            "first_over_budget": [
                {
                    "metric": marker["metric"],
                    "batch_id": marker["first_over_budget_batch_id"],
                    "time_ms": marker["first_over_budget_time_ms"],
                    "queue_watermark": marker["observed_queue_watermark"],
                    "multiple": marker["over_budget_multiple"],
                    "chain": marker["batch_chain"],
                }
                for marker in long_report["amplification"]
            ],
            "report_digest": long_report["report_digest"],
        },
        "precision_sensitivity": {
            "marker_count": near_report["summary"][
                "precision_sensitivity_marker_count"
            ],
            "kinds": sorted(
                {item["kind"] for item in near_report["precision_sensitivity"]}
            ),
            "report_digest": near_report["report_digest"],
        },
        "torn_spectrum_recovery": {
            "crash_exit_code": 138,
            "pending_before_resume": status_before_resume["pending_artifacts"],
            "repair": resume_result["data"]["recovery"],
            "report_byte_identical": resume_matches,
            "verify": verify_resume["data"],
        },
        "concurrency": {
            "processes": 4,
            "steps_each": 5,
            "step_index": concurrent_status["step_index"],
            "batch_count": concurrent_status["batch_count"],
            "report_byte_identical_to_serial": concurrent_matches,
            "verify": concurrent_verify["data"],
        },
        "artifacts": {
            "base_report": display_path(workdir / "base-report.json"),
            "long_backlog_report": display_path(workdir / "long-backlog-report.json"),
            "near_tolerance_report": display_path(
                workdir / "near-tolerance-report.json"
            ),
            "recovered_report": display_path(crash_report_path),
            "concurrent_report": display_path(concurrent_report_path),
            "verification": display_path(workdir / "verification.json"),
        },
    }
    verification_path = workdir / "verification.json"
    verification_path.write_text(
        json.dumps(verification, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(verification, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
