from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

from stream_reopt.engine import RuntimeEngine
from stream_reopt.models import parse_scenario
from stream_reopt.verify import verify_against_serial

from .conftest import run_cli, write_json


def _engine(scenario, tmp_path: Path, name: str) -> RuntimeEngine:
    return RuntimeEngine(
        scenario,
        db_path=tmp_path / name / "state.db",
        blocks_path=tmp_path / name / "blocks",
    )


def test_full_run_switches_drift_and_verifies(sample_dict: dict, tmp_path: Path) -> None:
    scenario = parse_scenario(sample_dict)
    engine = _engine(scenario, tmp_path, "primary")
    try:
        result = engine.run()
        assert result.complete
        assert result.final_watermark == 11
        assert result.final_shape["version"] == 2
        assert result.final_shape["join_order"] == ["customers", "orders", "lineitem"]
        assert len(result.drift_triggers) >= 1
        assert len(result.suppressed_requests) >= 1
        first_trigger = result.drift_triggers[0]
        primary = first_trigger["primary"]
        assert primary["source_table"] in {"orders", "lineitem", "customers"}
        assert primary["source_key"] in {"customer_id", "order_id"}
        assert primary["error_ledger"]["scale"] == 1_000_000
        assert primary["error_ledger"]["combined_error_bound"] >= 0
        assert primary["error_ledger"]["rounding_direction"]
        assert len(primary["batch_interval"]) <= scenario.config.observation_window
        decisions = result.decisions
        assert decisions[0]["status"] == "SWITCHED"
        assert decisions[0]["estimated_saving"] > decisions[0]["switch_cost"]["total"]
        assert decisions[0]["rejected_candidates"]
    finally:
        engine.close()
    verification = verify_against_serial(
        scenario,
        db_path=tmp_path / "primary" / "state.db",
        blocks_path=tmp_path / "primary" / "blocks",
        truth_db_path=tmp_path / "truth" / "state.db",
        truth_blocks_path=tmp_path / "truth" / "blocks",
    )
    assert verification.consistent
    assert verification.compared_batches == 12
    assert verification.drift_evidence["trigger_count"] >= 1
    assert verification.drift_evidence["suppressed_count"] >= 1


def test_switch_budget_retains_shape(sample_dict: dict, tmp_path: Path) -> None:
    data = deepcopy(sample_dict)
    data["config"]["switch_cost_budget"] = 0.0
    scenario = parse_scenario(data)
    engine = _engine(scenario, tmp_path, "budget")
    try:
        result = engine.run()
        assert result.final_shape["version"] == 1
        assert result.decisions
        assert all(decision["status"] == "RETAINED" for decision in result.decisions)
        assert result.decisions[0]["error_code"] == "E_SWITCH_BUDGET"
        assert result.decisions[0]["reason"] == "SWITCH_BUDGET_EXCEEDED"
        assert result.decisions[0]["selected"]["version"] == 1
    finally:
        engine.close()


def test_precision_sensitive_high_precision_recheck(sample_dict: dict, tmp_path: Path) -> None:
    data = deepcopy(sample_dict)
    data["config"]["float_tolerance"] = 100.0
    scenario = parse_scenario(data)
    engine = _engine(scenario, tmp_path, "precision")
    try:
        result = engine.run()
        decision = result.decisions[0]
        assert decision["status"] == "SWITCHED"
        assert decision["precision_sensitive"] is True
        recheck = decision["precision_recheck"]
        assert recheck["method"] == "decimal_precision_50"
        assert recheck["stable"] is True
        assert recheck["float_cost_delta"] < scenario.config.float_tolerance
        assert recheck["evidence"]["scale"] == scenario.config.fixed_scale
        assert recheck["evidence"]["error_source_chain"]
    finally:
        engine.close()


def test_cli_returns_switch_budget_error_by_default(sample_dict: dict, tmp_path: Path) -> None:
    data = deepcopy(sample_dict)
    data["config"]["switch_cost_budget"] = 0.0
    sample = write_json(tmp_path / "budget.json", data)
    result = run_cli(
        "run",
        "--sample",
        str(sample),
        "--db",
        str(tmp_path / "state.db"),
        "--blocks",
        str(tmp_path / "blocks"),
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 3
    assert "E_SWITCH_BUDGET" in payload["decision_error_codes"]
    assert payload["final_shape"]["version"] == 1
    assert payload["final_watermark"] == 11
    assert payload["complete"] is True


def test_verify_detects_tampered_batch_digest(sample_dict: dict, tmp_path: Path) -> None:
    scenario = parse_scenario(sample_dict)
    engine = _engine(scenario, tmp_path, "tampered")
    try:
        engine.run()
    finally:
        engine.close()
    with sqlite3.connect(tmp_path / "tampered" / "state.db") as conn:
        conn.execute("UPDATE batches SET output_digest = 'tampered' WHERE batch_id = 'b003'")
    result = run_cli(
        "verify",
        "--db",
        str(tmp_path / "tampered" / "state.db"),
        "--blocks",
        str(tmp_path / "tampered" / "blocks"),
        "--truth-db",
        str(tmp_path / "truth" / "state.db"),
        "--truth-blocks",
        str(tmp_path / "truth" / "blocks"),
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 4
    assert payload["consistent"] is False
    assert payload["error_code"] == "E_VERIFY_MISMATCH"
    assert any(item["field"] == "output_digest" for item in payload["mismatches"])

