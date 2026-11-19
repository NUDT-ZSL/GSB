from __future__ import annotations

import hashlib

import pytest

from shardroute.engine import QueryEngine
from shardroute.errors import BizError
from shardroute.injections import InjectionSet
from shardroute.verify import verify_state

from .conftest import INJECTIONS_PATH


def _engine(state, template_id: str):
    return QueryEngine(
        state,
        1100,
        injections=InjectionSet.load(INJECTIONS_PATH),
    )


def test_route_stage_injection_code_and_context(make_state):
    state = make_state()
    with pytest.raises(BizError) as raised:
        _engine(state, "orders_eq").query("orders_eq")
    error = raised.value
    assert error.code == "E_RULE_VERSION_CONFLICT"
    assert error.stage == "route"
    assert error.query_template == "orders_eq"
    assert error.target_shards == [2]
    assert error.rule_version == "r1"
    assert error.details["scenario_id"] == "route-rule-conflict"


def test_rewrite_stage_injection_code_and_context(make_state):
    state = make_state()
    with pytest.raises(BizError) as raised:
        _engine(state, "orders_range").query("orders_range")
    error = raised.value
    assert error.code == "E_SHARD_KEY_OUT_OF_RANGE"
    assert error.stage == "rewrite"
    assert error.target_shards == [2, 3]
    assert error.details["offending_shard_key"] == 99


def test_execute_stage_total_failure_is_specific(make_state):
    state = make_state()
    loaded = InjectionSet.load(INJECTIONS_PATH)
    only_execute = InjectionSet(
        [scenario for scenario in loaded.scenarios if scenario.stage == "execute"]
    )
    with pytest.raises(BizError) as raised:
        QueryEngine(state, 1100, injections=only_execute).query("orders_eq")
    error = raised.value
    assert error.code == "E_PHYSICAL_TABLE_MISSING"
    assert error.stage == "execute"
    assert error.target_shards == [2]
    outcomes = error.details["shard_outcomes"]
    assert outcomes[0]["status"] == "FAILED"
    assert outcomes[0]["error_code"] == "E_PHYSICAL_TABLE_MISSING"


def test_merge_stage_digest_mismatch(make_state):
    state = make_state()
    with pytest.raises(BizError) as raised:
        _engine(state, "orders_sum_count").query("orders_sum_count")
    error = raised.value
    assert error.code == "E_RESULT_DIGEST_MISMATCH"
    assert error.stage == "merge"
    assert error.details["expected_digest"] != error.details["actual_digest"]
    assert len(error.details["shard_outcomes"]) == 4
    assert not state.path.exists() or verify_state(state)["ok"] is True


def test_injection_does_not_affect_other_queries(state):
    engine = _engine(state, "orders_in")
    result = engine.query("orders_in")
    assert result["overall_status"] == "SUCCESS"
    assert [row["customer_id"] for row in result["rows"]] == [1, 6, 10]


def test_injection_does_not_modify_sample_databases(sample, tmp_path):
    shard_files = sorted((sample.root / "shards").rglob("*.db"))
    before = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in shard_files
    }
    # The core engine never writes to shard DBs; invoke all four injected paths.
    from shardroute.state import StateStore

    state_path = tmp_path / "state.db"
    StateStore.initialize(state_path, sample, 1000)
    scenario_state = StateStore(state_path)
    for template_id in ("orders_eq", "orders_range", "orders_sum_count"):
        try:
            _engine(scenario_state, template_id).query(template_id)
        except BizError:
            pass
    after = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in shard_files
    }
    assert before == after

