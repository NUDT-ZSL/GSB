from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from capbench.errors import WorkbenchError
from capbench.probe import SimulatedProbe
from capbench.report import build_report
from capbench.scenario import parse_scenario
from capbench.storage import Store
from capbench.workload import build_window_requests, summarize_window


ROOT = Path(__file__).resolve().parents[1]


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "capbench", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_half_committed_resume_matches_baseline(
    tmp_path: Path,
) -> None:
    scenario_path = ROOT / "scenarios" / "demo.json"
    baseline_db = tmp_path / "baseline.sqlite"
    baseline_report = tmp_path / "baseline.json"
    baseline = _cli(
        "run",
        "--scenario",
        str(scenario_path),
        "--db",
        str(baseline_db),
        "--report",
        str(baseline_report),
        "--transport",
        "simulated",
    )
    assert baseline.returncode == 0, baseline.stderr

    interrupted_db = tmp_path / "interrupted.sqlite"
    interrupted_report = tmp_path / "resumed.json"
    crashed = _cli(
        "run",
        "--scenario",
        str(scenario_path),
        "--db",
        str(interrupted_db),
        "--transport",
        "simulated",
        "--crash-after-claims",
        "10",
    )
    assert crashed.returncode == 137

    with sqlite3.connect(interrupted_db) as conn:
        running = conn.execute(
            "SELECT COUNT(*) FROM windows WHERE state = 'running'"
        ).fetchone()[0]
        reserved = conn.execute(
            "SELECT COALESCE(SUM(reserved_ms), 0) FROM budget_entries"
        ).fetchone()[0]
    assert running > 0
    assert reserved > 0

    status = _cli("status", "--db", str(interrupted_db), "--format", "json")
    assert status.returncode == 0
    status_payload = json.loads(status.stdout)
    assert status_payload["status"] == "running"
    assert any(unit["state"] == "running" for unit in status_payload["units"])
    assert status_payload["budget_used_ms"] == reserved

    resumed = _cli(
        "resume",
        "--db",
        str(interrupted_db),
        "--report",
        str(interrupted_report),
        "--transport",
        "simulated",
    )
    assert resumed.returncode == 0, resumed.stderr
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["status"] == "completed"
    assert resumed_payload["recovered_orphans"]
    assert baseline_report.read_bytes() == interrupted_report.read_bytes()

    verify = _cli(
        "verify",
        "--db",
        str(interrupted_db),
        "--report",
        str(interrupted_report),
    )
    assert verify.returncode == 0, verify.stdout
    assert json.loads(verify.stdout)["ok"] is True


def test_idempotent_commit_and_conflicting_commit(
    demo_scenario,
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "idempotent.sqlite")
    run_id = demo_scenario.default_run_id()
    store.initialize(demo_scenario, run_id=run_id, seed=demo_scenario.seed)
    token = store.new_owner_token()
    claim = store.reserve_and_claim(
        run_id=run_id,
        tenant="alpha",
        gradient_index=1,
        window_index=0,
        owner_token=token,
        max_parallel_gradients=1,
    )
    assert claim.status == "claimed"
    gradient = demo_scenario.plan_map["alpha"].gradients[0]
    requests = build_window_requests(
        gradient,
        0,
        demo_scenario.clock,
        int(claim.detail["request_seq_start"]),
    )
    outcomes = SimulatedProbe().probe_window(
        service=demo_scenario.tenant_service_map["alpha"],
        tenant="alpha",
        gradient=gradient,
        window_index=0,
        requests=requests,
        expected_status=200,
        timeout_ms=gradient.request_timeout_ms,
        seed=demo_scenario.seed,
        run_id=run_id,
    )
    summary = summarize_window(
        tenant="alpha",
        gradient=gradient,
        window_index=0,
        requests=requests,
        outcomes=outcomes,
        expected_status=200,
    )
    first = store.commit_window(
        run_id=run_id,
        tenant="alpha",
        gradient_index=1,
        window_index=0,
        summary=summary,
        owner_token=token,
        slo=demo_scenario.tenant_map["alpha"].slo,
        allow_early_termination=True,
    )
    second = store.commit_window(
        run_id=run_id,
        tenant="alpha",
        gradient_index=1,
        window_index=0,
        summary=summary,
        owner_token=token,
        slo=demo_scenario.tenant_map["alpha"].slo,
        allow_early_termination=True,
    )
    assert first["status"] == "committed"
    assert second["status"] == "already_committed"

    conflicting = copy.deepcopy(summary)
    conflicting["success_count"] += 1
    with pytest.raises(WorkbenchError) as exc_info:
        store.commit_window(
            run_id=run_id,
            tenant="alpha",
            gradient_index=1,
            window_index=0,
            summary=conflicting,
            owner_token=token,
            slo=demo_scenario.tenant_map["alpha"].slo,
            allow_early_termination=True,
        )
    assert exc_info.value.code == "E_IDEMPOTENCY_CONFLICT"


def test_verify_detects_tampered_report(
    demo_scenario,
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "verify.sqlite")
    run_id = demo_scenario.default_run_id()
    store.initialize(demo_scenario, run_id=run_id, seed=demo_scenario.seed)
    report = build_report(store, run_id)
    report_path = tmp_path / "report.json"
    report["run"]["status"] = "tampered"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    from capbench.report import verify_run

    result = verify_run(store, run_id, report_path=report_path)
    assert result["ok"] is False
    assert any(
        check["name"] == "report_matches_persisted_state" and not check["ok"]
        for check in result["checks"]
    )

