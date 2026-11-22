from __future__ import annotations

import json
from pathlib import Path

from conftest import ROOT, cli_json, run_cli


def _status(database: Path) -> dict:
    code, payload, completed = cli_json(["status", "--db", str(database)], cwd=ROOT)
    assert code == 0, completed.stderr
    return payload


def _verify(database: Path) -> dict:
    code, payload, completed = cli_json(
        ["verify", "--sample", "samples/reopt_demo.json", "--db", str(database)],
        cwd=ROOT,
    )
    assert code == 0, completed.stderr
    return payload


def test_sigkill_rolls_back_uncommitted_batch_and_resume_is_identical(tmp_path: Path) -> None:
    crash_database = tmp_path / "crash.db"
    clean_database = tmp_path / "clean.db"
    crashed = run_cli(
        [
            "run",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(crash_database),
            "--batches",
            "9",
            "--crash-before-commit-arrival",
            "5",
        ],
        cwd=ROOT,
    )
    assert crashed.returncode == -9
    before = _status(crash_database)
    assert before["watermark"]["committed_batches"] == 4
    assert before["watermark"]["last_arrival"] == 4
    assert [batch["arrival"] for batch in before["committed_batches"]] == [1, 2, 3, 4]
    before_version = before["shape_version"]

    code, resumed, completed = cli_json(
        [
            "resume",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(crash_database),
            "--batches",
            "9",
        ],
        cwd=ROOT,
    )
    assert code == 0, completed.stderr
    assert resumed["processed"] == [5, 6, 7, 8, 9]
    after = _status(crash_database)
    assert after["watermark"]["committed_batches"] == 9
    assert after["shape_version"] >= before_version
    connection = __import__("sqlite3").connect(crash_database)
    observation_count = len(connection.execute("SELECT seq FROM observations ORDER BY seq").fetchall())
    connection.close()
    assert observation_count == 9
    verify_payload = _verify(crash_database)
    assert verify_payload["ok"] is True

    code, clean_run, clean_completed = cli_json(
        [
            "run",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(clean_database),
            "--batches",
            "9",
        ],
        cwd=ROOT,
    )
    assert code == 0, clean_completed.stderr
    clean = _status(clean_database)
    assert after == clean


def test_parallel_workers_commit_each_batch_once(tmp_path: Path) -> None:
    database = tmp_path / "parallel.db"
    code, payload, completed = cli_json(
        [
            "run",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(database),
            "--batches",
            "9",
            "--workers",
            "4",
        ],
        cwd=ROOT,
    )
    assert code == 0, completed.stderr
    assert payload["processed"] == list(range(1, 10))
    assert sorted(set(payload["processed"])) == list(range(1, 10))
    assert payload["status"]["watermark"]["committed_batches"] == 9
    arrivals = [batch["arrival"] for batch in payload["status"]["committed_batches"]]
    assert arrivals == list(range(1, 10))
    assert _verify(database)["ok"] is True


def test_status_without_sample_is_stable_json(tmp_path: Path) -> None:
    database = tmp_path / "status.db"
    code, _, completed = cli_json(
        [
            "run",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(database),
            "--batches",
            "4",
        ],
        cwd=ROOT,
    )
    assert code == 0, completed.stderr
    first = run_cli(["status", "--db", str(database)], cwd=ROOT)
    second = run_cli(["status", "--db", str(database)], cwd=ROOT)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["watermark"]["committed_batches"] == 4
    assert "shape_version" in payload
    assert "drift_records" in payload
