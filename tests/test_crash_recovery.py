from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dynabatch", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


def _save(db: Path) -> None:
    result = _run("save", "--bundle", "sample/bundle.json", "--db", str(db))
    assert result.returncode == 0, result.stderr


def _schedule(db: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return _run("schedule", "--db", str(db), "--now", "300", *extra)


def _resume(db: Path) -> None:
    result = _run("resume", "--db", str(db))
    assert result.returncode == 0, result.stderr


def _train(db: Path) -> None:
    result = _run("train", "--db", str(db), "--now", "300")
    assert result.returncode == 0, result.stderr


def _status(db: Path) -> bytes:
    result = _run("status", "--db", str(db), "--canonical")
    assert result.returncode == 0, result.stderr
    return result.stdout.encode("utf-8")


def test_batch_decision_and_window_commit_crashes_match_baseline(tmp_path) -> None:
    baseline = tmp_path / "baseline.db"
    _save(baseline)
    assert _schedule(baseline).returncode == 0
    _train(baseline)
    baseline_bytes = _status(baseline)

    for phase, expected_code in (("batch_decision", 86), ("window_commit", 87)):
        crashed = tmp_path / f"{phase}.db"
        _save(crashed)
        first = _schedule(crashed, "--crash-at", phase)
        assert first.returncode == expected_code
        _resume(crashed)
        _train(crashed)
        crashed_bytes = _status(crashed)
        assert crashed_bytes == baseline_bytes
        baseline_status = json.loads(baseline_bytes)
        crashed_status = json.loads(crashed_bytes)
        assert crashed_status["state_digest"] == baseline_status["state_digest"]
        verification = _run("verify", "--db", str(crashed), "--baseline", str(baseline))
        assert verification.returncode == 0, verification.stdout + verification.stderr


def test_duplicate_submission_is_idempotent(tmp_path) -> None:
    db = tmp_path / "state.db"
    _save(db)
    assert _schedule(db).returncode == 0
    first = _status(db)
    assert _schedule(db).returncode == 0
    assert _status(db) == first
