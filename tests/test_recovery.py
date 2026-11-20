from __future__ import annotations

import json
from pathlib import Path

from dbpool.engine import initialize_database

from .conftest import cli_json, run_cli


def _fault_env(point: str) -> dict[str, str]:
    return {
        "DBPOOL_FAULT_POINT": point,
        "DBPOOL_FAULT_EXIT_CODE": "91",
    }


def _loaded_db(tmp_path: Path, catalog) -> Path:
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)
    return db


def _acquire(db: Path, seq: int = 1, clock: int = 0, env=None):
    return run_cli(
        "acquire",
        "--db",
        str(db),
        "--seq",
        str(seq),
        "--clock-ms",
        str(clock),
        env=env,
    )


def test_kill_after_connect_before_commit_leaves_no_lease(
    tmp_path: Path, live_catalog
) -> None:
    db = _loaded_db(tmp_path, live_catalog)
    result = _acquire(db, env=_fault_env("after_connect_before_commit"))
    assert result.returncode == 91

    status = cli_json("status", "--db", str(db))
    assert status["watermark"]["total_allocated"] == 0
    assert status["active_leases"] == []
    assert len(status["pending_intents"]) == 1
    assert status["pending_intents"][0]["state"] == "OPENING"

    resumed = cli_json("resume", "--db", str(db))
    assert resumed["resume"]["aborted_intents"] == 1
    assert resumed["resume"]["repaired_rows"] == 0
    assert resumed["pending_intents"] == []
    assert resumed["watermark"]["total_allocated"] == 0


def test_kill_after_rows_before_commit_rolls_back_entire_ledger(
    tmp_path: Path, live_catalog
) -> None:
    db = _loaded_db(tmp_path, live_catalog)
    result = _acquire(db, env=_fault_env("after_rows_before_commit"))
    assert result.returncode == 91

    status = cli_json("status", "--db", str(db))
    assert status["watermark"]["total_allocated"] == 0
    assert status["active_leases"] == []
    assert status["quota_accounts"][0]["allocated"] == 0
    assert len(status["pending_intents"]) == 1

    resumed = cli_json("resume", "--db", str(db))
    assert resumed["watermark"]["total_allocated"] == 0
    assert resumed["pending_intents"] == []


def test_kill_after_commit_is_idempotently_recovered(
    tmp_path: Path, live_catalog
) -> None:
    db = _loaded_db(tmp_path, live_catalog)
    killed = _acquire(db, env=_fault_env("after_commit_before_ack"))
    assert killed.returncode == 91

    status = cli_json("status", "--db", str(db))
    assert status["watermark"]["total_allocated"] == 1
    assert len(status["active_leases"]) == 1
    lease_id = status["active_leases"][0]["lease_id"]
    assert status["pending_intents"] == []

    retried = cli_json(
        "acquire",
        "--db",
        str(db),
        "--seq",
        "1",
        "--clock-ms",
        "1",
    )
    assert retried["idempotent"] is True
    assert retried["lease"]["lease_id"] == lease_id

    final = cli_json("status", "--db", str(db))
    assert final["watermark"]["total_allocated"] == 1
    assert final["watermark"]["total_active"] == 1
    assert len(final["active_leases"]) == 1


def test_kill_during_release_keeps_lease_active_until_retry(
    tmp_path: Path, live_catalog
) -> None:
    db = _loaded_db(tmp_path, live_catalog)
    acquired = _acquire(db)
    assert acquired.returncode == 0, acquired.stderr
    lease_id = json.loads(acquired.stdout)["lease"]["lease_id"]

    killed = run_cli(
        "release",
        "--db",
        str(db),
        "--seq",
        "1",
        "--clock-ms",
        "1",
        env=_fault_env("release_after_state_before_commit"),
    )
    assert killed.returncode == 91

    status = cli_json("status", "--db", str(db))
    assert status["active_leases"][0]["lease_id"] == lease_id
    assert status["released_leases"] == []
    assert status["watermark"]["total_active"] == 1

    retried = cli_json(
        "release",
        "--db",
        str(db),
        "--seq",
        "1",
        "--clock-ms",
        "2",
    )
    assert retried["idempotent"] is False
    assert retried["lease"]["status"] == "RELEASED"
    assert retried["receipt"]["release_clock_ms"] == 2

    repeated = cli_json(
        "release",
        "--db",
        str(db),
        "--seq",
        "1",
        "--clock-ms",
        "9",
    )
    assert repeated["idempotent"] is True
    assert repeated["receipt"]["release_clock_ms"] == 2
    final = cli_json("status", "--db", str(db))
    assert final["watermark"]["total_active"] == 0
    assert final["watermark"]["total_idle"] == 1


def test_kill_during_resume_repair_does_not_double_count(
    tmp_path: Path, live_catalog
) -> None:
    db = _loaded_db(tmp_path, live_catalog)
    assert _acquire(db).returncode == 0
    killed = run_cli(
        "resume",
        "--db",
        str(db),
        "--clock-ms",
        "1",
        env=_fault_env("resume_after_repair_before_commit"),
    )
    assert killed.returncode == 91
    before = cli_json("status", "--db", str(db))
    assert before["watermark"]["total_allocated"] == 1

    resumed = cli_json("resume", "--db", str(db), "--clock-ms", "2")
    assert resumed["watermark"]["total_allocated"] == 1
    assert resumed["watermark"]["total_active"] == 1
    assert resumed["resume"]["repaired_rows"] == 0
    assert resumed["resume"]["physical_rebuilt"] == 1
