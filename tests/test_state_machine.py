from __future__ import annotations

from pathlib import Path

import pytest

from smugglebench.codes import E_MIX_CONN, E_MSG_GAP, E_RULE_CYCLE
from smugglebench.models import Rule, TransitionConstraint
from smugglebench.normalize import load_workspace
from smugglebench.rules import RuleEngine, detect_rule_cycles, merge_rule_view
from smugglebench.state_machine import (
    build_connection_results,
    local_interpretation,
    parse_mutated_request,
    reassemble_connection,
)
from smugglebench.mutations import apply_mutation
from smugglebench.util import BusinessError


def _engine(sample_workspace: Path) -> tuple[RuleEngine, object]:
    data = load_workspace(sample_workspace)
    active, _audit = merge_rule_view(data.rules, data.observed_clock_ms)
    return RuleEngine(active.values(), audit_rules=data.rules), data


def test_out_of_order_reassembly_and_pipelined_boundary(
    sample_workspace: Path,
) -> None:
    engine, data = _engine(sample_workspace)
    results = build_connection_results(data.flows, rule_engine=engine)
    normal = results["conn-normal"]
    assert normal.completed
    assert len(normal.messages) == 1
    assert normal.messages[0].start_line.startswith("POST /echo")
    assert normal.messages[0].body == b"hello"
    assert normal.messages[0].flow_seq_start == 1
    assert normal.messages[0].flow_seq_end == 2

    spec = next(item for item in data.mutations if item.mutation_id == "m05-boundary-injection")
    template = results[spec.connection_id].messages[0]
    raw = apply_mutation(template, spec)
    messages, transitions, _hits, _suppressed, _candidates, _errors = parse_mutated_request(
        raw,
        connection_id=spec.connection_id,
        rule_engine=engine,
    )
    assert len(messages) == 2
    assert messages[1].target == "/smuggled"
    assert any(item.event == "MESSAGE_END" for item in transitions)


def test_gap_and_duplicate_and_mix_reports(sample_workspace: Path) -> None:
    engine, data = _engine(sample_workspace)
    results = build_connection_results(data.flows, rule_engine=engine)
    gap = results["conn-gap"]
    assert not gap.completed
    assert gap.errors[0]["code"] == E_MSG_GAP
    assert gap.errors[0]["missing_ranges"] == [[2, 2]]

    duplicate = results["conn-dupdelivery"]
    assert duplicate.completed
    assert duplicate.duplicates[0]["seq"] == 1
    assert duplicate.duplicates[0]["duplicate_count"] == 1
    assert duplicate.duplicates[0]["truth"] == "FIRST_OCCURRENCE"

    mixed = results["conn-mix"]
    assert not mixed.completed
    assert mixed.errors[0]["code"] == E_MIX_CONN
    assert mixed.errors[0]["payload_connection_id"] == "another-connection"


def test_http_states_cover_chunk_transfer_and_trailers(
    sample_workspace: Path,
) -> None:
    engine, data = _engine(sample_workspace)
    results = build_connection_results(data.flows, rule_engine=engine)
    trailer = results["conn-trailer"].messages[0]
    states = {item.state for item in trailer.state_trace}
    events = {item.event for item in trailer.state_trace}
    assert "START_LINE" in states
    assert "HEADERS" in states
    assert "CHUNK_SIZE" in states
    assert "CHUNK_DATA" in states
    assert "TRAILERS" in states
    assert "MESSAGE_END" in events
    assert trailer.trailers == (("X-Source", "sample"),)


def test_rule_hit_carries_state_offset_and_generation(
    sample_workspace: Path,
) -> None:
    engine, data = _engine(sample_workspace)
    results = build_connection_results(data.flows, rule_engine=engine)
    spec = next(item for item in data.mutations if item.mutation_id == "m01-te-cl-conflict")
    template = results[spec.connection_id].messages[0]
    raw = apply_mutation(template, spec)
    messages, _transitions, hits, _suppressed, candidates, _errors = parse_mutated_request(
        raw,
        connection_id=spec.connection_id,
        rule_engine=engine,
    )
    assert messages
    assert any(hit.rule_key == "te-cl-observation" for hit in hits)
    hit = next(hit for hit in hits if hit.rule_key == "te-cl-observation")
    assert hit.generation == 2
    assert hit.state == "HEADERS"
    assert hit.event == "HEADERS_DONE"
    assert hit.byte_offset > 0
    assert "te-cl-observation" in candidates


def test_conflicting_actions_are_stably_suppressed() -> None:
    transition_flag = TransitionConstraint(
        from_states=("HEADERS",),
        event="HEADERS_DONE",
        conditions={"header_names_all": ["host"]},
        action="FLAG",
        code="FLAG",
    )
    transition_reject = TransitionConstraint(
        from_states=("HEADERS",),
        event="HEADERS_DONE",
        conditions={"header_names_all": ["host"]},
        action="REJECT",
        code="REJECT",
    )
    low = Rule(
        canonical_name="a-low",
        display_name="a-low",
        generation=1,
        effective_from_ms=0,
        effective_to_ms=None,
        enabled=True,
        tombstone=False,
        transitions=(transition_flag,),
        source_kind="v2",
        source_file="<test>",
        record_index=0,
    )
    high = Rule(
        canonical_name="z-high",
        display_name="z-high",
        generation=2,
        effective_from_ms=0,
        effective_to_ms=None,
        enabled=True,
        tombstone=False,
        transitions=(transition_reject,),
        source_kind="v2",
        source_file="<test>",
        record_index=0,
    )
    engine = RuleEngine([low, high])
    evaluation = engine.evaluate(
        state="HEADERS",
        event="HEADERS_DONE",
        byte_offset=10,
        context={"header_names": {"host"}},
    )
    assert [hit.rule_key for hit in evaluation.hits] == ["z-high"]
    assert evaluation.suppressed[0]["rule_key"] == "a-low"
    assert evaluation.suppressed[0]["suppressed_by"] == "z-high"


def test_rule_dependency_cycle_is_reported() -> None:
    transitions = (
        TransitionConstraint(
            from_states=("HEADERS",),
            event="HEADERS_DONE",
            conditions={},
            action="FLAG",
            code="X",
        ),
    )
    a = Rule(
        "a",
        "a",
        1,
        0,
        None,
        True,
        False,
        transitions,
        "v2",
        "<test>",
        0,
        ("b",),
    )
    b = Rule(
        "b",
        "b",
        1,
        0,
        None,
        True,
        False,
        transitions,
        "v2",
        "<test>",
        1,
        ("a",),
    )
    with pytest.raises(BusinessError) as raised:
        detect_rule_cycles([a, b])
    assert raised.value.code == E_RULE_CYCLE
    assert set(raised.value.details["rule_names"]) == {"a", "b"}


def test_reassemble_rejects_multiple_connection_ids(
    sample_workspace: Path,
) -> None:
    data = load_workspace(sample_workspace)
    records = [flow for flow in data.flows if flow.connection_id in {"conn-clean", "conn-dup"}]
    streams = reassemble_connection(records)
    assert streams
    assert all(
        stream.errors
        and stream.errors[0]["code"] == E_MIX_CONN
        for stream in streams.values()
    )
