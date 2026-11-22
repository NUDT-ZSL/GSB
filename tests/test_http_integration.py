from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from conftest import (
    make_expected,
    make_instance,
    make_policy,
    make_service,
    reserve_ports,
    route,
)

from mesh_drift.mock_server import MockCluster
from mesh_drift.runner import reconcile_service, recover_all
from mesh_drift.store import Database
from mesh_drift.verify import verify_database

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _checkout_setup(instance_count: int = 1):
    routes = [
        route("r-stable", "/pay", "v1", 90),
        route("r-canary", "/pay", "v2", 10),
    ]
    ports = reserve_ports(instance_count)
    instances = [
        make_instance(
            ports[index],
            instance_id=f"svc-{chr(ord('a') + index)}",
            version=7,
            label="v1",
            routes=routes,
        )
        for index in range(instance_count)
    ]
    expected = make_expected([make_service("checkout", routes, instances)])
    return expected, instances, routes


def _run_cli(args: list[str], env: dict[str, str] | None = None, timeout: float = 20):
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = str(PROJECT_ROOT)
    if env:
        full_env.update(env)
    return subprocess.run(
        [PYTHON, "-m", "mesh_drift", *args],
        cwd=PROJECT_ROOT,
        env=full_env,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _write_inputs(tmp_path: Path, expected, policy):
    expected_path = tmp_path / "expected.json"
    policy_path = tmp_path / "policy.json"
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    return expected_path, policy_path


def test_http_classification_and_verify(tmp_path):
    expected, _instances, _routes = _checkout_setup(1)
    scenario = {
        "seed": 20261123,
        "default": "match",
        "rounds": {
            "1000": {
                "svc-a": {
                    "mode": "stale",
                    "version_offset": -1,
                    "remove_fields": ["healthy"],
                }
            }
        },
    }
    policy = make_policy(tolerance=5000, strict=True)
    database = Database(tmp_path / "ledger.db")
    with MockCluster(expected, scenario):
        summary = reconcile_service(
            database,
            expected=expected,
            policy=policy,
            service_name="checkout",
            logical_clock_ms=1000,
        )
    assert summary["decision_stats"]["MANUAL_INTERVENTION"] == 1
    entries = database.list_ledger_entries()
    types = {entry["drift_type"] for entry in entries}
    assert "VERSION_BEHIND" in types
    assert "REPORT_METADATA_MISSING" in types
    assert all(entry["evidence"]["source"]["kind"] == "loopback_http_report" for entry in entries)
    verification = verify_database(database)
    assert verification["ok"], verification["errors"]


def test_dedup_cooldown_new_entry_and_reopen(tmp_path):
    expected, _instances, _routes = _checkout_setup(1)
    scenario = {
        "seed": 20261123,
        "default": "match",
        "instances": {
            "svc-a": {
                "by_round": {
                    "1000": {
                        "mode": "weight_mismatch",
                        "rule_id": "r-stable",
                        "delta": -5,
                    },
                    "2000": {
                        "mode": "weight_mismatch",
                        "rule_id": "r-stable",
                        "delta": -5,
                    },
                    "5000": {
                        "mode": "weight_mismatch",
                        "rule_id": "r-stable",
                        "delta": -5,
                    },
                    "6000": {"mode": "match"},
                    "7000": {
                        "mode": "weight_mismatch",
                        "rule_id": "r-stable",
                        "delta": -5,
                    },
                }
            }
        },
    }
    policy = make_policy(tolerance=5000, strict=True, cooldown=2500)
    database = Database(tmp_path / "ledger.db")
    with MockCluster(expected, scenario):
        for clock in (1000, 2000, 5000, 6000, 7000):
            reconcile_service(
                database,
                expected=expected,
                policy=policy,
                service_name="checkout",
                logical_clock_ms=clock,
            )
    entries = sorted(database.list_ledger_entries(), key=lambda item: item["first_seen_clock_ms"])
    assert len(entries) == 2
    first, second = entries
    assert first["occurrences"] == 2
    assert first["first_seen_clock_ms"] == 1000
    assert first["last_seen_clock_ms"] == 2000
    assert first["status"] == "CLOSED"
    assert second["previous_entry_id"] == first["entry_id"]
    assert second["first_seen_clock_ms"] == 5000
    assert second["occurrences"] == 2
    assert second["status"] == "REOPENED"
    events = database.ledger_events(second["entry_id"])
    assert [event["event_type"] for event in events] == [
        "TOLERATED",
        "CLOSED",
        "REOPENED",
    ]
    verification = verify_database(database)
    assert verification["ok"], verification["errors"]


def test_budget_abandonment_and_started_error_isolation(tmp_path):
    expected, _instances, _routes = _checkout_setup(4)
    scenario = {
        "seed": 20261123,
        "default": "match",
        "rounds": {
            "1000": {
                "svc-a": {"mode": "timeout", "delay_ms": 500},
                "svc-b": {"mode": "match"},
            }
        },
    }
    policy = make_policy(
        tolerance=5000,
        strict=True,
        concurrency=2,
        budget=250,
        timeout=100,
    )
    database = Database(tmp_path / "ledger.db")
    with MockCluster(expected, scenario):
        summary = reconcile_service(
            database,
            expected=expected,
            policy=policy,
            service_name="checkout",
            logical_clock_ms=1000,
        )
    assert summary["started"] == ["svc-a", "svc-b"]
    assert [item["instance_id"] for item in summary["abandoned"]] == ["svc-c", "svc-d"]
    assert {item["error_code"] for item in summary["abandoned"]} == {
        "E_ROUND_BUDGET_EXHAUSTED"
    }
    outcomes = {item["instance_id"]: item for item in summary["outcomes"]}
    assert outcomes["svc-a"]["error_code"] == "E_REPORT_TIMEOUT"
    assert outcomes["svc-b"]["outcome"] == "PULLED_OK"
    assert outcomes["svc-c"]["outcome"] == "ABANDONED"
    states = {item["instance_id"]: item for item in database.instance_states()}
    assert set(states) == {"svc-a", "svc-b"}
    assert states["svc-a"]["anomaly_count"] == 1
    assert states["svc-b"]["anomaly_count"] == 0
    assert database.list_ledger_entries() == []
    assert summary["decision_stats"]["MANUAL_INTERVENTION"] == 0
    verification = verify_database(database)
    assert verification["ok"], verification["errors"]


def test_malformed_connection_and_version_regression_codes(tmp_path):
    routes = [
        route("r-stable", "/pay", "v1", 90),
        route("r-canary", "/pay", "v2", 10),
    ]
    malformed_port, connection_port, regression_port = reserve_ports(3)
    instances = [
        make_instance(
            malformed_port,
            instance_id="malformed-a",
            version=7,
            label="v1",
            routes=routes,
        ),
        make_instance(
            connection_port,
            instance_id="connection-b",
            version=7,
            label="v1",
            routes=routes,
        ),
        make_instance(
            regression_port,
            instance_id="regression-c",
            version=7,
            label="v1",
            routes=routes,
        ),
    ]
    expected = make_expected([make_service("checkout", routes, instances)])
    scenario = {
        "seed": 20261123,
        "default": "match",
        "rounds": {
            "1000": {
                "malformed-a": {"mode": "malformed"},
                "connection-b": {"mode": "connection_reset"},
                "regression-c": {"mode": "match"},
            },
            "2000": {
                "regression-c": {"mode": "version_regression", "version": 0},
            },
        },
    }
    policy = make_policy(tolerance=5000, budget=1000, concurrency=3)
    mock_expected = json.loads(json.dumps(expected))
    mock_expected["services"][0]["instances"] = [
        item
        for item in mock_expected["services"][0]["instances"]
        if item["instance_id"] != "connection-b"
    ]
    database = Database(tmp_path / "ledger.db")
    with MockCluster(mock_expected, scenario):
        first = reconcile_service(
            database,
            expected=expected,
            policy=policy,
            service_name="checkout",
            logical_clock_ms=1000,
            report_fetcher=None,
        )
        second = reconcile_service(
            database,
            expected=expected,
            policy=policy,
            service_name="checkout",
            logical_clock_ms=2000,
        )
    first_outcomes = {item["instance_id"]: item for item in first["outcomes"]}
    assert first_outcomes["malformed-a"]["error_code"] == "E_REPORT_MALFORMED"
    assert first_outcomes["connection-b"]["error_code"] == "E_REPORT_CONNECTION"
    second_outcomes = {item["instance_id"]: item for item in second["outcomes"]}
    assert second_outcomes["regression-c"]["error_code"] == "E_VERSION_REGRESSION"
    assert database.list_ledger_entries() == []


def test_hard_kill_after_ledger_writes_recovers_exactly(tmp_path):
    expected, _instances, _routes = _checkout_setup(1)
    scenario = {
        "seed": 20261123,
        "default": "match",
        "rounds": {
            "1000": {
                "svc-a": {
                    "mode": "weight_mismatch",
                    "rule_id": "r-stable",
                    "delta": -5,
                }
            },
            "2000": {
                "svc-a": {
                    "mode": "weight_mismatch",
                    "rule_id": "r-stable",
                    "delta": -5,
                }
            }
        },
    }
    policy = make_policy(tolerance=5000, strict=True)
    expected_path, policy_path = _write_inputs(tmp_path, expected, policy)
    normal_db = tmp_path / "normal.db"
    crash_db = tmp_path / "crash.db"
    normal_store = Database(normal_db)
    crash_store = Database(crash_db)
    with MockCluster(expected, scenario):
        for database in (normal_store, crash_store):
            reconcile_service(
                database,
                expected=expected,
                policy=policy,
                service_name="checkout",
                logical_clock_ms=1000,
            )
        reconcile_service(
            normal_store,
            expected=expected,
            policy=policy,
            service_name="checkout",
            logical_clock_ms=2000,
        )
        crash = _run_cli(
            [
                "reconcile",
                "--db",
                str(crash_db),
                "--expected",
                str(expected_path),
                "--policy",
                str(policy_path),
                "--service",
                "checkout",
                "--clock",
                "2000",
            ],
            env={"MESH_DRIFT_CRASH_AFTER_WRITES": "1"},
        )
        assert crash.returncode == -signal.SIGKILL, crash.stderr
        assert len(crash_store.list_rounds()) == 1
        assert len(crash_store.list_ledger_entries()) == 1
        assert crash_store.list_ledger_entries()[0]["occurrences"] == 1
        recover = _run_cli(["recover", "--db", str(crash_db)])
        assert recover.returncode == 0, recover.stdout + recover.stderr
    verification = verify_database(crash_store)
    assert verification["ok"], verification["errors"]
    assert normal_store.database_digest() == crash_store.database_digest()
    assert crash_store.list_ledger_entries()[0]["occurrences"] == 2


def test_hard_kill_after_fetch_uses_journal(tmp_path):
    expected, _instances, _routes = _checkout_setup(1)
    scenario = {
        "seed": 20261123,
        "default": "match",
        "rounds": {
            "1000": {"svc-a": {"mode": "stale", "version_offset": -1}}
        },
    }
    policy = make_policy()
    expected_path, policy_path = _write_inputs(tmp_path, expected, policy)
    normal_db = tmp_path / "normal.db"
    crash_db = tmp_path / "crash.db"
    normal_store = Database(normal_db)
    crash_store = Database(crash_db)
    with MockCluster(expected, scenario):
        reconcile_service(
            normal_store,
            expected=expected,
            policy=policy,
            service_name="checkout",
            logical_clock_ms=1000,
        )
        crash = _run_cli(
            [
                "reconcile",
                "--db",
                str(crash_db),
                "--expected",
                str(expected_path),
                "--policy",
                str(policy_path),
                "--service",
                "checkout",
                "--clock",
                "1000",
            ],
            env={"MESH_DRIFT_CRASH_POINT": "after_fetch"},
        )
        assert crash.returncode == -signal.SIGKILL
        run = crash_store.list_unfinished_runs()
        assert len(run) == 1
        assert run[0]["state"] == "READY"
        assert run[0]["observations"]["svc-a"]["outcome"] == "PULLED_OK"
        recovered = recover_all(crash_store)
        assert len(recovered) == 1
    assert verify_database(crash_store)["ok"]
    assert normal_store.database_digest() == crash_store.database_digest()


def test_concurrent_services_and_idempotent_same_round(tmp_path):
    routes_a = [
        route("a-stable", "/a", "v1", 100),
    ]
    routes_b = [
        route("b-stable", "/b", "v1", 100),
    ]
    port_a, port_b = reserve_ports(2)
    instance_a = make_instance(port_a, instance_id="a-1", version=3, label="v1", routes=routes_a)
    instance_b = make_instance(port_b, instance_id="b-1", version=4, label="v1", routes=routes_b)
    expected = make_expected(
        [
            make_service("service-a", routes_a, [instance_a]),
            make_service("service-b", routes_b, [instance_b]),
        ]
    )
    policy = make_policy(budget=500)
    scenario = {
        "seed": 20261123,
        "default": "match",
        "instances": {
            "a-1": {
                "by_round": {"1000": {"mode": "stale", "version_offset": -1}}
            }
        },
    }
    expected_path, policy_path = _write_inputs(tmp_path, expected, policy)
    db_path = tmp_path / "shared.db"
    with MockCluster(expected, scenario):
        cmd_a = [
            PYTHON,
            "-m",
            "mesh_drift",
            "reconcile",
            "--db",
            str(db_path),
            "--expected",
            str(expected_path),
            "--policy",
            str(policy_path),
            "--service",
            "service-a",
            "--clock",
            "1000",
        ]
        cmd_b = [
            PYTHON,
            "-m",
            "mesh_drift",
            "reconcile",
            "--db",
            str(db_path),
            "--expected",
            str(expected_path),
            "--policy",
            str(policy_path),
            "--service",
            "service-b",
            "--clock",
            "1000",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        process_a = subprocess.Popen(cmd_a, cwd=PROJECT_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process_b = subprocess.Popen(cmd_b, cwd=PROJECT_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out_a, err_a = process_a.communicate(timeout=20)
        out_b, err_b = process_b.communicate(timeout=20)
        assert process_a.returncode == 0, out_a + err_a
        assert process_b.returncode == 0, out_b + err_b

        duplicate = subprocess.Popen(
            cmd_a,
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        duplicate_out, duplicate_err = duplicate.communicate(timeout=20)
        assert duplicate.returncode == 0, duplicate_out + duplicate_err
    database = Database(db_path)
    rounds = database.list_rounds()
    assert len(rounds) == 2
    entries = database.list_ledger_entries()
    assert len(entries) == 1
    assert entries[0]["occurrences"] == 1
    assert verify_database(database)["ok"]
