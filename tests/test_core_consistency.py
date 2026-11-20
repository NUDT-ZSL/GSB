from __future__ import annotations

import json

import pytest

from mtstream.core import decode_whole, encode_source, rank_candidates
from mtstream.models import CandidateRecord, DecodeConfig, DecoderState
from mtstream.session import StreamingSession
from mtstream.util import canonical_json


def _semantic_payload(result):
    payload = result.to_dict(include_candidates=True)
    payload.pop("timing", None)
    return canonical_json(payload)


def test_encoding_and_whole_decode_are_byte_stable(case, config):
    first_encoding = encode_source(case, config)
    second_encoding = encode_source(case, config)
    assert first_encoding == second_encoding
    assert first_encoding.encoding_digest == second_encoding.encoding_digest
    assert DecoderState.initial(config.embedding_dim).state_hash == (
        DecoderState.initial(config.embedding_dim).state_hash
    )

    first = decode_whole(case, config, first_encoding)
    second = decode_whole(case, config, second_encoding)
    assert _semantic_payload(first) == _semantic_payload(second)
    assert first.semantic_hash == second.semantic_hash
    assert first.intermediate_digest == second.intermediate_digest


@pytest.mark.parametrize("block_size", [1, 2, 3, 4, 6, 20])
def test_chunked_and_whole_decode_match_for_block_size_matrix(case, block_size):
    config = DecodeConfig(
        block_size=block_size,
        context_window=3,
        latency_budget_ms=1000.0,
    )
    session = StreamingSession(case, config)
    blocks = [
        session.request_block(index)
        for index in range(len(session.plan.boundaries))
    ]
    full = session.full_decode()
    chunk_tokens = tuple(token for block in blocks for token in block.tokens)
    chunk_confidences = tuple(
        confidence for block in blocks for confidence in block.confidences
    )
    chunk_projections = tuple(
        projection for block in blocks for projection in block.projections
    )
    assert chunk_tokens == full.tokens
    assert chunk_confidences == full.confidences
    assert chunk_projections == full.projections
    assert tuple(
        record for block in blocks for record in block.step_records
    ) == full.step_records
    assert blocks[0].timing.observed_ns > 0
    assert full.timing.observed_ns > 0
    assert blocks[0].timing.stage == "chunk_first_block"
    assert full.timing.stage == "whole_decode"
    assert len(session.cache_entries()) == case.target_length + 1
    assert all(
        block.timing.candidate_score_count
        == len(block.tokens) * len(case.target_vocab)
        for block in blocks
    )


def test_step_candidates_and_confidence_are_queryable(case, config):
    session = StreamingSession(case, config)
    first = session.request_block(0)
    payload = session.query_candidates(0)
    assert payload["block_index"] == 0
    assert len(payload["steps"]) == len(first.tokens)
    for record in payload["steps"]:
        candidates = record["candidates"]
        assert len(candidates) == len(case.target_vocab)
        assert candidates[0]["rank"] == 1
        assert candidates[0]["token"] == record["token"]
        assert record["confidence"] == pytest.approx(
            candidates[0]["probability"], abs=1e-12
        )
        ordered = sorted(
            candidates,
            key=lambda item: (-item["score"], item["token"]),
        )
        assert [item["token"] for item in candidates] == [
            item["token"] for item in ordered
        ]


def test_ready_block_query_matches_whole_slice(case, config):
    session = StreamingSession(case, config)
    block = session.request_block(0)
    full = session.full_decode()
    queried = session.query_block(0)
    assert queried["tokens"] == list(
        full.tokens[block.start_step : block.end_step_exclusive]
    )
    assert queried["confidences"] == [
        round(value, 12)
        for value in full.confidences[
            block.start_step : block.end_step_exclusive
        ]
    ]
    assert queried["projections"] == [
        [round(v, 12) for v in projection]
        for projection in full.projections[
            block.start_step : block.end_step_exclusive
        ]
    ]


def test_equal_scores_use_target_token_lexical_tie_break():
    def candidate(token):
        return CandidateRecord(
            token=token,
            score=1.25,
            probability=0.5,
            rank=0,
            has_current_alignment=True,
            aligned_source_indices=(0,),
            attention_weights=(1.0,),
        )

    ranked = rank_candidates(
        [candidate("tgt_z"), candidate("tgt_a"), candidate("tgt_m")]
    )
    assert [item.token for item in ranked] == [
        "tgt_a",
        "tgt_m",
        "tgt_z",
    ]
    assert [item.rank for item in ranked] == [1, 2, 3]

