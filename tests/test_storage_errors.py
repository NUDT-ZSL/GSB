from __future__ import annotations

from pathlib import Path

import pytest

from stream_reopt.data import generate_batch
from stream_reopt.engine import RuntimeEngine
from stream_reopt.errors import BusinessError, ErrorCode
from stream_reopt.join import execute_batch
from stream_reopt.models import ExecutionShape, load_scenario
from stream_reopt.reopt import assess_drift


def test_version_noop_append_and_regression(tmp_path: Path) -> None:
    scenario = load_scenario("samples/adaptive_join_sample.json")
    engine = RuntimeEngine(
        scenario,
        db_path=tmp_path / "state.db",
        blocks_path=tmp_path / "blocks",
    )
    try:
        run_id, _ = engine.initialize()
        current = scenario.initial_shape
        assert engine.store.append_shadow_shape(run_id, current)["action"] == "NOOP"
        shadow = ExecutionShape(
            version=2,
            join_order=("customers", "orders", "lineitem"),
            algorithms=("hash", "hash"),
            reason="shadow",
        )
        appended = engine.store.append_shadow_shape(run_id, shadow)
        assert appended["action"] == "INCREMENTAL_APPEND"
        assert engine.store.get_state(run_id)["current_version"] == 1
        with pytest.raises(BusinessError) as error:
            engine.store.append_shadow_shape(
                run_id,
                ExecutionShape(
                    version=1,
                    join_order=("orders", "customers", "lineitem"),
                    algorithms=("hash", "hash"),
                ),
            )
        assert error.value.code == ErrorCode.FORM_VERSION_REGRESSION
    finally:
        engine.close()


def test_duplicate_commit_idempotent_and_digest_conflict(tmp_path: Path) -> None:
    scenario = load_scenario("samples/adaptive_join_sample.json")
    engine = RuntimeEngine(
        scenario,
        db_path=tmp_path / "state.db",
        blocks_path=tmp_path / "blocks",
    )
    try:
        run_id, _ = engine.initialize()
        batch = scenario.plan.batches[0]
        tables = generate_batch(scenario, batch)
        execution = execute_batch(scenario, batch, tables, scenario.initial_shape)
        engine.store.commit_batch(
            run_id=run_id,
            result=execution,
            observation_records=[],
            assessment={"triggered": False, "drift_increment": 0.0},
            decision=None,
            expected_seq=0,
            expected_shape_version=1,
            commit_digest="commit-one",
            block_path=None,
        )
        duplicate = engine.store.commit_batch(
            run_id=run_id,
            result=execution,
            observation_records=[],
            assessment={"triggered": False, "drift_increment": 0.0},
            decision=None,
            expected_seq=0,
            expected_shape_version=1,
            commit_digest="commit-one",
            block_path=None,
        )
        assert duplicate["duplicate"] is True
        assert len(engine.store.list_observations(run_id)) == 0
        with pytest.raises(BusinessError) as error:
            engine.store.commit_batch(
                run_id=run_id,
                result=execution,
                observation_records=[],
                assessment={"triggered": False, "drift_increment": 0.0},
                decision=None,
                expected_seq=0,
                expected_shape_version=1,
                commit_digest="commit-two",
                block_path=None,
            )
        assert error.value.code == ErrorCode.BATCH_DIGEST_MISMATCH
    finally:
        engine.close()


def test_empty_observation_and_watermark_errors(sample_dict: dict, tmp_path: Path) -> None:
    scenario = load_scenario("samples/adaptive_join_sample.json")
    batch = scenario.plan.batches[0]
    with pytest.raises(BusinessError) as empty:
        assess_drift(
            scenario,
            current_records=[],
            history_records=[],
            current_batch=batch,
            last_decision_ms=None,
        )
    assert empty.value.code == ErrorCode.EMPTY_OBSERVATION
    engine = RuntimeEngine(
        scenario,
        db_path=tmp_path / "state.db",
        blocks_path=tmp_path / "blocks",
    )
    try:
        run_id, _ = engine.initialize()
        tables = generate_batch(scenario, batch)
        execution = execute_batch(scenario, batch, tables, scenario.initial_shape)
        with pytest.raises(BusinessError) as watermark:
            engine.store.commit_batch(
                run_id=run_id,
                result=execution,
                observation_records=[],
                assessment={"triggered": False, "drift_increment": 0.0},
                decision=None,
                expected_seq=4,
                expected_shape_version=1,
                commit_digest="bad-watermark",
                block_path=None,
            )
        assert watermark.value.code == ErrorCode.WATERMARK_MISMATCH
        assert engine.store.list_batches(run_id) == []
    finally:
        engine.close()

