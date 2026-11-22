from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from dynabatch.status import build_status
from dynabatch.store import Store


ROOT = Path(__file__).resolve().parents[1]


def test_parallel_schedulers_commit_once(tmp_path) -> None:
    baseline_db = tmp_path / "baseline.db"
    baseline_save = subprocess.run(
        [
            sys.executable,
            "-m",
            "dynabatch",
            "save",
            "--bundle",
            "sample/bundle.json",
            "--db",
            str(baseline_db),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert baseline_save.returncode == 0, baseline_save.stderr
    baseline_schedule = subprocess.run(
        [
            sys.executable,
            "-m",
            "dynabatch",
            "schedule",
            "--db",
            str(baseline_db),
            "--now",
            "300",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert baseline_schedule.returncode == 0, baseline_schedule.stderr
    with Store(baseline_db, create=False) as baseline_store:
        baseline_status = build_status(baseline_store)

    db = tmp_path / "state.db"
    save = subprocess.run(
        [
            sys.executable,
            "-m",
            "dynabatch",
            "save",
            "--bundle",
            "sample/bundle.json",
            "--db",
            str(db),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert save.returncode == 0, save.stderr
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "dynabatch",
                "schedule",
                "--db",
                str(db),
                "--now",
                "300",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    results = [process.communicate(timeout=60) for process in processes]
    assert all(process.returncode == 0 for process in processes), results
    with Store(db, create=False) as store:
        status = build_status(store)
        assert status["stats"]["request_status_counts"] == {"BATCHED": 18}
        assert status["stats"]["window_status_counts"] == baseline_status["stats"][
            "window_status_counts"
        ]
        assert status["stats"]["batches_total"] == baseline_status["stats"]["batches_total"]
        assert status["state_digest"] == baseline_status["state_digest"]
        duplicate = store.fetchall(
            """
            SELECT request_seq, COUNT(DISTINCT batch_id) AS count
            FROM batch_members
            GROUP BY request_seq HAVING count != 1
            """
        )
        assert duplicate == []
