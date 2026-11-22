from __future__ import annotations

import sqlite3

import pytest
from conftest import (
    make_expected,
    make_instance,
    make_policy,
    make_service,
    reserve_ports,
    route,
)

from mesh_drift.cli import run as cli_run
from mesh_drift.errors import E_CLOCK_REGRESSION, DriftError
from mesh_drift.mock_server import MockCluster
from mesh_drift.runner import reconcile_service
from mesh_drift.store import Database
from mesh_drift.verify import verify_database


def _setup():
    routes = [
        route("r1", "/a", "v1", 90),
        route("r2", "/a", "v2", 10),
    ]
    port = reserve_ports(1)[0]
    instance = make_instance(port, instance_id="a", version=5, label="v1", routes=routes)
    expected = make_expected([make_service("svc", routes, [instance])])
    return expected, routes


def test_logical_clock_regression_is_rejected(tmp_path):
    expected, _routes = _setup()
    database = Database(tmp_path / "ledger.db")
    scenario = {"seed": 1, "default": "match"}
    with MockCluster(expected, scenario):
        reconcile_service(
            database,
            expected=expected,
            policy=make_policy(),
            service_name="svc",
            logical_clock_ms=2000,
            round_id="round-explicit-2000",
        )
        with pytest.raises(DriftError) as caught:
            reconcile_service(
                database,
                expected=expected,
                policy=make_policy(),
                service_name="svc",
                logical_clock_ms=1000,
                round_id="round-explicit-1000",
            )
    assert caught.value.code == E_CLOCK_REGRESSION
    assert [item["round_id"] for item in database.list_rounds()] == [
        "round-explicit-2000"
    ]


def test_multiple_diffs_of_same_type_do_not_orphan_entries(tmp_path):
    expected, _routes = _setup()
    expected["services"][0]["instances"][0]["pull_cost_logic_ms"] = 100
    scenario = {
        "seed": 1,
        "default": "match",
        "rounds": {
            "1000": {
                "a": {
                    "mode": "weight_mismatch",
                    "rule_id": "r1",
                    "delta": -5,
                    "rule_overrides": {"r2": {"weight": 15}},
                }
            },
            "2000": {
                "a": {
                    "mode": "weight_mismatch",
                    "rule_id": "r1",
                    "delta": -5,
                    "rule_overrides": {"r2": {"weight": 15}},
                }
            },
            "3000": {"mode": "match"},
        },
    }
    database = Database(tmp_path / "ledger.db")
    with MockCluster(expected, scenario):
        for clock in (1000, 2000, 3000):
            reconcile_service(
                database,
                expected=expected,
                policy=make_policy(cooldown=3000),
                service_name="svc",
                logical_clock_ms=clock,
            )
    weight_entries = [
        entry
        for entry in database.list_ledger_entries()
        if entry["drift_type"] == "ROUTE_WEIGHT_MISMATCH"
    ]
    assert len(weight_entries) == 2
    assert {entry["occurrences"] for entry in weight_entries} == {2}
    assert all(entry["status"] == "CLOSED" for entry in weight_entries)
    assert verify_database(database)["ok"]


def test_cli_output_is_byte_stable(tmp_path, capsys):
    expected, routes = _setup()
    import json

    expected_path = tmp_path / "expected.json"
    policy_path = tmp_path / "policy.json"
    reports_path = tmp_path / "reports.json"
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    policy_path.write_text(json.dumps(make_policy()), encoding="utf-8")
    report = {
        "service": "svc",
        "instance_id": "a",
        "version": 4,
        "digest": "sha256:wrong",
        "logical_clock_ms": 1000,
        "healthy": True,
        "rules": routes,
    }
    reports_path.write_text(json.dumps({"reports": {"a": report}}), encoding="utf-8")
    args = [
        "analyze",
        "--expected",
        str(expected_path),
        "--policy",
        str(policy_path),
        "--reports",
        str(reports_path),
        "--service",
        "svc",
        "--clock",
        "1000",
    ]
    assert cli_run(args) == 0
    first = capsys.readouterr().out
    assert cli_run(args) == 0
    second = capsys.readouterr().out
    assert first == second
    parsed = json.loads(first)
    assert parsed["analysis"]["instances"][0]["instance_id"] == "a"


def test_verify_detects_tampered_revision(tmp_path):
    expected, _routes = _setup()
    database = Database(tmp_path / "ledger.db")
    scenario = {
        "seed": 1,
        "default": "match",
        "rounds": {"1000": {"a": {"mode": "stale", "version_offset": -1}}},
    }
    with MockCluster(expected, scenario):
        reconcile_service(
            database,
            expected=expected,
            policy=make_policy(),
            service_name="svc",
            logical_clock_ms=1000,
        )
    assert verify_database(database)["ok"]
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "UPDATE ledger_revisions SET canonical_digest='sha256:tampered'"
        )
    result = verify_database(database)
    assert not result["ok"]
    assert any(item["code"] == "E_VERIFY_REVISION_DIGEST" for item in result["errors"])
