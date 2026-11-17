#!/usr/bin/env python3
"""End-to-end acceptance: replay, consistency, failure codes, and recovery."""

from __future__ import annotations

import hashlib
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
ARTIFACTS = ROOT / "artifacts"
PYTHON = sys.executable


def run_cli(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        [PYTHON, "-m", "ticket_risk.cli", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def parse_events(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def clear_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def verify_manifest(snapshot_dir: Path) -> dict[str, Any]:
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    entries = [manifest["runtime_file"], manifest["failures_file"], *manifest["stage_files"].values()]
    for entry in entries:
        path = snapshot_dir / entry["file"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == entry["sha256"], f"snapshot hash mismatch: {path.name}"
    return manifest


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    snapshots = ROOT / "snapshots"
    reports = ROOT / "reports"
    clear_dir(snapshots)
    reports.mkdir(parents=True, exist_ok=True)
    for path in reports.iterdir():
        path.unlink()

    replay = run_cli(["--fresh", "--replay", "commands/replay.txt"])
    assert replay.returncode == 0, replay.stderr
    events = parse_events(replay.stdout)
    edits = [event for event in events if event.get("event") == "edit_response"]
    assert edits, "no interactive edit responses"
    assert any("E_INTERACT_BUDGET_PENDING" in event["business_codes"] for event in edits)
    assert all(not event["over_budget"] for event in edits), "an edit response exceeded its budget"

    report = json.loads((reports / "diagnostic_report.json").read_text(encoding="utf-8"))
    verification = report.get("verification") or {}
    assert verification.get("consistent") is True, verification
    assert verification.get("differences") == []
    assert report["state_version"] == report["snapshot_version"]
    assert len(report["samples"]) + len(report["failures"]) == 600
    assert sum(report["summary"]["risk_distribution"].values()) == len(report["samples"])
    manifest = verify_manifest(snapshots)
    assert manifest["global_version"] == report["state_version"]
    assert manifest["completed_sample_ids"] == sorted(
        sample["sample_id"] for sample in report["samples"]
    )

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        invalid_replay = base / "invalid.txt"
        invalid_replay.write_text("quit\n", encoding="utf-8")
        invalid = run_cli(
            [
                "--fresh",
                "--samples",
                "tests/fixtures/invalid_samples.jsonl",
                "--snapshots",
                str(base / "invalid-snapshots"),
                "--temp",
                str(base / "invalid-temp"),
                "--report",
                str(base / "invalid-report.json"),
                "--replay",
                str(invalid_replay),
            ]
        )
        assert invalid.returncode == 0, invalid.stderr
        invalid_events = parse_events(invalid.stdout)
        boot = next(event for event in invalid_events if event.get("event") == "boot")
        failure_codes = {item["business_code"] for item in boot["load_failures"]}
        assert {"E_EMPTY_TEXT", "E_UNKNOWN_CHANNEL", "E_BAD_RECORD"} <= failure_codes
        invalid_restart = run_cli(
            [
                "--samples",
                "tests/fixtures/invalid_samples.jsonl",
                "--snapshots",
                str(base / "invalid-snapshots"),
                "--temp",
                str(base / "invalid-temp"),
                "--report",
                str(base / "invalid-report-restarted.json"),
                "--replay",
                str(invalid_replay),
            ]
        )
        assert invalid_restart.returncode == 0, invalid_restart.stderr
        invalid_restart_boot = next(
            event
            for event in parse_events(invalid_restart.stdout)
            if event.get("event") == "boot"
        )
        assert invalid_restart_boot["recovery_info"]["recovered"] is True
        invalid_report = json.loads(
            (base / "invalid-report-restarted.json").read_text(encoding="utf-8")
        )
        restored_failure_codes = {item["business_code"] for item in invalid_report["failures"]}
        assert {"E_EMPTY_TEXT", "E_UNKNOWN_CHANNEL", "E_BAD_RECORD"} <= restored_failure_codes

        cycle_replay = base / "cycle.txt"
        cycle_replay.write_text("quit\n", encoding="utf-8")
        cycle = run_cli(
            [
                "--fresh",
                "--rules",
                "tests/fixtures/cycle_rules.json",
                "--snapshots",
                str(base / "cycle-snapshots"),
                "--temp",
                str(base / "cycle-temp"),
                "--report",
                str(base / "cycle-report.json"),
                "--replay",
                str(cycle_replay),
            ]
        )
        assert cycle.returncode == 2
        assert "E_RULE_CYCLE" in parse_events(cycle.stdout)[0]["business_codes"]

        changed_config = json.loads((ROOT / "config/determination.json").read_text(encoding="utf-8"))
        changed_config["interaction_budget_ms"] = 201.0
        changed_config_path = base / "changed-config.json"
        changed_config_path.write_text(json.dumps(changed_config, ensure_ascii=False), encoding="utf-8")
        mismatch_replay = base / "mismatch.txt"
        mismatch_replay.write_text("quit\n", encoding="utf-8")
        mismatch = run_cli(
            [
                "--config",
                str(changed_config_path),
                "--replay",
                str(mismatch_replay),
            ]
        )
        assert mismatch.returncode == 2
        assert "E_CONFIG_MISMATCH" in parse_events(mismatch.stdout)[0]["business_codes"]

        kill_snapshots = base / "kill-snapshots"
        kill_reports = base / "kill-reports"
        kill_temp = base / "kill-temp"
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        process = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "ticket_risk.cli",
                "--fresh",
                "--snapshots",
                str(kill_snapshots),
                "--temp",
                str(kill_temp),
                "--report",
                str(kill_reports / "report.json"),
            ],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        assert process.stdin is not None and process.stdout is not None
        boot_line = process.stdout.readline()
        assert boot_line, "killed process did not emit boot"
        process.stdin.write("set_threshold 风险 0.61\n")
        process.stdin.flush()
        time.sleep(0.10)
        process.kill()
        process.communicate(timeout=10)
        killed_manifest = verify_manifest(kill_snapshots)
        kill_restart_replay = base / "kill-restart.txt"
        kill_restart_replay.write_text("status\nverify\nquit\n", encoding="utf-8")
        restart = run_cli(
            [
                "--snapshots",
                str(kill_snapshots),
                "--temp",
                str(kill_temp),
                "--report",
                str(kill_reports / "report.json"),
                "--replay",
                str(kill_restart_replay),
            ]
        )
        assert restart.returncode == 0, restart.stderr
        restart_events = parse_events(restart.stdout)
        restart_boot = next(event for event in restart_events if event.get("event") == "boot")
        recovery = restart_boot["recovery_info"]
        assert recovery["recovered"] is True
        assert recovery["recovery_point"]["global_version"] == killed_manifest["global_version"]
        restart_verify = next(event for event in restart_events if event.get("event") == "verify")
        assert restart_verify["consistent"] is True

        graceful_snapshots = base / "graceful-snapshots"
        graceful_reports = base / "graceful-reports"
        graceful_temp = base / "graceful-temp"
        graceful_process = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "ticket_risk.cli",
                "--fresh",
                "--snapshots",
                str(graceful_snapshots),
                "--temp",
                str(graceful_temp),
                "--report",
                str(graceful_reports / "report.json"),
            ],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        assert graceful_process.stdin is not None and graceful_process.stdout is not None
        assert graceful_process.stdout.readline(), "graceful process did not emit boot"
        graceful_process.stdin.write("set_threshold 风险 0.61\n")
        graceful_process.stdin.flush()
        time.sleep(0.05)
        graceful_process.send_signal(signal.SIGTERM)
        graceful_stdout, graceful_stderr = graceful_process.communicate(timeout=120)
        assert graceful_process.returncode == 0, graceful_stderr
        graceful_events = parse_events(graceful_stdout)
        shutdown_event = next(event for event in graceful_events if event.get("event") == "shutdown")
        assert shutdown_event["exit_code"] == 0
        graceful_manifest = verify_manifest(graceful_snapshots)
        assert graceful_manifest["global_version"] >= 1

    max_latency = max(event["latency_ms"] for event in edits)
    pending_count = sum(event["pending"] for event in edits)
    results = {
        "ok": True,
        "seed": json.loads((ROOT / "assets_meta.json").read_text(encoding="utf-8"))["seed"],
        "sample_count": len(report["samples"]),
        "edit_commands": len(edits),
        "pending_edit_responses": pending_count,
        "max_edit_latency_ms": max_latency,
        "budget_ms": report.get("interaction_budget_ms"),
        "over_budget_count": sum(event["over_budget"] for event in edits),
        "final_state_version": report["state_version"],
        "final_snapshot_version": report["snapshot_version"],
        "risk_distribution": report["summary"]["risk_distribution"],
        "incremental_vs_full_serial": verification["consistent"],
        "failures": report["failures"],
        "checks": {
            "budget_pending_observed": True,
            "failure_codes": True,
            "config_mismatch_refused": True,
            "forced_kill_manifest_valid": True,
            "forced_kill_recovered": True,
            "sigterm_graceful_exit": True,
        },
    }
    (ARTIFACTS / "acceptance_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (ARTIFACTS / "acceptance_replay_stdout.jsonl").write_text(replay.stdout, encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
