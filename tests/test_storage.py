from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sre_pipeline.engine import replay_all
from sre_pipeline.model import load_bundle
from sre_pipeline.storage import Store, get_status, verify_store

from .helpers import base_config, event, run_cli, write_sample


def _sample(tmp_path: Path, events=None):
    config = base_config()
    events = events or [
        event(1, "svc-a", 0),
        event(2, "svc-a", 100, False),
        event(3, "svc-a", 200, False),
        event(4, "svc-a", 1000),
        event(5, "svc-b", 1100),
        event(6, "svc-b", 1200),
        event(7, "svc-b", 1300),
    ]
    return write_sample(tmp_path / "sample", config, events)


def _cli_load(paths, db):
    result = run_cli(
        "load",
        "--db",
        str(db),
        "--config",
        str(paths["config"]),
        "--events",
        str(paths["events"]),
        "--anomalies",
        str(paths["anomalies"]),
    )
    assert result.returncode == 0, result.stderr
    return result


def _cli_run(db, *extra):
    return run_cli("run", "--db", str(db), *extra)


def test_cli_full_lifecycle_and_idempotency(tmp_path):
    paths = _sample(tmp_path)
    db = tmp_path / "state.sqlite"
    _cli_load(paths, db)
    first = _cli_run(db)
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    assert first_payload["processed"] == 7

    status_result = run_cli("status", "--db", str(db))
    assert status_result.returncode == 0, status_result.stderr
    status = json.loads(status_result.stdout)
    first_state_version = status["state_version"]
    first_hash = status["state_sha256"]
    assert [item["name"] for item in status["services"]] == ["svc-a", "svc-b"]

    second = _cli_run(db)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["processed"] == 0
    second_status = json.loads(run_cli("status", "--db", str(db)).stdout)
    assert second_status["state_version"] == first_state_version
    assert second_status["state_sha256"] == first_hash

    verify_result = run_cli("verify", "--db", str(db))
    assert verify_result.returncode == 0, verify_result.stderr
    verification = json.loads(verify_result.stdout)
    assert verification["ok"] is True
    assert verification["fixed_point"]["failure_rate"]["max_error"] == 0.5

    with Store(db) as store:
        assert len(store.connection.execute("SELECT * FROM event_log").fetchall()) == 7
        assert verify_store(store)["ok"] is True


def test_status_output_is_stable(tmp_path):
    paths = _sample(tmp_path)
    db = tmp_path / "state.sqlite"
    _cli_load(paths, db)
    assert _cli_run(db).returncode == 0
    first = run_cli("status", "--db", str(db)).stdout
    second = run_cli("status", "--db", str(db)).stdout
    assert first == second


@pytest.mark.parametrize("stage", ["rate_limit", "window_commit", "degradation"])
def test_forced_termination_then_resume_matches_uninterrupted(tmp_path, stage):
    paths = _sample(tmp_path, [event(i, "svc-a", i * 100) for i in range(1, 16)])
    baseline_db = tmp_path / "baseline.sqlite"
    _cli_load(paths, baseline_db)
    baseline_run = _cli_run(baseline_db)
    assert baseline_run.returncode == 0, baseline_run.stderr
    baseline_status = json.loads(run_cli("status", "--db", str(baseline_db)).stdout)

    crash_db = tmp_path / f"crash-{stage}.sqlite"
    _cli_load(paths, crash_db)
    crashed = _cli_run(crash_db, "--crash-at", f"{stage}:5")
    assert crashed.returncode == 86

    with Store(crash_db) as store:
        version, state = (
            store.connection.execute(
                "SELECT version FROM state_pointer WHERE singleton=1"
            ).fetchone()[0],
            None,
        )
        assert version == 4
        event_seqs = [
            int(row[0])
            for row in store.connection.execute(
                "SELECT event_seq FROM event_log ORDER BY event_seq"
            ).fetchall()
        ]
        assert event_seqs == [1, 2, 3, 4]

    resumed = run_cli("resume", "--db", str(crash_db))
    assert resumed.returncode == 0, resumed.stderr
    resumed_status = json.loads(run_cli("status", "--db", str(crash_db)).stdout)
    assert resumed_status["state_sha256"] == baseline_status["state_sha256"]
    assert resumed_status["state_version"] == baseline_status["state_version"]
    assert resumed_status["logical_clock_ms"] == baseline_status["logical_clock_ms"]
    assert resumed_status["services"] == baseline_status["services"]
    verify_result = run_cli("verify", "--db", str(crash_db))
    assert verify_result.returncode == 0, verify_result.stderr


def test_two_processes_share_one_transactional_queue(tmp_path):
    events = [event(i, "svc-a" if i % 2 else "svc-b", i * 30) for i in range(1, 31)]
    paths = _sample(tmp_path, events)
    db = tmp_path / "concurrent.sqlite"
    _cli_load(paths, db)
    command = [
        sys.executable,
        "-m",
        "sre_pipeline",
        "run",
        "--db",
        str(db),
    ]
    first = subprocess.Popen(
        command,
        cwd=str(Path(__file__).resolve().parents[1]),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second = subprocess.Popen(
        command,
        cwd=str(Path(__file__).resolve().parents[1]),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_stdout, first_stderr = first.communicate(timeout=60)
    second_stdout, second_stderr = second.communicate(timeout=60)
    assert first.returncode == 0, first_stderr
    assert second.returncode == 0, second_stderr
    total_processed = json.loads(first_stdout)["processed"] + json.loads(
        second_stdout
    )["processed"]
    assert total_processed == 30
    verify_result = run_cli("verify", "--db", str(db))
    assert verify_result.returncode == 0, verify_result.stderr
    assert json.loads(verify_result.stdout)["ok"] is True


def test_runtime_anomaly_is_audited_without_state_mutation(tmp_path):
    config = base_config()
    events = [
        event(1, "svc-a", 0),
        event(2, "svc-a", 100),
        event(3, "svc-a", 200),
    ]
    anomalies = [
        {
            "anomaly_id": "audit-1",
            "event_seq": 2,
            "service": "svc-a",
            "stage": "rate_limit",
            "kind": "clock_rollback_over_tolerance",
            "details": {"rollback_ms": 100},
        }
    ]
    paths = write_sample(tmp_path / "sample", config, events, anomalies)
    db = tmp_path / "state.sqlite"
    _cli_load(paths, db)
    result = _cli_run(db)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["errors"] == 1
    with Store(db) as store:
        version_before = store.connection.execute(
            "SELECT version FROM state_pointer WHERE singleton=1"
        ).fetchone()[0]
        row = store.connection.execute(
            "SELECT status, error_code FROM event_log WHERE event_seq=2"
        ).fetchone()
        assert tuple(row) == ("error", "E_CLOCK_ROLLBACK")
        verify_result = verify_store(store)
        assert verify_result["ok"] is True
        assert version_before == 2


def test_replay_command_matches_persisted_state(tmp_path):
    paths = _sample(tmp_path)
    db = tmp_path / "state.sqlite"
    _cli_load(paths, db)
    assert _cli_run(db).returncode == 0
    replay_result = run_cli(
        "replay",
        "--config",
        str(paths["config"]),
        "--events",
        str(paths["events"]),
        "--anomalies",
        str(paths["anomalies"]),
    )
    assert replay_result.returncode == 0, replay_result.stderr
    replay_payload = json.loads(replay_result.stdout)
    status = json.loads(run_cli("status", "--db", str(db)).stdout)
    assert replay_payload["state_sha256"] == status["state_sha256"]

