from __future__ import annotations

from mtstream.models import DecodeConfig
from mtstream.sample_data import (
    modification_token_for,
    unaligned_source_indices,
)
from mtstream.session import StreamingSession


def _ready_session(case, config):
    session = StreamingSession(case, config)
    for index in range(len(session.plan.boundaries)):
        session.request_block(index)
    return session


def _output_tuple(session):
    blocks = [
        session.request_block(index)
        for index in range(len(session.plan.boundaries))
    ]
    return (
        tuple(token for block in blocks for token in block.tokens),
        tuple(conf for block in blocks for conf in block.confidences),
        tuple(proj for block in blocks for proj in block.projections),
    )


def _later_aligned_position(case):
    by_source = {}
    for alignment in case.alignments:
        by_source.setdefault(alignment.source_index, []).append(
            alignment.target_index
        )
    candidates = [
        (min(targets), source)
        for source, targets in by_source.items()
        if min(targets) > 0
    ]
    assert candidates
    return min(candidates)[1], min(candidates)[0]


def test_unreferenced_source_edit_reuses_all_entries_and_keeps_output(case, config):
    session = _ready_session(case, config)
    before_hashes = {
        entry.boundary_step: entry.payload_hash
        for entry in session.cache_entries()
    }
    position = unaligned_source_indices(case)[0]
    changed = case.with_source_token(
        position, modification_token_for(case, position)
    )
    report = session.analyze_invalidation(changed)
    assert report.first_affected_step is None
    assert report.affected_steps == ()
    assert report.reused_steps == tuple(range(case.target_length))
    assert len(report.reused_cache_entries) == len(before_hashes)
    assert report.affected_cache_entries == ()
    assert report.to_dict()["set_check"] == {
        "disjoint": True,
        "covers_all_steps": True,
    }

    original_outputs = _output_tuple(
        StreamingSession(case, config)
    )
    changed_outputs = _output_tuple(
        StreamingSession(changed, config)
    )
    assert changed_outputs == original_outputs

    session.apply_case_change(changed)
    after_hashes = {
        entry.boundary_step: entry.payload_hash
        for entry in session.cache_entries()
    }
    assert after_hashes == before_hashes
    replayed = [
        session.request_block(index)
        for index in range(len(session.plan.boundaries))
    ]
    assert all(
        block.timing.stage == "chunk_cache_replay"
        and block.timing.candidate_score_count == 0
        for block in replayed
    )
    assert tuple(
        record for block in replayed for record in block.step_records
    ) == session.full_decode().step_records


def test_aligned_source_edit_reuses_only_unaffected_prefix(case, config):
    session = _ready_session(case, config)
    position, expected_first = _later_aligned_position(case)
    before = {
        entry.boundary_step: entry.payload_hash
        for entry in session.cache_entries()
    }
    changed = case.with_source_token(
        position, modification_token_for(case, position)
    )
    report = session.analyze_invalidation(changed)
    assert report.reason == "source_tokens"
    assert report.first_affected_step == expected_first
    assert report.affected_steps == tuple(
        range(expected_first, case.target_length)
    )
    assert report.reused_steps == tuple(range(expected_first))
    assert report.to_dict()["set_check"] == {
        "disjoint": True,
        "covers_all_steps": True,
    }
    reuse_entries = {
        entry.boundary_step: entry
        for entry in session.cache_entries()
        if entry.key in set(report.reused_cache_entries)
    }
    assert max(reuse_entries) == expected_first
    assert all(boundary <= expected_first for boundary in reuse_entries)
    assert all(
        entry.payload_hash == before[entry.boundary_step]
        for entry in reuse_entries.values()
    )

    session.apply_case_change(changed)
    for boundary, entry in reuse_entries.items():
        current = next(
            item
            for item in session.cache_entries()
            if item.boundary_step == boundary
        )
        assert current.key == entry.key
        assert current.payload_hash == entry.payload_hash
    recomputed = [
        session.request_block(index)
        for index in range(len(session.plan.boundaries))
    ]
    assert sum(
        block.timing.candidate_score_count for block in recomputed
    ) == (
        case.target_length - expected_first
    ) * len(case.target_vocab)
    for block in recomputed:
        if block.end_step_exclusive <= expected_first:
            assert block.timing.stage == "chunk_cache_replay"
            assert block.timing.candidate_score_count == 0
    full = session.full_decode()
    assert full.tokens == tuple(
        token
        for block in recomputed
        for token in block.tokens
    )
    assert full.step_records == tuple(
        record for block in recomputed for record in block.step_records
    )


def test_alignment_version_change_invalidates_everything(case, config):
    session = _ready_session(case, config)
    changed = case.with_align_version(case.align_version + 1)
    report = session.analyze_invalidation(changed)
    assert report.first_affected_step == 0
    assert report.reused_steps == ()
    assert report.affected_steps == tuple(range(case.target_length))
    assert report.reused_cache_entries == ()
    assert len(report.affected_cache_entries) == case.target_length + 1


def test_block_size_change_invalidates_target_cache_but_not_source_encoding(case, config):
    session = _ready_session(case, config)
    new_config = DecodeConfig(
        block_size=config.block_size + 2,
        context_window=config.context_window,
        latency_budget_ms=config.latency_budget_ms,
    )
    report = session.analyze_invalidation(case, new_config)
    assert report.first_affected_step == 0
    assert report.reused_steps == ()
    assert report.affected_steps == tuple(range(case.target_length))
    assert len(report.affected_cache_entries) == case.target_length + 1
    assert session.encoded.encoding_digest == encode_digest(case, config)


def encode_digest(case, config):
    from mtstream.core import encode_source

    return encode_source(case, config).encoding_digest

