import copy

import pytest

from opchain.cache import ContentCache
from opchain.config import Config
from opchain.engine import execute_case, run_batch, run_query
from opchain.errors import ErrorCode, OpChainError
from opchain.samplegen import VALID_CASES
from opchain.task import parse_spec


def test_batch_adjudication_passes_and_records_profile(specs, config):
    envelope, cache, _ = run_batch(specs, config)
    assert envelope["version"] == 1
    assert len(envelope["payload"]["results"]) == len(specs)
    for result in envelope["payload"]["results"]:
        assert result["status"] == ErrorCode.OK
        assert result["adjudication"]["final_report"]["violation_count"] == 0
        assert result["tolerance"]["precision"] == result["precision"]
        assert result["tolerance"]["atol"] > 0
    assert cache.size() == sum(len(spec.op_chain) for spec in specs)


def test_rerun_is_byte_identical(specs, config):
    first, _, _ = execute_case(specs[0], config, cache=ContentCache())
    second, _, _ = execute_case(specs[0], config, cache=ContentCache())
    assert first["final_hash"] == second["final_hash"]
    assert first["stages"] == second["stages"]
    assert first["input_record"] == second["input_record"]


def test_query_matches_batch(specs, config):
    envelope, _, _ = run_batch(specs, config)
    result, _, _ = run_query(specs[0], config, envelope, cache=ContentCache())
    assert result["query_consistency"]["checks"]["final_hash_equal"] is True
    assert all(
        check["content_hash_equal"]
        for check in result["query_consistency"]["stage_checks"]
    )


def test_latency_budget_breach_observed_by_timer():
    record = copy.deepcopy(VALID_CASES[0])
    record["policy"] = {"latency_budget_ms": 1e-12}
    spec = parse_spec(record)
    with pytest.raises(OpChainError) as exc:
        execute_case(spec, Config(), cache=ContentCache(), latency_budget_ms=1e-12)
    assert exc.value.code == ErrorCode.LATENCY_BUDGET
    assert exc.value.details["observed_elapsed_ms"] >= 0.0
    assert "processed_blocks" in exc.value.details


def test_inconsistent_query_is_flagged(specs, config):
    envelope, _, _ = run_batch(specs, config)
    tampered = copy.deepcopy(specs[0])
    tampered.seed = tampered.seed + 1
    with pytest.raises(OpChainError) as exc:
        run_query(tampered, config, envelope, cache=ContentCache())
    assert exc.value.code == ErrorCode.INCONSISTENT


def test_float32_case_uses_float32_profile(specs, config):
    f32 = next(spec for spec in specs if spec.dtype == "float32")
    result, _, _ = execute_case(f32, config, cache=ContentCache())
    assert result["precision"] == "float32"
    assert result["tolerance"]["atol"] == 1e-6
    assert result["adjudication"]["final_report"]["violation_count"] == 0


def test_estimate_records_element_op_transpose_counts(specs, config):
    result, _, _ = execute_case(specs[1], config, cache=ContentCache())
    estimate = result["execution_plan"]["estimate"]
    assert estimate["element_count"] == 4 * 6 * 5
    assert estimate["op_count"] == len(specs[1].op_chain)
    assert estimate["transpose_count"] == 2
    assert estimate["estimated_latency_ms"] > 0
