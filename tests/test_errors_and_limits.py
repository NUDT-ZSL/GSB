from __future__ import annotations

import pytest

from mtstream.errors import (
    E_ALIGN_INVALID,
    E_CHUNK_MISMATCH,
    E_CONFIG_INVALID,
    E_DUP_CASE,
    E_INCONSISTENT,
    E_LATENCY_BUDGET,
    E_RESOURCE_LIMIT,
    E_STAGE_BUSY,
    E_TOKEN_UNKNOWN,
    MTStreamError,
)
from mtstream.models import DecodeConfig
from mtstream.sample_data import make_sample_case
from mtstream.session import StreamingSession
from mtstream.validation import CaseRegistry


def test_duplicate_case_id(case):
    registry = CaseRegistry()
    registry.add(case)
    with pytest.raises(MTStreamError) as caught:
        registry.add(case)
    assert caught.value.code == E_DUP_CASE
    assert caught.value.field == "case_id"


def test_unknown_source_token(case, config):
    broken = case.__class__(
        case_id=case.case_id,
        seed=case.seed,
        source_tokens=("not-in-vocab",) + case.source_tokens[1:],
        source_vocab=case.source_vocab,
        target_vocab=case.target_vocab,
        pair_vocab=case.pair_vocab,
        alignments=case.alignments,
        source_version=case.source_version,
        align_version=case.align_version,
    )
    with pytest.raises(MTStreamError) as caught:
        StreamingSession(broken, config)
    assert caught.value.code == E_TOKEN_UNKNOWN
    assert caught.value.field == "source_tokens[0]"


def test_alignment_target_token_unknown(case, config):
    first = case.alignments[0]
    broken_alignment = first.__class__(
        source_index=first.source_index,
        target_index=first.target_index,
        target_token="missing-target",
        weight=first.weight,
    )
    broken = case.__class__(
        case_id=case.case_id,
        seed=case.seed,
        source_tokens=case.source_tokens,
        source_vocab=case.source_vocab,
        target_vocab=case.target_vocab,
        pair_vocab=case.pair_vocab,
        alignments=(broken_alignment,) + case.alignments[1:],
        source_version=case.source_version,
        align_version=case.align_version,
    )
    with pytest.raises(MTStreamError) as caught:
        StreamingSession(broken, config)
    assert caught.value.code == E_ALIGN_INVALID
    assert caught.value.field == "alignments[0].target_token"


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"block_size": 0}, "block_size"),
        ({"context_window": 0}, "context_window"),
        ({"context_window": 65}, "context_window"),
    ],
)
def test_invalid_config_fields(case, changes, field):
    config = DecodeConfig(**changes)
    with pytest.raises(MTStreamError) as caught:
        StreamingSession(case, config)
    assert caught.value.code == E_CONFIG_INVALID
    assert caught.value.field == field


def test_first_block_latency_budget_reports_real_work(case):
    config = DecodeConfig(block_size=4, latency_budget_ms=0.000001)
    session = StreamingSession(case, config)
    with pytest.raises(MTStreamError) as caught:
        session.request_block(0)
    error = caught.value
    assert error.code == E_LATENCY_BUDGET
    assert error.data["completed_stage"] == "chunk_step_loop"
    assert error.data["decoded_steps"] == 4
    assert error.data["observed_ns"] > error.data["budget_ns"]
    assert error.data["candidate_score_count"] == 4 * len(case.target_vocab)
    assert session.get_ready_blocks()["ready_block_count"] == 0


def test_chunk_mismatch_reports_first_deviation(case, config):
    session = StreamingSession(case, config)
    session.enable_diagnostic_drift(0, 2, field="confidence", delta=0.02)
    with pytest.raises(MTStreamError) as caught:
        session.request_block(0)
    error = caught.value
    assert error.code == E_CHUNK_MISMATCH
    assert error.data["block_index"] == 0
    assert error.data["first_deviation_step"] == 2
    assert error.data["deviation_field"] == "confidence"
    assert error.field == "confidence"


def test_inconsistent_query_points_to_block_and_category(case, config):
    session = StreamingSession(case, config)
    session.request_block(0)
    session.mutate_ready_block_for_diagnostic(0, field="projection")
    with pytest.raises(MTStreamError) as caught:
        session.query_block(0)
    error = caught.value
    assert error.code == E_INCONSISTENT
    assert error.data["block_index"] == 0
    assert error.data["category"] == "projection"


def test_query_rejects_changed_version_before_comparison(case, config):
    session = StreamingSession(case, config)
    session.request_block(0)
    session.case = session.case.with_source_version(2)
    with pytest.raises(MTStreamError) as caught:
        session.query_block(0)
    error = caught.value
    assert error.code == E_INCONSISTENT
    assert error.data["block_index"] == 0
    assert error.data["category"] == "source_version"


def test_stage_busy_is_immediate(case, config):
    session = StreamingSession(case, config)
    assert session.stage.try_acquire("test-owner")
    try:
        with pytest.raises(MTStreamError) as caught:
            session.submit_plan()
    finally:
        session.stage.release("test-owner")
    assert caught.value.code == E_STAGE_BUSY
    assert caught.value.data["waited"] is False


def test_different_active_plan_is_stage_busy(case, config):
    session = StreamingSession(case, config)
    first = session.submit_plan()
    different = session._build_plan(config.block_size + 1)
    with pytest.raises(MTStreamError) as caught:
        session.submit_plan(different)
    assert caught.value.code == E_STAGE_BUSY
    assert caught.value.data["active_plan_id"] == first.plan_id
    assert caught.value.data["attempted_plan_id"] == different.plan_id


def test_resource_limits_and_block_memory_degradation(case):
    too_many_sources = DecodeConfig(max_source_tokens=4)
    with pytest.raises(MTStreamError) as caught:
        StreamingSession(case, too_many_sources)
    assert caught.value.code == E_RESOURCE_LIMIT
    assert caught.value.field == "source_tokens"

    too_few_cache_entries = DecodeConfig(max_cache_entries=3)
    with pytest.raises(MTStreamError) as caught:
        StreamingSession(case, too_few_cache_entries)
    assert caught.value.code == E_RESOURCE_LIMIT
    assert caught.value.field == "prefix_cache_entries"

    degraded_config = DecodeConfig(
        block_size=4,
        max_block_memory_bytes=6_000,
        latency_budget_ms=1000.0,
    )
    session = StreamingSession(case, degraded_config)
    assert session.plan.degraded is True
    assert session.plan.degradation_category == "block_memory"
    assert session.plan.block_size_effective < session.plan.block_size_requested
    assert (
        session.plan.block_size_effective
        * session._block_memory_per_step()
        <= degraded_config.max_block_memory_bytes
    )
    overlapping = [
        session.request_block(index)
        for index in range(len(session.plan.boundaries))
    ]
    baseline_config = DecodeConfig(
        block_size=4, latency_budget_ms=1000.0
    )
    baseline = StreamingSession(case, baseline_config)
    baseline_blocks = [
        baseline.request_block(index)
        for index in range(len(baseline.plan.boundaries))
    ]
    assert tuple(
        token for block in overlapping for token in block.tokens
    ) == tuple(token for block in baseline_blocks for token in block.tokens)
    assert tuple(
        value for block in overlapping for value in block.confidences
    ) == tuple(
        value for block in baseline_blocks for value in block.confidences
    )
    assert tuple(
        value for block in overlapping for value in block.projections
    ) == tuple(
        value for block in baseline_blocks for value in block.projections
    )


def test_unknown_case_with_different_case_id_is_not_duplicate(case, config):
    registry = CaseRegistry()
    registry.add(case)
    another = make_sample_case(case.seed + 1)
    registry.add(another)
    assert set(registry.snapshot()) == {case.case_id, another.case_id}

