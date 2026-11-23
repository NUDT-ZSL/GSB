from __future__ import annotations

import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from coldstart_ledger.engine import ColdStartEngine
from coldstart_ledger.errors import BusinessError, ErrorCode
from coldstart_ledger.store import canonical_json, sha256_text

from conftest import make_engine, write_bundle


def _worker_admit(input_dir: str, db_path: str, instance_id: str, queue) -> None:
    try:
        from coldstart_ledger.engine import ColdStartEngine as WorkerEngine
        from coldstart_ledger.loader import load_inputs as worker_load

        engine = WorkerEngine(db_path, worker_load(input_dir))
        result = engine.admit(instance_id, "concurrent-round", 1)
        queue.put(("ok", instance_id, result["ledger_revision"]["version"]))
    except BusinessError as exc:
        queue.put(("error", instance_id, exc.code))
    except Exception as exc:  # pragma: no cover - diagnostic path
        queue.put(("exception", instance_id, repr(exc)))


def test_crash_before_commit_keeps_previous_current_pointer(tmp_path: Path) -> None:
    def crash(point: str) -> None:
        if point == "after_version_insert":
            raise RuntimeError("simulated forced termination")

    engine = make_engine(tmp_path, crash_hook=crash)
    before = engine.snapshot()
    with pytest.raises(RuntimeError):
        engine.run_round("r1", 1)

    reopened = make_engine(tmp_path)
    recovered = reopened.recover()
    assert recovered["recovered"] is True
    assert recovered["revision"]["version"] == before["revision"]["version"]
    assert recovered["state_sha256"] == before["state_sha256"]
    assert recovered["state"]["rounds"] == []
    assert recovered["audit"]["ok"] is True
    assert reopened.run_round("r1", 1).to_dict()["summary"]["admitted"] == ["i1"]


def test_canonical_replay_is_bitwise_identical(tmp_path: Path) -> None:
    instances = [
        {
            "instance_id": "i1",
            "service": "svc",
            "priority": 10,
            "requested_ephemeral_bytes": 1000,
            "required_layers": ["sha256:l1"],
            "deadline_ms": 100,
        },
        {
            "instance_id": "i2",
            "service": "svc",
            "priority": 20,
            "requested_ephemeral_bytes": 1000,
            "required_layers": ["sha256:l2"],
            "deadline_ms": 100,
        },
    ]
    layers = [
        {"digest": "sha256:l1", "bytes": 777, "node": "node-a", "ref_count": 1},
        {"digest": "sha256:l2", "bytes": 999, "node": "node-a", "ref_count": 1},
    ]
    nodes = [
        {
            "node": "node-a",
            "available_ephemeral_bytes": 100_000,
            "image_layer_bytes": 2000,
            "max_concurrent_cold_starts": 2,
        }
    ]
    engine_a = make_engine(
        tmp_path / "a",
        instances=instances,
        layers=layers,
        nodes=nodes,
        config={
            "layer_read_bandwidth_bytes_per_second": 3,
            "round_logical_time_budget_ms": 1_000_000,
        },
    )
    engine_b = make_engine(
        tmp_path / "b",
        instances=instances,
        layers=layers,
        nodes=nodes,
        config={
            "layer_read_bandwidth_bytes_per_second": 3,
            "round_logical_time_budget_ms": 1_000_000,
        },
    )
    for engine in (engine_a, engine_b):
        engine.run_round("r1", 1)
        engine.complete_reading("i1", "r1", ["sha256:l1"], 2)
        engine.complete_writing("i1", "r1", 3)
    state_a = engine_a.snapshot()["state"]
    state_b = engine_b.snapshot()["state"]
    assert canonical_json(state_a) == canonical_json(state_b)
    assert engine_a.snapshot()["state_sha256"] == engine_b.snapshot()["state_sha256"]


def test_multiprocess_same_idempotency_key_charges_once(tmp_path: Path) -> None:
    write_bundle(tmp_path)
    database = tmp_path / "ledger.db"
    engine = make_engine(tmp_path)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [
        context.Process(
            target=_worker_admit,
            args=(str(tmp_path), str(database), "i1", queue),
        )
        for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    results = [queue.get(timeout=5) for _ in workers]
    assert all(result[0] == "ok" for result in results)
    assert len({result[2] for result in results}) == 1
    state = engine.snapshot()["state"]
    assert state["nodes"]["node-a"]["charged_ephemeral_bytes"] == 1000
    assert engine.audit()["ok"] is True


def test_multiprocess_different_instances_do_not_overbook_node(tmp_path: Path) -> None:
    instances = [
        {
            "instance_id": f"i{index}",
            "service": "svc",
            "priority": 10,
            "requested_ephemeral_bytes": 1000,
            "required_layers": ["sha256:l1"],
            "deadline_ms": 100,
        }
        for index in range(1, 5)
    ]
    nodes = [
        {
            "node": "node-a",
            "available_ephemeral_bytes": 100_000,
            "image_layer_bytes": 1000,
            "max_concurrent_cold_starts": 2,
        }
    ]
    write_bundle(tmp_path, instances=instances, nodes=nodes)
    database = tmp_path / "ledger.db"
    engine = make_engine(tmp_path, instances=instances, nodes=nodes)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [
        context.Process(
            target=_worker_admit,
            args=(str(tmp_path), str(database), f"i{index}", queue),
        )
        for index in range(1, 5)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    results = [queue.get(timeout=5) for _ in workers]
    successes = [result for result in results if result[0] == "ok"]
    failures = [result for result in results if result[0] == "error"]
    assert len(successes) == 2
    assert len(failures) == 2
    assert {result[2] for result in failures} == {ErrorCode.NO_CAPACITY}
    state = engine.snapshot()["state"]
    assert state["nodes"]["node-a"]["active_count"] == 2
    assert state["nodes"]["node-a"]["charged_ephemeral_bytes"] == 2000
    assert engine.audit()["ok"] is True


def test_detects_corrupted_ledger_checksum(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    database = tmp_path / "ledger.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE ledger_versions SET state_json = ? WHERE version = (SELECT version FROM current_ledger WHERE singleton = 1)",
        (json.dumps({"tampered": True}),),
    )
    connection.commit()
    connection.close()
    with pytest.raises(BusinessError) as caught:
        engine.snapshot()
    assert caught.value.code == ErrorCode.LEDGER_CORRUPTION


def test_idempotency_key_reuse_with_different_payload_conflicts(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.admit("i1", "r1", 1, source="external")
    with pytest.raises(BusinessError) as caught:
        engine.admit("i1", "r1", 2, source="external")
    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT

