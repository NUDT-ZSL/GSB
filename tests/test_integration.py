from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.conftest import (
    popen_cli,
    run_cli,
    sample_payload,
    write_scenario,
)


def _load(tmp_path: Path, port: int, rounds: int = 3) -> tuple[Path, Path]:
    payload = sample_payload(port=port, rounds=rounds)
    scenario_path = write_scenario(tmp_path / "scenario.json", payload)
    db_path = tmp_path / "diag.db"
    result = run_cli(["--db", str(db_path), "load", str(scenario_path)])
    assert json.loads(result.stdout)["config_version"] == 1
    return db_path, scenario_path


def _sql(db_path: Path, query: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(query, params).fetchall()


def _outcomes(db_path: Path) -> dict[str, int]:
    return {
        row[0]: row[1]
        for row in _sql(
            db_path,
            "SELECT outcome, COUNT(*) FROM probe_samples GROUP BY outcome",
        )
    }


def test_loopback_end_to_end_probe_stats_diagnosis_and_verify(
    tmp_path: Path, mock_service
) -> None:
    db_path, _ = _load(tmp_path, mock_service.port, rounds=4)
    probe_result = json.loads(
        run_cli(["--db", str(db_path), "probe", "--all"]).stdout
    )
    assert len(probe_result["processed_rounds"]) == 4
    assert _sql(
        db_path,
        "SELECT COUNT(*) FROM probe_samples",
    )[0][0] == 40
    assert _sql(
        db_path,
        "SELECT COUNT(DISTINCT probe_seq) FROM probe_samples",
    )[0][0] == 40
    assert _sql(
        db_path,
        "SELECT COUNT(*) FROM round_commits",
    )[0][0] == 40
    assert _sql(
        db_path,
        "SELECT COUNT(*) FROM round_attempts WHERE status != 'committed'",
    )[0][0] == 0

    first_round = probe_result["processed_rounds"][0]
    abandon_kinds = {
        item["abandon_kind"] for item in first_round["abandoned"]
    }
    assert {
        "active_budget_cutoff",
        "not_started_budget",
    } <= abandon_kinds
    outcomes = _outcomes(db_path)
    assert outcomes["timeout"] > 0
    assert outcomes["connection_refused"] > 0
    assert outcomes["status_mismatch"] > 0
    assert outcomes["malformed_response"] > 0
    assert outcomes["abandoned"] > 0

    diagnostics = json.loads(
        run_cli(["--db", str(db_path), "diagnose"]).stdout
    )
    assert all(item["matches_persisted"] for item in diagnostics["diagnoses"])
    assert diagnostics == json.loads(
        run_cli(["--db", str(db_path), "diagnose"]).stdout
    )

    status_a = run_cli(["--db", str(db_path), "status"]).stdout
    status_b = run_cli(["--db", str(db_path), "status"]).stdout
    assert status_a == status_b
    status = json.loads(status_a)
    assert status["pending_attempts"] == 0
    assert [item["target"] for item in status["targets"]] == sorted(
        item["target"] for item in status["targets"]
    )
    by_name = {item["target"]: item for item in status["targets"]}
    assert by_name["healthy"]["state"] == "healthy"
    assert by_name["slow"]["state"] == "unreachable"
    assert by_name["budgeted"]["loss_rate"] is None
    assert by_name["budgeted"]["abandoned_count"] > 0

    verify = json.loads(
        run_cli(["--db", str(db_path), "verify"]).stdout
    )
    assert verify["ok"] is True
    assert verify["mismatches"] == []


def test_multiprocess_disjoint_targets_and_same_target_idempotency(
    tmp_path: Path, mock_service
) -> None:
    db_path, _ = _load(tmp_path, mock_service.port, rounds=2)
    commands = [
        ["--db", str(db_path), "probe", "--round", "1", "--target", "healthy"],
        ["--db", str(db_path), "probe", "--round", "1", "--target", "healthy"],
        ["--db", str(db_path), "probe", "--round", "1", "--target", "lossy"],
    ]
    processes = []
    for command in commands:
        processes.append(popen_cli(command))
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, (stdout, stderr)

    assert _sql(
        db_path,
        """
        SELECT COUNT(*) FROM round_commits
        WHERE config_version = 1 AND round_no = 1
          AND target_name IN ('healthy', 'lossy')
        """,
    )[0][0] == 2
    assert _sql(
        db_path,
        """
        SELECT COUNT(*) FROM probe_samples
        WHERE config_version = 1 AND round_no = 1
          AND target_name IN ('healthy', 'lossy')
        """,
    )[0][0] == 2
    assert _sql(
        db_path,
        """
        SELECT COUNT(*) FROM probe_samples
        WHERE config_version = 1 AND target_name = 'healthy'
          AND probe_seq = 1
        """,
    )[0][0] == 1
    assert _sql(
        db_path,
        "SELECT COUNT(*) FROM round_attempts WHERE status != 'committed'",
    )[0][0] == 0

    for target in ("healthy", "lossy"):
        run_cli(
            [
                "--db",
                str(db_path),
                "probe",
                "--round",
                "1",
                "--target",
                target,
            ]
        )
    assert _sql(db_path, "SELECT COUNT(*) FROM probe_samples")[0][0] == 2
    assert json.loads(
        run_cli(["--db", str(db_path), "verify"]).stdout
    )["ok"] is True


def test_config_replacement_preserves_unaffected_state_and_rejects_rollback(
    tmp_path: Path, mock_service
) -> None:
    payload_v1 = sample_payload(port=mock_service.port, rounds=2)
    scenario_v1 = write_scenario(tmp_path / "v1.json", payload_v1)
    payload_v2 = sample_payload(
        port=mock_service.port,
        rounds=2,
        config_updates={"round_budget_ms": 250},
    )
    scenario_v2 = write_scenario(tmp_path / "v2.json", payload_v2)
    db_path = tmp_path / "diag.db"
    run_cli(["--db", str(db_path), "load", str(scenario_v1)])
    run_cli(
        [
            "--db",
            str(db_path),
            "probe",
            "--round",
            "1",
            "--target",
            "healthy",
        ]
    )
    status_before = json.loads(
        run_cli(["--db", str(db_path), "status"]).stdout
    )
    samples_before = _sql(db_path, "SELECT COUNT(*) FROM probe_samples")[0][0]

    load_v2 = json.loads(
        run_cli(["--db", str(db_path), "load", str(scenario_v2)]).stdout
    )
    assert load_v2["config_version"] == 2
    status_after = json.loads(
        run_cli(["--db", str(db_path), "status"]).stdout
    )
    assert status_after["current_config_version"] == 2
    assert status_after["targets"] == status_before["targets"]
    assert _sql(db_path, "SELECT COUNT(*) FROM probe_samples")[0][0] == samples_before

    rollback = run_cli(
        ["--db", str(db_path), "load", str(scenario_v1)],
        check=False,
    )
    assert rollback.returncode == 2
    assert json.loads(rollback.stdout)["error"]["code"] == "CONFIG_ROLLBACK_REJECTED"
    assert json.loads(
        run_cli(["--db", str(db_path), "status"]).stdout
    )["current_config_version"] == 2


def test_prepared_attempt_recovers_and_stays_bound_to_old_config(
    tmp_path: Path, mock_service
) -> None:
    payload_v1 = sample_payload(port=mock_service.port, rounds=2)
    scenario_v1 = write_scenario(tmp_path / "v1.json", payload_v1)
    payload_v2 = sample_payload(
        port=mock_service.port,
        rounds=2,
        config_updates={"loss_rate_threshold": 0.5},
    )
    scenario_v2 = write_scenario(tmp_path / "v2.json", payload_v2)
    db_path = tmp_path / "diag.db"
    run_cli(["--db", str(db_path), "load", str(scenario_v1)])
    crashed = run_cli(
        [
            "--db",
            str(db_path),
            "probe",
            "--round",
            "1",
            "--target",
            "healthy",
        ],
        crash_at="prepared",
        check=False,
    )
    assert crashed.returncode == 75
    assert _sql(
        db_path, "SELECT COUNT(*) FROM probe_samples"
    )[0][0] == 0
    assert _sql(
        db_path,
        "SELECT COUNT(*) FROM round_attempts WHERE status IN ('prepared', 'recovering')",
    )[0][0] == 1

    run_cli(["--db", str(db_path), "load", str(scenario_v2)])
    resume = json.loads(
        run_cli(["--db", str(db_path), "resume"]).stdout
    )
    assert resume["recovered_attempts"]
    assert _sql(
        db_path,
        """
        SELECT COUNT(*) FROM round_commits
        WHERE config_version = 1 AND round_no = 1 AND target_name = 'healthy'
        """,
    )[0][0] == 1
    assert _sql(
        db_path,
        "SELECT COUNT(*) FROM round_attempts WHERE status != 'committed'",
    )[0][0] == 0
    assert json.loads(
        run_cli(["--db", str(db_path), "verify"]).stdout
    )["ok"] is True


def test_commit_mid_crash_rolls_back_and_resume_repairs(
    tmp_path: Path, mock_service
) -> None:
    db_path, scenario_path = _load(tmp_path, mock_service.port, rounds=2)
    baseline_db = tmp_path / "baseline.db"
    run_cli(["--db", str(baseline_db), "load", str(scenario_path)])
    run_cli(["--db", str(baseline_db), "probe", "--all"])
    crashed = run_cli(
        ["--db", str(db_path), "probe", "--round", "1"],
        crash_at="commit_mid",
        check=False,
    )
    assert crashed.returncode == 75
    assert _sql(db_path, "SELECT COUNT(*) FROM probe_samples")[0][0] == 0
    assert _sql(db_path, "SELECT COUNT(*) FROM round_commits")[0][0] == 0
    assert _sql(
        db_path,
        "SELECT COUNT(*) FROM round_attempts WHERE status IN ('prepared', 'recovering')",
    )[0][0] == len(
        [target for target in sample_payload(port=mock_service.port, rounds=2)["targets"]]
    )

    resume = json.loads(
        run_cli(["--db", str(db_path), "resume"]).stdout
    )
    assert resume["recovered_attempts"]
    assert _sql(db_path, "SELECT COUNT(*) FROM probe_samples")[0][0] == 20
    assert _sql(
        db_path,
        "SELECT COUNT(DISTINCT target_name || ':' || probe_seq) FROM probe_samples",
    )[0][0] == 20
    assert _sql(
        db_path,
        "SELECT COUNT(*) FROM round_attempts WHERE status != 'committed'",
    )[0][0] == 0
    assert json.loads(
        run_cli(["--db", str(db_path), "verify"]).stdout
    )["ok"] is True
    assert json.loads(
        run_cli(["--db", str(db_path), "status"]).stdout
    ) == json.loads(
        run_cli(["--db", str(baseline_db), "status"]).stdout
    )
    commit_query = """
        SELECT target_name, round_no, commit_id
        FROM round_commits
        ORDER BY target_name, round_no
    """
    assert _sql(db_path, commit_query) == _sql(baseline_db, commit_query)


def test_after_one_commit_crash_resume_does_not_duplicate(
    tmp_path: Path, mock_service
) -> None:
    db_path, scenario_path = _load(tmp_path, mock_service.port, rounds=2)
    baseline_db = tmp_path / "baseline.db"
    run_cli(["--db", str(baseline_db), "load", str(scenario_path)])
    run_cli(["--db", str(baseline_db), "probe", "--all"])
    crashed = run_cli(
        ["--db", str(db_path), "probe", "--round", "1"],
        crash_at="after_commit_0",
        check=False,
    )
    assert crashed.returncode == 75
    commits = _sql(db_path, "SELECT COUNT(*) FROM round_commits")[0][0]
    assert commits == 1
    pending = _sql(
        db_path,
        "SELECT COUNT(*) FROM round_attempts WHERE status IN ('prepared', 'recovering')",
    )[0][0]
    assert pending == 9
    samples = _sql(db_path, "SELECT COUNT(*) FROM probe_samples")[0][0]
    assert samples == 1

    run_cli(["--db", str(db_path), "resume"])
    assert _sql(db_path, "SELECT COUNT(*) FROM round_commits")[0][0] == 20
    assert _sql(db_path, "SELECT COUNT(*) FROM probe_samples")[0][0] == 20
    assert _sql(
        db_path,
        "SELECT COUNT(DISTINCT target_name || ':' || probe_seq) FROM probe_samples",
    )[0][0] == 20
    assert json.loads(
        run_cli(["--db", str(db_path), "verify"]).stdout
    )["ok"] is True
    resumed_status = json.loads(
        run_cli(["--db", str(db_path), "status"]).stdout
    )
    baseline_status = json.loads(
        run_cli(["--db", str(baseline_db), "status"]).stdout
    )
    assert resumed_status == baseline_status
    commit_query = """
        SELECT target_name, round_no, commit_id
        FROM round_commits
        ORDER BY target_name, round_no
    """
    assert _sql(db_path, commit_query) == _sql(baseline_db, commit_query)
