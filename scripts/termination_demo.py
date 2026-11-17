#!/usr/bin/env python3
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shardcheck.io import atomic_write_json, read_json, remove_tree
from shardcheck.report import deterministic_report_view


PYTHON = sys.executable


def wait_for_snapshot(run_dir: Path, predicate: Callable[[dict], bool], timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        path = run_dir / "state" / "snapshot.json"
        if path.exists():
            try:
                last = read_json(path)
                if predicate(last):
                    return last
            except Exception:
                pass
        time.sleep(0.02)
    raise RuntimeError(f"snapshot predicate timeout; last={last!r}")


def run_cli(run_dir: Path, *extra: str, fresh: bool = False) -> subprocess.CompletedProcess[str]:
    command = [
        PYTHON,
        "-m",
        "shardcheck",
        "run",
        "--samples",
        "samples",
        "--run-dir",
        str(run_dir),
        *extra,
    ]
    if fresh:
        command.append("--fresh")
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def main() -> int:
    work = ROOT / "artifacts" / "termination-demo"
    remove_tree(work)
    work.mkdir(parents=True, exist_ok=True)

    baseline = work / "baseline"
    result = run_cli(baseline, fresh=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr)
    baseline_report = read_json(baseline / "report.json")

    graceful = work / "graceful"
    process = subprocess.Popen(
        [
            PYTHON,
            "-m",
            "shardcheck",
            "run",
            "--samples",
            "samples",
            "--run-dir",
            str(graceful),
            "--fresh",
            "--write-delay-seconds",
            "0.08",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wait_for_snapshot(
        graceful,
        lambda snap: snap.get("round_cursor", 0) >= 1
        and snap.get("phase") in {"train", "write", "retention"},
    )
    process.send_signal(signal.SIGTERM)
    _, graceful_stderr = process.communicate(timeout=60)
    if process.returncode != 0:
        raise SystemExit(f"graceful run failed: {graceful_stderr}")
    graceful_exit_code = process.returncode
    graceful_report = read_json(graceful / "report.json")
    graceful_cursor = graceful_report["round_cursor"]
    result = run_cli(graceful)
    if result.returncode != 0:
        raise SystemExit(result.stderr)
    graceful_resumed_report = read_json(graceful / "report.json")

    forced = work / "forced"
    process = subprocess.Popen(
        [
            PYTHON,
            "-m",
            "shardcheck",
            "run",
            "--samples",
            "samples",
            "--run-dir",
            str(forced),
            "--fresh",
            "--write-delay-seconds",
            "0.2",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wait_for_snapshot(
        forced,
        lambda snap: snap.get("phase") == "train" and snap.get("round_cursor", 0) >= 1,
    )
    time.sleep(0.03)
    process.kill()
    process.wait(timeout=30)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    report_absent_after_kill = not (forced / "report.json").exists()
    result = run_cli(forced)
    if result.returncode != 0:
        raise SystemExit(result.stderr)
    forced_report = read_json(forced / "report.json")

    summary = {
        "baseline": {
            "exit_path": baseline_report["exit"]["path"],
            "final_params_digest": baseline_report["final_params_digest"],
            "manifest_digest": baseline_report["checkpoint_manifest"]["manifest_digest"],
        },
        "graceful_signal": {
            "exit_code": graceful_exit_code,
            "exit_path": graceful_report["exit"]["path"],
            "signal_received": graceful_report["exit"]["signal_received"],
            "cursor_before_resume": graceful_cursor,
            "resume_recovery_mode": graceful_resumed_report["recovery"]["mode"],
            "deterministic_fields_equal_to_baseline": deterministic_report_view(
                graceful_resumed_report
            )
            == deterministic_report_view(baseline_report),
        },
        "forced_termination": {
            "report_absent_during_forced_run": report_absent_after_kill,
            "recovery_mode": forced_report["recovery"]["mode"],
            "clean_previous_exit": forced_report["recovery"]["clean_previous_exit"],
            "deterministic_fields_equal_to_baseline": deterministic_report_view(forced_report)
            == deterministic_report_view(baseline_report),
            "final_params_digest": forced_report["final_params_digest"],
        },
        "all_checks_passed": (
            graceful_report["exit"]["path"] == "graceful_signal"
            and report_absent_after_kill
            and forced_report["recovery"]["mode"] == "forced_termination_resume"
            and deterministic_report_view(graceful_resumed_report)
            == deterministic_report_view(baseline_report)
            and deterministic_report_view(forced_report)
            == deterministic_report_view(baseline_report)
        ),
    }
    atomic_write_json(work / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
