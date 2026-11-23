from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from scaler.engine import Autoscaler
from scaler.errors import BusinessError
from scaler.models import ScalerSpec, canonical_json
from scaler.storage import SQLiteStore


def test_duplicate_service_and_node_are_rejected(base_spec: ScalerSpec) -> None:
    raw = base_spec.to_dict()
    raw["services"].append(copy.deepcopy(raw["services"][0]))
    with pytest.raises(BusinessError) as exc:
        ScalerSpec.from_dict(raw)
    assert exc.value.code == "E_DUPLICATE_SERVICE"

    raw = base_spec.to_dict()
    raw["nodes"].append(copy.deepcopy(raw["nodes"][0]))
    with pytest.raises(BusinessError) as exc:
        ScalerSpec.from_dict(raw)
    assert exc.value.code == "E_DUPLICATE_NODE"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("replica_memory_mib", 0),
        ("replica_memory_mib", -1),
        ("replica_compute_units", 0),
        ("min_replicas", 6),
        ("max_replicas", 0),
        ("cold_start_ms", -1),
        ("batch_window_ms", 0),
        ("tokens_per_second_per_compute_unit", 0),
    ],
)
def test_service_validation(base_spec: ScalerSpec, field: str, value: int) -> None:
    raw = base_spec.to_dict()
    raw["services"][0][field] = value
    with pytest.raises(BusinessError) as exc:
        ScalerSpec.from_dict(raw)
    assert exc.value.code == "E_INPUT_INVALID"


def test_invalid_trace_and_backoff(base_spec: ScalerSpec) -> None:
    raw = base_spec.to_dict()
    first = raw["trace"]["services"]["chat-alpha"][0]
    raw["trace"]["services"]["chat-alpha"].append(copy.deepcopy(first))
    with pytest.raises(BusinessError) as exc:
        ScalerSpec.from_dict(raw)
    assert exc.value.code == "E_DUPLICATE_WINDOW_TIME"

    raw = base_spec.to_dict()
    raw["trace"]["services"]["chat-alpha"][0]["arrival_rate"] = -1
    with pytest.raises(BusinessError) as exc:
        ScalerSpec.from_dict(raw)
    assert exc.value.code == "E_INPUT_INVALID"

    raw = base_spec.to_dict()
    raw["config"]["backoff_base_ms"] = 5000
    raw["config"]["backoff_max_ms"] = 100
    with pytest.raises(BusinessError) as exc:
        ScalerSpec.from_dict(raw)
    assert exc.value.code == "E_INPUT_INVALID"


def test_non_loopback_launch_target_is_rejected(base_spec: ScalerSpec) -> None:
    raw = base_spec.to_dict()
    raw["config"]["launch_target_url"] = "http://example.com:9000"
    with pytest.raises(BusinessError) as exc:
        ScalerSpec.from_dict(raw)
    assert exc.value.code == "E_INPUT_INVALID"


def test_config_version_regression_leaves_state_unchanged(
    tmp_path: Path, spec: ScalerSpec
) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    engine = Autoscaler(store, spec)
    engine.run(rounds=1)
    before, before_snapshot = store.load()
    before_json = canonical_json(before)

    lower = replace(spec, config=replace(spec.config, config_version=0))
    lower_engine = Autoscaler(store, lower)
    with pytest.raises(BusinessError) as exc:
        lower_engine.run_round(clock_ms=2000)
    assert exc.value.code == "E_CONFIG_VERSION_REGRESSION"
    after, after_snapshot = store.load()
    assert after_snapshot == before_snapshot
    assert canonical_json(after) == before_json

    conflict = replace(
        spec,
        config=replace(spec.config, round_budget_ms=spec.config.round_budget_ms + 1),
    )
    conflict_engine = Autoscaler(store, conflict)
    with pytest.raises(BusinessError) as exc:
        conflict_engine.run_round(clock_ms=2000)
    assert exc.value.code == "E_CONFIG_VERSION_CONFLICT"
    assert canonical_json(store.load()[0]) == before_json


def test_clock_regression_and_missing_observation_do_not_commit(
    tmp_path: Path, spec: ScalerSpec
) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    engine = Autoscaler(store, spec)
    engine.run(rounds=1)
    before, before_snapshot = store.load()
    before_json = canonical_json(before)

    with pytest.raises(BusinessError) as exc:
        engine.run_round(clock_ms=500)
    assert exc.value.code == "E_CLOCK_REGRESSION"
    assert exc.value.details["rollback_ms"] == 500
    assert store.load()[1] == before_snapshot
    assert canonical_json(store.load()[0]) == before_json

    fresh_store = SQLiteStore(tmp_path / "fresh.db")
    fresh_engine = Autoscaler(fresh_store, spec)
    with pytest.raises(BusinessError) as exc:
        fresh_engine.run_round(clock_ms=0)
    assert exc.value.code == "E_NO_OBSERVATION"
    assert not fresh_store.load()[0]["replicas"]


def test_clock_tolerance_clamps_without_regression(
    tmp_path: Path, spec: ScalerSpec
) -> None:
    tolerant = replace(
        spec,
        config=replace(spec.config, clock_regression_tolerance_ms=500),
    )
    store = SQLiteStore(tmp_path / "state.db")
    engine = Autoscaler(store, tolerant)
    engine.run(rounds=1)
    result = engine.run_round(clock_ms=600)
    state = store.load()[0]
    assert state["logical_clock_ms"] == 1000
    round_data = result["round"]
    assert round_data["clock_adjustment"]["reason"] == (
        "CLOCK_REGRESSION_WITHIN_TOLERANCE_CLAMPED"
    )
