from __future__ import annotations

import json

from kubesim.jsonutil import pretty_json
from kubesim.models import parse_dataset
from kubesim.scheduler import Scheduler
from kubesim.store import Store

from .conftest import CONFIG, INJECTIONS, NODES, PODS, load_sample, read_json


def _schedule(db_path):
    store = Store(db_path)
    dataset = parse_dataset(
        read_json(NODES),
        read_json(PODS),
        read_json(CONFIG),
        read_json(INJECTIONS),
    )
    store.import_dataset(dataset)
    return store, Scheduler(store, dataset, "test").schedule()


def test_filter_chain_is_complete_sorted_and_traceable(tmp_path):
    _, report = _schedule(tmp_path / "state.sqlite")
    pods = {item["pod"]: item for item in report["pods"]}

    for pod in report["pods"]:
        if pod["started"]:
            assert [node["node"] for node in pod["filters"]] == sorted(
                node["node"] for node in pod["filters"]
            )
            for node in pod["filters"]:
                assert [plugin["sequence"] for plugin in node["plugins"]] == [
                    1,
                    2,
                    3,
                    4,
                    5,
                ]

    cache_b = pods["cache-b"]
    n4 = next(node for node in cache_b["filters"] if node["node"] == "n4")
    assert n4["first_failure_sequence"] == 4
    assert n4["first_failure_plugin"] == "pod_anti_affinity"

    grid_c = pods["grid-c"]
    assert grid_c["started"] is True
    assert grid_c["outcome"] == "FILTER_FAILED"
    reasons = {
        node["node"]: (node["first_failure_sequence"], node["first_failure_plugin"])
        for node in grid_c["filters"]
    }
    assert reasons["n1"] == (5, "topology_spread")
    assert reasons["n2"] == (5, "topology_spread")
    assert reasons["n3"] == (2, "taint_toleration")
    assert reasons["n4"] == (1, "resource_feasibility")
    assert reasons["n5"] == (3, "node_affinity")


def test_scoring_normalization_error_bound_and_precision_marker(tmp_path):
    _, report = _schedule(tmp_path / "state.sqlite")
    db_pod = next(item for item in report["pods"] if item["pod"] == "db-primary")
    assert db_pod["precision_sensitive"] is True
    scoring = db_pod["scoring"]
    assert scoring["selected_node"] == "n1"
    assert scoring["tolerance"] == "0.000001"
    assert scoring["top_two_difference"] == "0"
    assert len(scoring["error_sources"]) >= 5
    scores = {item["node"]: item for item in scoring["node_scores"]}
    assert scores["n1"]["total_score"] == scores["n2"]["total_score"]
    for node in scores.values():
        assert node["weight_sum"] == "0.9"
        assert float(node["error_bound"]) > 0
        assert [plugin["normalization_rounding"] for plugin in node["plugins"]] == [
            "ROUND_HALF_EVEN",
            "ROUND_HALF_EVEN",
            "ROUND_HALF_EVEN",
        ]
        directions = {plugin["rounding_direction"] for plugin in node["plugins"]}
        assert directions <= {"UP", "DOWN", "EXACT"}


def test_budget_preserves_started_conclusions_and_abandons_only_unstarted(tmp_path):
    _, report = _schedule(tmp_path / "state.sqlite")
    assert report["summary"] == {
        "total": 10,
        "bound": 8,
        "filter_failed": 1,
        "budget_unstarted": 1,
        "active_abandon": 0,
        "bind_failed": 0,
    }
    assert report["logical_clock"]["overrun_ms"] > 0
    pods = {item["pod"]: item for item in report["pods"]}
    assert pods["grid-c"]["started"] is True
    assert pods["grid-c"]["outcome"] == "FILTER_FAILED"
    assert pods["tail-b"]["started"] is False
    assert pods["tail-b"]["outcome"] == "BUDGET_UNSTARTED"
    assert pods["tail-b"]["reason_code"] == "E_BUDGET_UNSTARTED"
    assert pods["tail-b"]["finished_at_ms"] == report["logical_clock"]["deadline_ms"]
    assert all(
        pod["outcome"] != "BUDGET_UNSTARTED" or pod["started"] is False
        for pod in report["pods"]
    )


def test_injected_active_abandon_is_a_started_terminal_decision(tmp_path):
    dataset = parse_dataset(
        read_json(NODES),
        read_json(PODS),
        read_json(CONFIG),
        {
            "injections": [
                {
                    "type": "abandon",
                    "pod": "db-primary",
                    "phase": "after_filter",
                    "reason": "operator cancelled this workload",
                }
            ]
        },
    )
    store = Store(tmp_path / "active-abandon.sqlite")
    store.import_dataset(dataset)
    report = Scheduler(store, dataset, "test").schedule()
    pod = next(item for item in report["pods"] if item["pod"] == "db-primary")
    assert pod["started"] is True
    assert pod["outcome"] == "ACTIVE_ABANDON"
    assert pod["reason_code"] == "E_INJECTED_ACTIVE_ABANDON"
    assert pod["reason"] == "operator cancelled this workload"
    assert pod["selected_node"] is None
    assert report["summary"]["active_abandon"] == 1
    assert store.verify()["ok"] is True


def test_two_clean_runs_are_byte_identical_and_repeat_is_idempotent(tmp_path):
    store_a, report_a = _schedule(tmp_path / "a.sqlite")
    store_b, report_b = _schedule(tmp_path / "b.sqlite")
    assert pretty_json(report_a).encode("utf-8") == pretty_json(report_b).encode("utf-8")
    assert report_a["content_digest"] == report_b["content_digest"]

    repeated = Scheduler(store_a, store_a.latest_dataset(), "again").schedule()
    assert pretty_json(repeated) == pretty_json(report_a)
    assert len(store_a.status()["rounds"]) == 1


def test_verify_detects_tampered_report(tmp_path):
    store, report = _schedule(tmp_path / "state.sqlite")
    assert store.verify()["ok"] is True
    report_path = tmp_path / "report.json"
    report_path.write_text(pretty_json(report), encoding="utf-8")
    assert store.verify(str(report_path))["ok"] is True
    tampered = json.loads(json.dumps(report))
    tampered["nodes"][0]["remaining"]["cpu_millicores"] -= 1
    report_path.write_text(pretty_json(tampered), encoding="utf-8")
    assert store.verify(str(report_path))["ok"] is False


def test_cli_explain_and_status_use_committed_snapshot(tmp_path):
    db_path = tmp_path / "state.sqlite"
    load_sample(db_path)
    scheduled = cli_schedule = None
    from .conftest import cli

    scheduled = cli("schedule", "--db", str(db_path))
    assert scheduled.returncode == 0, scheduled.stderr
    explain = cli("explain", "--db", str(db_path), "--pod", "db-primary")
    assert explain.returncode == 0, explain.stderr
    payload = json.loads(explain.stdout)
    assert payload["pod"]["selected_node"] == "n1"
    assert payload["pod"]["precision_sensitive"] is True
    status = cli("status", "--db", str(db_path))
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["logical_clock_ms"] == json.loads(
        scheduled.stdout
    )["logical_clock"]["end_ms"]
    assert status_payload["snapshot_round_id"] is not None
