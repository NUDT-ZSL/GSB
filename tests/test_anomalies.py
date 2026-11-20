from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import json_stdout


def test_each_injection_maps_to_distinct_code(anomaly_run: dict) -> None:
    executed = anomaly_run["execute"]
    assert executed.returncode == 3
    payload = json_stdout(executed)
    failures = payload["failures"]
    codes = {failure["error_code"] for failure in failures}
    assert {
        "E_MALFORMED_COLUMN",
        "E_DIGEST_MISMATCH",
        "E_FIXED_OVERFLOW",
        "E_WINDOW_MISMATCH",
        "E_SPILL_TAMPERED",
        "E_SPILL_READBACK_FAILED",
    }.issubset(codes)
    assert payload["progress"]["committed"] == 10
    assert payload["progress"]["failed"] == 2


def test_affected_groups_only_are_failed(anomaly_run: dict) -> None:
    status = json_stdout(anomaly_run["status"])
    failed = {
        tuple(group["group"]): group["failure"]["error_code"]
        for group in status["groups"]
        if group["status"] == "failed"
    }
    assert failed == {
        ("S00", 0): "E_FIXED_OVERFLOW",
        ("S01", 1): "E_WINDOW_MISMATCH",
        ("S02", 2): "E_SPILL_TAMPERED",
        ("S03", 3): "E_SPILL_READBACK_FAILED",
    }
    active = [group for group in status["groups"] if group["status"] == "active"]
    assert len(active) == 196
    assert all(group["version"] == 10 for group in active)


def test_diagnostics_keep_batch_column_and_group(anomaly_run: dict) -> None:
    failures = json_stdout(anomaly_run["execute"])["failures"]
    by_code = {failure["error_code"]: failure for failure in failures}
    assert by_code["E_MALFORMED_COLUMN"]["batch_seq"] == 2
    assert by_code["E_MALFORMED_COLUMN"]["column"] == "value"
    assert by_code["E_DIGEST_MISMATCH"]["batch_seq"] == 4
    assert by_code["E_FIXED_OVERFLOW"]["group"] == ["S00", 0]
    assert by_code["E_FIXED_OVERFLOW"]["column"] == "value"
    assert by_code["E_WINDOW_MISMATCH"]["group"] == ["S01", 1]
    assert by_code["E_SPILL_TAMPERED"]["group"] == ["S02", 2]
    assert by_code["E_SPILL_READBACK_FAILED"]["group"] == ["S03", 3]


def test_verify_reports_first_window_divergence(anomaly_run: dict) -> None:
    verified = anomaly_run["verify"]
    assert verified.returncode == 4
    payload = json_stdout(verified)
    assert payload["ok"] is False
    window_issue = next(
        issue for issue in payload["issues"] if issue["code"] == "E_WINDOW_MISMATCH"
    )
    divergence = window_issue["first_divergence"]
    assert divergence["group"] == ["S01", 1]
    assert divergence["window_start"] == 4
    assert divergence["expected"]["sum"] != divergence["actual"]["sum"]
    assert divergence["intermediate"]["expected_sum"] == divergence["expected"]["sum"]
    assert divergence["intermediate"]["actual_sum"] == divergence["actual"]["sum"]


def test_unaffected_group_results_are_not_rolled_back(
    anomaly_run: dict,
) -> None:
    anomaly_db = anomaly_run["root"] / "workspace" / "colagg.sqlite3"
    query = """
        SELECT item_id, value_json FROM results
        WHERE kind = 'aggregate' AND group_key_json = ?
        ORDER BY item_id
    """
    with sqlite3.connect(anomaly_db) as a:
        anomaly_rows = a.execute(query, ('["S10",0]',)).fetchall()
    assert anomaly_rows
    verified = json_stdout(anomaly_run["verify"])
    assert verified["aggregates"]["mismatches"] == []
