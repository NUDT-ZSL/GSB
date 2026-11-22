from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from .conftest import ROOT, SAMPLE_PATH, run_cli


def _paths(tmp_path: Path) -> list[str]:
    return [
        "--sample",
        str(SAMPLE_PATH),
        "--db",
        str(tmp_path / "state.db"),
        "--blocks",
        str(tmp_path / "blocks"),
    ]


def _verify_command(tmp_path: Path) -> list[str]:
    return [
        "verify",
        *_paths(tmp_path),
        "--truth-db",
        str(tmp_path / "verify_truth" / "state.db"),
        "--truth-blocks",
        str(tmp_path / "verify_truth" / "blocks"),
    ]


def test_forced_termination_after_staging_recovers_once(tmp_path: Path) -> None:
    crashed = run_cli(
        "run",
        *_paths(tmp_path),
        "--fresh",
        "--crash-at-batch",
        "b004",
        "--crash-point",
        "after-staging",
    )
    assert crashed.returncode == 137, crashed.stderr
    status = run_cli("status", *_paths(tmp_path))
    status_payload = json.loads(status.stdout)
    assert status_payload["watermark_seq"] == 3
    assert status_payload["current_shape"]["version"] == 1
    assert status_payload["orphaned_staging"]
    assert status_payload["orphaned_staging"][0]["status"] == "ORPHANED_HALF_COMMIT"

    resumed = run_cli("resume", *_paths(tmp_path))
    assert resumed.returncode == 0
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["orphaned_staging"]
    assert next(item["batch_id"] for item in resumed_payload["processed_batches"]) == "b004"
    assert resumed_payload["final_watermark"] == 11

    with sqlite3.connect(tmp_path / "state.db") as conn:
        batch_count = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        observation_count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        staging_count = conn.execute("SELECT COUNT(*) FROM staging_shapes").fetchone()[0]
        shape_count = conn.execute("SELECT COUNT(*) FROM shapes").fetchone()[0]
    assert batch_count == 12
    assert observation_count == 24
    assert staging_count == 0
    assert shape_count == 2
    verified = run_cli(*_verify_command(tmp_path))
    assert verified.returncode == 0
    assert json.loads(verified.stdout)["consistent"] is True


def test_forced_termination_during_join_does_not_advance_state(tmp_path: Path) -> None:
    crashed = run_cli(
        "run",
        *_paths(tmp_path),
        "--fresh",
        "--crash-at-batch",
        "b002",
        "--crash-point",
        "during-join",
    )
    assert crashed.returncode == 137, crashed.stderr
    status = run_cli("status", *_paths(tmp_path))
    status_payload = json.loads(status.stdout)
    assert status_payload["watermark_seq"] == 1
    assert status_payload["counts"]["drift_triggers"] == 0
    resumed = run_cli("resume", *_paths(tmp_path))
    assert resumed.returncode == 0
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["processed_batches"][0]["batch_id"] == "b002"
    assert resumed_payload["final_watermark"] == 11
    with sqlite3.connect(tmp_path / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 24
    assert json.loads(run_cli(*_verify_command(tmp_path)).stdout)["consistent"] is True


def test_concurrent_workers_commit_each_batch_once(tmp_path: Path) -> None:
    init = run_cli("run", *_paths(tmp_path), "--fresh", "--max-batches", "0")
    assert init.returncode == 0
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "stream_reopt", "resume", *_paths(tmp_path)],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(3)
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=90)
        outputs.append((process.returncode, stdout, stderr))
    assert all(returncode == 0 for returncode, _, _ in outputs), outputs
    with sqlite3.connect(tmp_path / "state.db") as conn:
        batch_count = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        distinct_batches = conn.execute("SELECT COUNT(DISTINCT batch_id) FROM batches").fetchone()[0]
        observation_count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        distinct_observations = conn.execute(
            "SELECT COUNT(*) FROM (SELECT batch_id, join_id FROM observations GROUP BY batch_id, join_id)"
        ).fetchone()[0]
        watermark = conn.execute("SELECT watermark_seq FROM state").fetchone()[0]
    assert batch_count == distinct_batches == 12
    assert observation_count == distinct_observations == 24
    assert watermark == 11
    verified = run_cli(*_verify_command(tmp_path))
    assert verified.returncode == 0
    payload = json.loads(verified.stdout)
    assert payload["consistent"] is True

