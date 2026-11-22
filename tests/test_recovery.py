from __future__ import annotations

import json
from pathlib import Path

from conftest import SAMPLES, remove_db, run_cli, run_cli_json


def _prepare(tmp_path: Path, name: str) -> Path:
    db = tmp_path / name
    remove_db(db)
    code, _ = run_cli_json(["load", str(SAMPLES / "sample.json"), "--db", str(db)])
    assert code == 0
    return db


def _status_without_volatile(status: dict) -> dict:
    return status


def test_record_crash_before_commit_rolls_back_and_resume_matches_fresh(
    tmp_path: Path,
) -> None:
    crashed = _prepare(tmp_path, "crashed.sqlite")
    fresh = _prepare(tmp_path, "fresh.sqlite")
    result = run_cli(
        ["run", "--db", str(crashed), "--queue", "eth0:0", "--compact"],
        env={
            "IC_CRASH_POINT": "record_before_commit",
            "IC_CRASH_RECORD": "1000",
        },
    )
    assert result.returncode == 99
    _, before = run_cli_json(["status", "--db", str(crashed)])
    q0 = next(item for item in before["queues"] if item["queue"] == 0 and item["iface"] == "eth0")
    assert q0["record_watermark"] is None
    assert q0["fixed_interrupts"] == 0

    code, resumed = run_cli_json(["resume", "--db", str(crashed)])
    assert code == 0
    assert resumed["run"]["committed"] == 75
    assert resumed["recovered"]["pending_operations"] == 0

    run_cli_json(["run", "--db", str(fresh)])
    _, crashed_status = run_cli_json(["status", "--db", str(crashed)])
    _, fresh_status = run_cli_json(["status", "--db", str(fresh)])
    assert _status_without_volatile(crashed_status) == _status_without_volatile(
        fresh_status
    )
    code, verified = run_cli_json(["verify", "--db", str(crashed)])
    assert code == 0
    assert verified["ok"] is True


def test_record_crash_after_commit_resume_is_idempotent(tmp_path: Path) -> None:
    db = _prepare(tmp_path, "after.sqlite")
    result = run_cli(
        ["run", "--db", str(db), "--queue", "eth0:0", "--compact"],
        env={
            "IC_CRASH_POINT": "record_after_commit",
            "IC_CRASH_RECORD": "1000",
        },
    )
    assert result.returncode == 99
    _, before = run_cli_json(["status", "--db", str(db)])
    q0 = next(item for item in before["queues"] if item["queue"] == 0 and item["iface"] == "eth0")
    assert q0["record_watermark"] == 1000
    assert q0["fixed_interrupts"] == 0
    code, resumed = run_cli_json(["resume", "--db", str(db)])
    assert code == 0
    _, after = run_cli_json(["status", "--db", str(db)])
    q0_after = next(
        item for item in after["queues"] if item["queue"] == 0 and item["iface"] == "eth0"
    )
    assert q0_after["fixed_interrupts"] == 9
    code, verified = run_cli_json(["verify", "--db", str(db)])
    assert code == 0
    assert verified["ok"] is True


def test_parameter_staging_crash_recovers_atomic_replacement(tmp_path: Path) -> None:
    crashed = _prepare(tmp_path, "param_crashed.sqlite")
    fresh = _prepare(tmp_path, "param_fresh.sqlite")
    run_cli_json(["run", "--db", str(crashed)])
    run_cli_json(["run", "--db", str(fresh)])

    result = run_cli(
        [
            "load",
            str(SAMPLES / "params_v2_coalesce.json"),
            "--db",
            str(crashed),
            "--version",
            "2",
            "--compact",
        ],
        env={"IC_CRASH_POINT": "param_after_stage"},
    )
    assert result.returncode == 99
    _, before = run_cli_json(["status", "--db", str(crashed)])
    assert before["active_batch_version"] == 1
    assert before["pending_operations"] == 1

    code, resumed = run_cli_json(["resume", "--db", str(crashed)])
    assert code == 0
    assert resumed["recovered"]["recovered"][0]["action"] == "completed_staged_import"

    run_cli_json(
        [
            "load",
            str(SAMPLES / "params_v2_coalesce.json"),
            "--db",
            str(fresh),
            "--version",
            "2",
        ]
    )
    run_cli_json(["run", "--db", str(fresh)])
    _, crashed_status = run_cli_json(["status", "--db", str(crashed)])
    _, fresh_status = run_cli_json(["status", "--db", str(fresh)])
    assert crashed_status == fresh_status
    code, verified = run_cli_json(["verify", "--db", str(crashed)])
    assert code == 0
    assert verified["ok"] is True


def test_parameter_crash_before_final_commit_leaves_old_pointer(tmp_path: Path) -> None:
    db = _prepare(tmp_path, "param_before.sqlite")
    run_cli_json(["run", "--db", str(db)])
    result = run_cli(
        [
            "load",
            str(SAMPLES / "params_v2_weight.json"),
            "--db",
            str(db),
            "--version",
            "2",
            "--compact",
        ],
        env={"IC_CRASH_POINT": "param_before_commit"},
    )
    assert result.returncode == 99
    _, before = run_cli_json(["status", "--db", str(db)])
    assert before["active_batch_version"] == 1
    assert before["pending_operations"] == 1
    code, resumed = run_cli_json(["resume", "--db", str(db)])
    assert code == 0
    _, after = run_cli_json(["status", "--db", str(db)])
    assert after["active_batch_version"] == 2
    assert after["queues"][0]["weight"] == 5

