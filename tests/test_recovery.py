from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_external_kill_recovery_matches_uninterrupted_ledger(
    tmp_path: Path,
) -> None:
    output = tmp_path / "recovery"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "kill_recovery_demo.py"),
            "--scenario",
            "all",
            "--output",
            str(output),
            "--timeout-seconds",
            "20",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert payload["ok"] is True
    assert len(payload["scenarios"]) == 2
    for scenario in payload["scenarios"]:
        assert scenario["process_killed"] is True
        assert scenario["fingerprints_equal"] is True
        assert scenario["verify_ok"] is True


@pytest.mark.parametrize(
    "fault_point",
    [
        "deliver_before_commit",
        "deliver_after_commit",
    ],
)
def test_kill_points_preserve_round_ledger(
    tmp_path: Path,
    fault_point: str,
) -> None:
    output = tmp_path / fault_point
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "kill_recovery_demo.py"),
            "--scenario",
            "deliver",
            "--fault-point",
            fault_point,
            "--output",
            str(output),
            "--timeout-seconds",
            "20",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    scenario = payload["scenarios"][0]
    assert scenario["process_killed"] is True
    assert scenario["fingerprints_equal"] is True
    assert scenario["verify_ok"] is True
    assert scenario["baseline_summary"] == scenario["resume_summary"]
