from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_two_processes_share_one_budget_without_oversubscription(
    tmp_path: Path,
) -> None:
    scenario = ROOT / "scenarios" / "demo.json"
    db = tmp_path / "shared.sqlite"
    report_a = tmp_path / "a.json"
    report_b = tmp_path / "b.json"
    command = [
        sys.executable,
        "-m",
        "capbench",
        "run",
        "--scenario",
        str(scenario),
        "--db",
        str(db),
        "--transport",
        "simulated",
        "--workers",
        "2",
    ]
    process_a = subprocess.Popen(
        [*command, "--report", str(report_a)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process_b = subprocess.Popen(
        [*command, "--report", str(report_b)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out_a, err_a = process_a.communicate(timeout=90)
    out_b, err_b = process_b.communicate(timeout=90)
    assert process_a.returncode == 0, (out_a, err_a)
    assert process_b.returncode == 0, (out_b, err_b)
    report = json.loads(report_a.read_text(encoding="utf-8"))
    assert report["run"]["budget_used_ms"] <= report["run"]["global_time_budget_ms"]
    assert report["run"]["status"] == "completed"
    assert report_a.read_bytes() == report_b.read_bytes()

    verify = subprocess.run(
        [
            sys.executable,
            "-m",
            "capbench",
            "verify",
            "--db",
            str(db),
            "--report",
            str(report_a),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr

