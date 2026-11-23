from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path

import pytest

from lbworkbench.db import WorkbenchDB
from lbworkbench.engine import run_check
from lbworkbench.errors import WorkbenchError
from lbworkbench.probes import ProbeHTTPServer
from lbworkbench.probes import execute_probe
from lbworkbench.store import get_status, load_package, verify_state
from lbworkbench.util import canonical_json

from conftest import read_json, write_json


def _full_run(
    workdir: Path,
    package: Path,
    plan: Path,
    server: ProbeHTTPServer,
) -> dict:
    db = WorkbenchDB(workdir)
    load_package(db, package)
    result = run_check(db, plan_path=str(plan), probe_base_url=server.base_url)
    assert result["ok"] is True
    return get_status(db)


def test_check_flow_transitions_and_attributions(
    sample_copy: Path,
    check_plan: Path,
    probe_server: ProbeHTTPServer,
    tmp_path: Path,
) -> None:
    status = _full_run(
        tmp_path / "work",
        sample_copy / "package-2026.11.1.json",
        check_plan,
        probe_server,
    )
    by_id = {item["instance_id"]: item for item in status["instances"]}
    expected = json.loads((sample_copy / "drift_injections.json").read_text())["expected"]

    for instance_id, expectation in expected.items():
        drift = by_id[instance_id]["drift"]
        assert drift is not None
        assert drift["cause"] == expectation["cause"]
        assert drift["round_index"] == expectation["first_round_index"]
        evidence = drift["evidence"]
        assert evidence["round"]["round_index"] == drift["round_index"]
        assert evidence["snapshot_version"] == "2026.11.1"
        assert "fields" in evidence
        assert "v1_original" in evidence
        assert "v2_original" in evidence

    assert by_id["manual-01"]["health_status"] == "pending_manual"
    assert by_id["manual-01"]["drift"] is None
    assert by_id["api-01"]["health_status"] == "suspect"
    assert (by_id["api-01"]["consecutive_failures"], by_id["api-01"]["consecutive_successes"]) == (0, 1)
    assert by_id["api-02"]["health_status"] == "healthy"
    assert by_id["api-02"]["consecutive_successes"] == 3
    assert by_id["cache-01"]["health_status"] == "unhealthy"
    assert by_id["cache-01"]["effective_weight"] == 0
    assert by_id["worker-01"]["tombstone"] is True
    assert by_id["worker-01"]["effective_weight"] == 0
    assert status["clock_waterline_ms"] == 1_764_000_003_000
    assert status["committed_rounds"]["count"] == 3
    assert status["observed_error_codes"] == [
        "E_PROBE_MALFORMED",
        "E_PROBE_REFUSED",
        "E_PROBE_STATUS_MISMATCH",
        "E_PROBE_TIMEOUT",
    ]
    assert len(status["probe_errors"]) == 9
    refused = next(
        item for item in status["probe_errors"] if item["error_code"] == "E_PROBE_REFUSED"
    )
    timeout = next(
        item for item in status["probe_errors"] if item["error_code"] == "E_PROBE_TIMEOUT"
    )
    assert refused["round_index"] == 2 and refused["detail"]
    assert timeout["request_budget_ms"] == 200 and timeout["target"] == "/slow"
    verify = verify_state(WorkbenchDB(tmp_path / "work"))
    assert verify["ok"] is True
    assert verify["differences"] == []


def test_probe_error_evidence_is_diagnostic(
    sample_copy: Path,
    check_plan: Path,
    probe_server: ProbeHTTPServer,
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    _full_run(work, sample_copy / "package-2026.11.1.json", check_plan, probe_server)
    conn = sqlite3.connect(work / "workbench.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        rows = {
            (row["instance_id"], row["round_index"]): json.loads(row["result_json"])
            for row in conn.execute(
                """
                SELECT o.*, r.round_index
                FROM probe_observations o
                JOIN rounds r ON r.round_id = o.round_id
                """
            )
        }
    finally:
        conn.close()
    refused = rows[("worker-01", 2)]
    timeout = rows[("cache-01", 2)]
    malformed = rows[("db-01", 2)]
    mismatch = rows[("api-01", 2)]
    assert refused["error_code"] == "E_PROBE_REFUSED"
    assert timeout["error_code"] == "E_PROBE_TIMEOUT"
    assert timeout["request_budget_ms"] == 200
    assert malformed["error_code"] == "E_PROBE_MALFORMED"
    assert mismatch["error_code"] == "E_PROBE_STATUS_MISMATCH"
    assert mismatch["http_status"] == 503
    for result in (refused, timeout, malformed, mismatch):
        assert result["target"].startswith("/")
        assert result["expected_status"] == 200


def test_status_is_byte_stable_across_runs(
    sample_copy: Path,
    check_plan: Path,
    probe_server: ProbeHTTPServer,
    tmp_path: Path,
) -> None:
    first = _full_run(
        tmp_path / "one",
        sample_copy / "package-2026.11.1.json",
        check_plan,
        probe_server,
    )
    second = _full_run(
        tmp_path / "two",
        sample_copy / "package-2026.11.1.json",
        check_plan,
        probe_server,
    )
    assert canonical_json(first) == canonical_json(second)


def test_repeated_round_is_idempotent(
    sample_copy: Path,
    check_plan: Path,
    probe_server: ProbeHTTPServer,
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    db = WorkbenchDB(work)
    load_package(db, sample_copy / "package-2026.11.1.json")
    first = run_check(
        db,
        plan_path=str(check_plan),
        probe_base_url=probe_server.base_url,
        round_index=1,
    )
    assert first["rounds"][0]["status"] == "committed"
    after_first = canonical_json(get_status(db))
    second = run_check(
        db,
        plan_path=str(check_plan),
        probe_base_url=probe_server.base_url,
        round_index=1,
    )
    assert second["rounds"][0]["idempotent"] is True
    assert canonical_json(get_status(db)) == after_first


def test_actual_closed_port_maps_to_refused() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = execute_probe(
        {
            "instance_id": "closed-port",
            "target": f"http://127.0.0.1:{port}/probe",
            "expected_status": 200,
            "require_json": True,
            "timeout_ms": 200,
        },
        None,
    )
    assert result["error_code"] == "E_PROBE_REFUSED"
    assert result["observed_health"] == "unhealthy"


def test_bad_plan_returns_bad_plan_and_creates_no_round(
    sample_copy: Path,
    check_plan: Path,
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    db = WorkbenchDB(work)
    load_package(db, sample_copy / "package-2026.11.1.json")
    plan = read_json(check_plan)
    plan["interval_ms"] = 0
    invalid_plan = tmp_path / "invalid-plan.json"
    write_json(invalid_plan, plan)
    with pytest.raises(WorkbenchError) as caught:
        run_check(
            db,
            plan_path=str(invalid_plan),
            probe_base_url=None,
        )
    assert caught.value.code == "E_BAD_PLAN"
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rounds").fetchone()["n"] == 0
