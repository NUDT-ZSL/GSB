from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from flowguard import Adjudicator
from flowguard.verifier import Verifier

from .conftest import fixed_sample


ROOT = Path(__file__).resolve().parents[1]


def _python(*args: str, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "flowguard", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        **kwargs,
    )


def test_hard_exit_after_stage_then_resume_matches_uninterrupted(
    tmp_path: Path,
) -> None:
    baseline = Adjudicator(tmp_path / "baseline.db")
    baseline.load_sample(fixed_sample(), reset=True)
    baseline_report = baseline.run()
    assert baseline_report["ok"] is True

    crash_db = tmp_path / "crash.db"
    loaded = _python("load", "--db", str(crash_db), "--reset")
    assert loaded.returncode == 0, loaded.stderr
    crashed = _python(
        "run",
        "--db",
        str(crash_db),
        "--crash-after-stage",
        "10",
    )
    assert crashed.returncode == 137

    status_before = Adjudicator(crash_db).status()
    assert status_before["counts"]["events"]["processed"] < 28
    assert status_before["counts"]["events"]["pending"] >= 1

    resumed = _python("resume", "--db", str(crash_db))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    resume_payload = json.loads(resumed.stdout)
    assert resume_payload["recovered_staged_events"] >= 1
    assert resume_payload["decision_digest"] == baseline_report["decision_digest"]
    verify = Verifier(crash_db).verify(replay=True)
    assert verify["ok"] is True
    assert verify["decision_digest"] == baseline_report["decision_digest"]


def test_parallel_processes_commit_each_event_once(tmp_path: Path) -> None:
    db = tmp_path / "parallel.db"
    loaded = _python("load", "--db", str(db), "--reset")
    assert loaded.returncode == 0, loaded.stderr

    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "flowguard",
                "run",
                "--db",
                str(db),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONHASHSEED": "0"},
        )
        for _ in range(3)
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=90)
        assert process.returncode == 0, stdout + stderr
        outputs.append(json.loads(stdout))

    adjudicator = Adjudicator(db)
    counts = adjudicator.store.counts()
    assert counts["events"] == {"total": 28, "processed": 28, "pending": 0}
    assert counts["decisions"] == {"allow": 24, "degrade_allow": 2, "reject": 2}
    verify = Verifier(db).verify(replay=True)
    assert verify["ok"] is True
    assert len({item["decision_digest"] for item in outputs}) == 1
