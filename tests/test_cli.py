from __future__ import annotations

import json
from pathlib import Path

from .conftest import SAMPLES, cli_json, run_cli


def _load(db: Path):
    return run_cli(
        "load",
        "--endpoints",
        str(SAMPLES / "endpoints.json"),
        "--config",
        str(SAMPLES / "pool_config.json"),
        "--trace",
        str(SAMPLES / "trace.json"),
        "--db",
        str(db),
    )


def test_load_is_idempotent_and_status_is_stable(tmp_path: Path) -> None:
    db = tmp_path / "pool.db"
    first = _load(db)
    second = _load(db)
    assert first.returncode == 0
    assert second.returncode == 0
    assert json.loads(first.stdout)["status"] == "LOADED"
    assert json.loads(second.stdout)["status"] == "ALREADY_LOADED"

    status_one = run_cli("status", "--db", str(db))
    status_two = run_cli("status", "--db", str(db))
    assert status_one.returncode == 0
    assert status_one.stdout == status_two.stdout
    payload = json.loads(status_one.stdout)
    assert payload["algorithm"]["mode"] == "exact"
    assert list(payload) == sorted(payload)


def test_verify_reports_determinism_equivalence_and_switch(
    tmp_path: Path,
) -> None:
    first = cli_json(
        "verify",
        "--endpoints",
        str(SAMPLES / "endpoints.json"),
        "--config",
        str(SAMPLES / "pool_config.json"),
        "--trace",
        str(SAMPLES / "trace.json"),
        "--transport",
        "simulated",
    )
    second = cli_json(
        "verify",
        "--endpoints",
        str(SAMPLES / "endpoints.json"),
        "--config",
        str(SAMPLES / "pool_config.json"),
        "--trace",
        str(SAMPLES / "trace.json"),
        "--transport",
        "simulated",
    )
    assert first == second
    assert first["deterministic"] is True
    assert first["exact_tiered_equivalent"] is True
    assert len(first["leases_issued"]) == 10
    assert first["leases_issued"][8]["reused"] is True
    assert first["leases_issued"][9]["reused"] is True
    assert first["mode_switches"]
    assert first["status"]["watermark"]["total_idle"] == 2


def test_acquire_release_status_cli(tmp_path: Path, live_catalog) -> None:
    db = tmp_path / "pool.db"
    from dbpool.engine import initialize_database

    initialize_database(db, live_catalog)
    acquired = cli_json(
        "acquire",
        "--db",
        str(db),
        "--seq",
        "1",
        "--clock-ms",
        "0",
    )
    assert acquired["status"] == "LEASED"
    released = cli_json(
        "release",
        "--db",
        str(db),
        "--seq",
        "1",
        "--clock-ms",
        "20",
        "--close",
    )
    assert released["status"] == "RELEASED"
    assert released["receipt"]["closed"] is True
    status = cli_json("status", "--db", str(db), "--at-clock", "20")
    assert status["watermark"]["total_allocated"] == 0
    assert status["watermark"]["total_idle"] == 0
    assert status["closed_by_reason"] == [
        {"close_reason": "ACTIVE_CLOSE", "count": 1}
    ]


def test_cli_reports_business_error_json(tmp_path: Path) -> None:
    result = run_cli("status", "--db", str(tmp_path / "missing.db"))
    assert result.returncode == 2
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_DATABASE_NOT_FOUND"
