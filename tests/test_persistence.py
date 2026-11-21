from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from kubesim.errors import ConflictError
from kubesim.models import Resources, parse_dataset
from kubesim.scheduler import Scheduler
from kubesim.store import Store

from .conftest import (
    CONFIG,
    INJECTIONS,
    NODES,
    PODS,
    ROOT,
    load_sample,
    read_json,
)


def _sample_dataset():
    return parse_dataset(
        read_json(NODES),
        read_json(PODS),
        read_json(CONFIG),
        read_json(INJECTIONS),
    )


@pytest.mark.parametrize(
    "phase,plugin",
    [
        ("filter", "node_affinity"),
        ("score", "resource_bin_packing"),
        ("bind", None),
    ],
)
def test_forced_termination_recovery_is_atomic_and_idempotent(tmp_path, phase, plugin):
    db_path = tmp_path / "state.sqlite"
    injections_path = tmp_path / "injections.json"
    injection = {
        "type": "terminate",
        "pod": "db-primary",
        "phase": phase,
        "exit_code": 137,
    }
    if plugin is not None:
        injection["plugin"] = plugin
    injections_path.write_text(
        json.dumps({"injections": [injection]}),
        encoding="utf-8",
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "kubesim",
            "schedule",
            "--db",
            str(db_path),
            "--nodes",
            str(NODES),
            "--pods",
            str(PODS),
            "--config",
            str(CONFIG),
            "--injections",
            str(injections_path),
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert crashed.returncode == 137

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    running = connection.execute(
        "SELECT status FROM rounds"
    ).fetchone()
    assert running["status"] == "RUNNING"
    before = connection.execute("SELECT COUNT(*) AS n FROM bindings").fetchone()["n"]
    connection.close()

    resumed = subprocess.run(
        [
            sys.executable,
            "-m",
            "kubesim",
            "resume",
            "--db",
            str(db_path),
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr
    report = json.loads(resumed.stdout)
    assert report["recovery"]["resumed"] is True
    assert report["summary"]["bound"] >= 7
    assert report["summary"]["total"] == 10
    assert (
        report["summary"]["bound"]
        + report["summary"]["budget_unstarted"]
        + report["summary"]["filter_failed"]
        + report["summary"]["active_abandon"]
        + report["summary"]["bind_failed"]
        == 10
    )
    by_pod = {item["pod"]: item for item in report["pods"]}
    assert by_pod["db-primary"]["outcome"] == "BOUND"
    connection = sqlite3.connect(db_path)
    assert (
        connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0]
        == report["summary"]["bound"]
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM bindings WHERE pod_name='db-primary'"
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT round_id, pod_name, pod_version, COUNT(*) AS n
                FROM bindings GROUP BY 1,2,3 HAVING n > 1
            )
            """
        ).fetchone()[0]
        == 0
    )
    for row in connection.execute("SELECT * FROM nodes"):
        expected_cpu = row[1] - connection.execute(
            "SELECT COALESCE(SUM(cpu_millicores),0) FROM bindings WHERE node_name=?",
            (row[0],),
        ).fetchone()[0]
        assert row[4] == expected_cpu
    connection.close()
    verify = Store(db_path).verify()
    assert verify["ok"] is True, verify["errors"]


def test_multiple_processes_coalesce_to_one_round_and_one_binding_per_pod(tmp_path):
    db_path = tmp_path / "state.sqlite"
    load_sample(db_path)
    common = [
        sys.executable,
        "-m",
        "kubesim",
        "schedule",
        "--db",
        str(db_path),
        "--nodes",
        str(NODES),
        "--pods",
        str(PODS),
        "--config",
        str(CONFIG),
        "--injections",
        str(INJECTIONS),
    ]
    output_paths = [tmp_path / "p1.json", tmp_path / "p2.json"]
    processes = [
        subprocess.Popen(
            [*common, "--instance-id", f"proc-{index}", "--output", str(output_paths[index])],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(2)
    ]
    results = [process.communicate(timeout=60) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], results
    first = output_paths[0].read_bytes()
    second = output_paths[1].read_bytes()
    assert first == second
    connection = sqlite3.connect(db_path)
    assert connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 8
    assert (
        connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT round_id, pod_name, pod_version, COUNT(*) AS n
                FROM bindings GROUP BY 1,2,3 HAVING n > 1
            )
            """
        ).fetchone()[0]
        == 0
    )
    connection.close()


def test_idempotent_commit_and_stale_resource_version_rejection(tmp_path):
    nodes = {
        "nodes": [
            {
                "name": "single",
                "allocatable": {
                    "cpu_millicores": 2000,
                    "memory_mib": 2048,
                    "gpu": 0,
                },
                "labels": {"zone": "z1"},
            }
        ]
    }
    pods = {
        "pods": [
            {
                "name": "one",
                "priority": 2,
                "resources": {
                    "cpu_millicores": 100,
                    "memory_mib": 64,
                    "gpu": 0,
                },
            },
            {
                "name": "two",
                "priority": 1,
                "resources": {
                    "cpu_millicores": 100,
                    "memory_mib": 64,
                    "gpu": 0,
                },
            },
        ]
    }
    config = {
        "filters": [
            "resource_feasibility",
            "taint_toleration",
            "node_affinity",
            "pod_anti_affinity",
            "topology_spread",
        ],
        "scores": {
            "resource_bin_packing": 1,
            "node_affinity": 1,
            "topology_balance": 1,
        },
        "total_time_budget_ms": 10,
        "max_concurrent_per_workload": 1,
        "retry_budget": 1,
        "logical_clock_step_ms": 1,
    }
    dataset = parse_dataset(nodes, pods, config, None)
    store = Store(tmp_path / "state.sqlite")
    store.import_dataset(dataset)
    versions = store.node_versions()
    input_digest = store.compute_input_digest(
        dataset.digest,
        dataset.config,
        dataset.pods,
        versions,
    )
    round_row, _ = store.create_round(
        round_id="manual-round",
        batch_digest=dataset.digest,
        input_digest=input_digest,
        config=dataset.config,
        pods_in_order=list(dataset.pods),
        versions=versions,
        start_clock_ms=0,
    )
    chain = {"pod": "one", "marker": "deterministic"}
    commit_args = {
        "round_id": round_row["round_id"],
        "round_input_digest": input_digest,
        "pod_name": "one",
        "pod_version": next(pod for pod in dataset.pods if pod.name == "one").version,
        "node_name": "single",
        "resources": Resources(100, 64, 0),
        "expected_node_version": 0,
        "topology_domains": {},
        "clock_ms": 1,
        "chain": chain,
        "started_at_ms": 0,
    }
    first = store.commit_binding(**commit_args)
    assert first["idempotent"] is False
    uncommitted_round_view = store.status()
    assert uncommitted_round_view["snapshot_round_id"] is None
    assert (
        uncommitted_round_view["nodes"][0]["remaining"]["cpu_millicores"] == 2000
    )
    assert uncommitted_round_view["topology_domains"] == []
    duplicate = store.commit_binding(**commit_args)
    assert duplicate["idempotent"] is True
    assert duplicate["binding_id"] == first["binding_id"]

    with pytest.raises(ConflictError) as conflict:
        store.commit_binding(
            **{
                **commit_args,
                "resources": Resources(101, 64, 0),
            }
        )
    assert conflict.value.code == "E_IDEMPOTENCY_CONFLICT"
    with pytest.raises(ConflictError) as chain_conflict:
        store.commit_binding(
            **{
                **commit_args,
                "chain": {"pod": "one", "marker": "different"},
            }
        )
    assert chain_conflict.value.code == "E_IDEMPOTENCY_CONFLICT"

    second_pod = next(pod for pod in dataset.pods if pod.name == "two")
    with pytest.raises(ConflictError) as stale:
        store.commit_binding(
            **{
                **commit_args,
                "pod_name": "two",
                "pod_version": second_pod.version,
                "expected_node_version": 0,
            }
        )
    assert stale.value.code == "E_STALE_RESOURCE_VERSION"
