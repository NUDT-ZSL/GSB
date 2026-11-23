from __future__ import annotations

from pathlib import Path

import pytest

from scaler.engine import Autoscaler
from scaler.errors import BusinessError
from scaler.launch_client import LaunchClient
from scaler.models import canonical_json
from scaler.state import validate_state
from scaler.storage import SQLiteStore


def test_six_round_lifecycle_budget_and_ledger(
    tmp_path: Path, spec
) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    engine = Autoscaler(store, spec)
    result = engine.run(rounds=6)
    state = result["status"]
    validate_state(store.load()[0])

    assert state["state_revision"] == 6
    assert state["round_watermark"] == 6
    assert state["phase_counts"]["COLD_STARTING"] == 0
    assert state["phase_counts"]["DRAINING"] == 0
    assert state["phase_counts"]["RECYCLED"] >= 1

    classifications = {
        disposition["classification"]
        for round_data in state["recent_rounds"]
        for disposition in round_data["dispositions"]
    }
    assert "ACTIVELY_ABANDONED" in classifications
    assert "LAUNCH_FAILED" in classifications
    assert "WITHIN_BUDGET_NOT_STARTED" in classifications
    assert "HEALTH_FAILURE_DRAINING" in classifications
    assert "STARTED_SUCCEEDED" in classifications

    for round_data in state["recent_rounds"]:
        budget = round_data["budget"]
        assert budget["reserved_ms"] <= budget["total_ms"]
        assert budget["remaining_ms"] == budget["total_ms"] - budget["reserved_ms"]
        for operation in round_data["operations"].values():
            assert operation["status"] == "DONE"
            assert operation["result"] is not None

    raw = store.load()[0]
    for node in raw["nodes"].values():
        assert node["used_instances"] >= 0
        assert all(value >= 0 for value in node["gpu_remaining_mib"])

    guard_phases = [
        replica["phase"]
        for replica in state["replicas"]
        if replica["replica_id"].startswith("guard-gamma-")
    ]
    assert "RECYCLED" in guard_phases
    assert "READY" in guard_phases


def test_request_id_is_idempotent(tmp_path: Path, spec) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    engine = Autoscaler(store, spec)
    first = engine.run_round(clock_ms=1000, request_id="acceptance-001")
    before = store.load()
    second = engine.run_round(clock_ms=1000, request_id="acceptance-001")
    after = store.load()
    assert second["idempotent"] is True
    assert second["round_no"] == first["round_no"]
    assert before[1] == after[1]
    assert canonical_json(before[0]) == canonical_json(after[0])

    with pytest.raises(BusinessError) as exc:
        engine.run_round(clock_ms=2000, request_id="acceptance-001")
    assert exc.value.code == "E_REQUEST_CONFLICT"


def test_repeated_runs_with_same_input_are_byte_identical(
    tmp_path: Path, spec
) -> None:
    first_store = SQLiteStore(tmp_path / "first.db")
    second_store = SQLiteStore(tmp_path / "second.db")
    Autoscaler(first_store, spec).run(rounds=3)
    Autoscaler(second_store, spec).run(rounds=3)
    first_state = first_store.load()[0]
    second_state = second_store.load()[0]
    assert first_state == second_state
    assert canonical_json(first_state) == canonical_json(second_state)


def test_connection_refused_is_a_distinct_launch_failure(
    tmp_path: Path, base_spec
) -> None:
    from dataclasses import replace

    bad_spec = replace(
        base_spec,
        config=replace(base_spec.config, launch_target_url="http://127.0.0.1:9"),
    )
    store = SQLiteStore(tmp_path / "state.db")
    engine = Autoscaler(store, bad_spec)
    result = engine.run(rounds=1)
    failures = [
        item
        for item in result["rounds"][0]["round"]["dispositions"]
        if item["classification"] == "LAUNCH_FAILED"
    ]
    assert failures
    assert all(item["reason_code"] == "E_LAUNCH_CONNECTION_REFUSED" for item in failures)
    replicas = result["status"]["replicas"]
    assert all(item["phase"] == "PENDING" for item in replicas if item["last_error_code"])


def test_cold_start_budget_exceeded_code(tmp_path: Path, base_spec) -> None:
    from dataclasses import replace

    from scaler.stub import StubServer

    scenario = {
        "services": {
            "chat-alpha": {"mode": "success", "delay_ms": 300},
            "embed-beta": {"mode": "success", "delay_ms": 1},
            "guard-gamma": {"mode": "success", "delay_ms": 1},
        }
    }
    with StubServer(port=0, scenario=scenario) as stub:
        slow_spec = replace(
            base_spec,
            config=replace(base_spec.config, launch_target_url=stub.url),
        )
        store = SQLiteStore(tmp_path / "state.db")
        engine = Autoscaler(store, slow_spec)
        result = engine.run(rounds=1)
        codes = {
            item["reason_code"]
            for item in result["rounds"][0]["round"]["dispositions"]
            if item["classification"] == "LAUNCH_FAILED"
        }
        assert "E_COLD_START_BUDGET_EXCEEDED" in codes
        slow_operation = next(
            operation
            for operation in result["rounds"][0]["round"]["operations"].values()
            if operation["service"] == "chat-alpha"
        )
        queried = LaunchClient(stub.url).query_operation(
            slow_operation["operation_id"], cold_start_budget_ms=250
        )
        assert queried is not None
        assert queried.code == "E_COLD_START_BUDGET_EXCEEDED"
        assert result["ok"] is True


def test_ledger_tamper_is_rejected_without_pointer_advance(
    tmp_path: Path, spec
) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    engine = Autoscaler(store, spec)
    engine.run(rounds=1)
    state, snapshot_id = store.load()
    state["nodes"]["gpu-a1"]["gpu_remaining_mib"][0] -= 1
    with pytest.raises(BusinessError) as exc:
        store.commit(state, expected_snapshot_id=snapshot_id, reason="tamper")
    assert exc.value.code == "E_LEDGER_MISMATCH"
    assert store.load()[1] == snapshot_id
