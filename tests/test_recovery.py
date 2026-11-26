from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from cold_diag.runner import run_scenario
from cold_diag.sample_data import generate_sample
from cold_diag.utils import canonical_json


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_sigkill_replay_matches_clean_run(tmp_path: Path) -> None:
    data_root = Path(generate_sample(tmp_path / "data")["root"])
    recovered_db = tmp_path / "crash-recovery.sqlite"
    report_path = tmp_path / "crash-report.json"

    demo = subprocess.run(
        [
            sys.executable,
            "-m",
            "cold_diag.cli",
            "crash-demo",
            "--data-dir",
            str(data_root),
            "--db",
            str(recovered_db),
            "--step",
            "round:warm-r1",
            "--output",
            str(report_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert demo.returncode == 0, demo.stderr + demo.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["crash_returncode"] == -9
    recovery = report["recovery"]
    assert [item["step_id"] for item in recovery["half_committed_steps"]] == [
        "round:warm-r1"
    ]
    assert recovery["half_committed_steps"][0]["committed_budget_charges"] == []

    clean = run_scenario(data_root, db_path=tmp_path / "clean.sqlite")
    clean_bytes = canonical_json(clean["state"])
    clean["engine"].close()
    recovered_bytes = canonical_json(report["state"])
    assert recovered_bytes == clean_bytes

    dispatch = report["summary"]["dispatch_report"]
    assert [(row["seq"], row["instance_id"]) for row in dispatch] == [
        (1, "alpha-1"),
        (2, "alpha-1"),
        (3, "beta-1"),
        (4, "gamma-1"),
        (5, "alpha-2"),
        (6, "beta-1"),
        (7, "gamma-1"),
        (8, "delta-3"),
    ]

    with sqlite3.connect(recovered_db) as conn:
        charge_rows = conn.execute(
            """
            SELECT candidate_id, amount_ms, COUNT(*)
              FROM budget_charges
             WHERE round_id='warm-r1'
             GROUP BY candidate_id, amount_ms
             ORDER BY candidate_id
            """
        ).fetchall()
        step_status = conn.execute(
            "SELECT status FROM step_journal WHERE step_id='round:warm-r1'"
        ).fetchone()[0]
    assert step_status == "committed"
    assert charge_rows == [("alpha-1", 100, 1), ("beta-1", 80, 1)]
