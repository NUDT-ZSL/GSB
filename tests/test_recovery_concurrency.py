from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mvengine.storage import (
    initialize,
    query,
    refresh_logs,
    resume,
    status,
    verify,
)


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "mvengine", *args]


def test_forced_kill_then_resume_is_exactly_once(
    base_package,
    sample_suite: dict[str, Path],
    tmp_path: Path,
) -> None:
    db = tmp_path / "interrupted.sqlite"
    initialize(base_package, db)
    command = _cli(
        "refresh",
        "--db",
        str(db),
        "--package",
        str(sample_suite["base"]),
        "--kill-after",
        "4",
    )
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 137
    interrupted = status(db)
    assert interrupted["watermark_seq"] == 4
    assert interrupted["incomplete_refresh_runs"] == 1

    resumed = resume(db_path=db, package=base_package, logs=base_package.logs)
    assert resumed["applied"] == list(range(5, 13))
    assert resumed["recovered_runs"] == 1
    assert status(db)["watermark_seq"] == 12
    assert status(db)["incomplete_refresh_runs"] == 0
    assert verify(db_path=db, logs=base_package.logs)["ok"]

    baseline_db = tmp_path / "baseline.sqlite"
    initialize(base_package, baseline_db)
    refresh_logs(db_path=baseline_db, logs=base_package.logs)
    assert query(db_path=db) == query(db_path=baseline_db)


def test_two_refresh_processes_apply_each_seq_once(
    base_package,
    sample_suite: dict[str, Path],
    tmp_path: Path,
) -> None:
    db = tmp_path / "concurrent.sqlite"
    initialize(base_package, db)
    command = _cli(
        "refresh",
        "--db",
        str(db),
        "--package",
        str(sample_suite["base"]),
    )
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first_out, first_err = first.communicate(timeout=30)
    second_out, second_err = second.communicate(timeout=30)
    assert first.returncode == 0, first_err
    assert second.returncode == 0, second_err
    payloads = [json.loads(first_out), json.loads(second_out)]
    assert sorted(len(item["applied"]) for item in payloads) == [0, 12]
    final = status(db)
    assert final["watermark_seq"] == 12
    baseline_db = tmp_path / "baseline.sqlite"
    initialize(base_package, baseline_db)
    refresh_logs(db_path=baseline_db, logs=base_package.logs)
    assert query(db_path=db) == query(db_path=baseline_db)
    assert verify(db_path=db, logs=base_package.logs)["ok"]

