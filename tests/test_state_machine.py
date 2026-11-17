from __future__ import annotations

from pathlib import Path

from slpguard import ResourceLimits, analyze_bytes, analyze_path, judge, parse_file
from slpguard.protocol import SLPRecord
from slpguard.samples import RecordBuilder

from .conftest import violation_codes


LIMITS = ResourceLimits(max_steps_per_session=1000, max_file_seconds=15.0)


def _session_violation_signature(records: list[SLPRecord], physical_order: list[int]):
    data = __import__("slpguard").encode_file([records[index] for index in physical_order])
    result = analyze_bytes(data, limits=LIMITS)
    return [
        (
            session.session_id,
            [
                (
                    violation.code.name,
                    violation.sequences,
                    violation.sync_ids,
                )
                for violation in session.violations
            ],
        )
        for session in result.sessions
    ]


def test_physical_order_does_not_change_business_verdict(corpus) -> None:
    normal = analyze_path(corpus / "valid_slp.slp", limits=LIMITS)
    reordered = analyze_path(corpus / "physical_reorder.slp", limits=LIMITS)
    assert violation_codes(normal) == set()
    assert violation_codes(reordered) == set()
    assert normal.truth.all_consistent
    assert reordered.truth.all_consistent

    builder = RecordBuilder()
    records = builder.session(42, 1)
    normal_order = list(range(5))
    shuffled = [2, 0, 4, 1, 3]
    assert _session_violation_signature(records, normal_order) == _session_violation_signature(
        records, shuffled
    )


def test_duplicate_and_logical_reorder(corpus) -> None:
    duplicate = analyze_path(corpus / "duplicate.slp", limits=LIMITS)
    assert violation_codes(duplicate) == {"E_TRANSITION"}
    assert duplicate.truth.all_consistent
    assert any(
        violation.sequences == (4,)
        for session in duplicate.sessions
        for violation in session.violations
    )

    logical = analyze_path(corpus / "logical_reorder.slp", limits=LIMITS)
    assert violation_codes(logical) == {"E_TRANSITION"}
    assert logical.truth.all_consistent
    assert logical.truth.matched == 3


def test_stage_jump_auth_before_data_and_incomplete(corpus) -> None:
    jump = analyze_path(corpus / "stage_jump.slp", limits=LIMITS)
    assert violation_codes(jump) == {"E_TRANSITION", "E_INCOMPLETE"}
    assert jump.truth.all_consistent

    early = analyze_path(corpus / "auth_before_data.slp", limits=LIMITS)
    assert violation_codes(early) == {"E_TRANSITION", "E_INCOMPLETE"}
    assert early.truth.all_consistent
    transition = next(
        violation
        for session in early.sessions
        for violation in session.violations
        if violation.code.name == "E_TRANSITION"
    )
    assert transition.sequences == (4,)

    incomplete = analyze_path(corpus / "incomplete.slp", limits=LIMITS)
    assert violation_codes(incomplete) == {"E_INCOMPLETE"}
    assert incomplete.truth.all_consistent


def test_packet_after_close_uses_after_close_not_transition(corpus) -> None:
    result = analyze_path(corpus / "half_close_violation.slp", limits=LIMITS)
    assert violation_codes(result) == {"E_AFTER_CLOSE"}
    assert result.truth.all_consistent
    violation = result.sessions[0].violations[0]
    assert violation.sequences == (6,)
    assert violation.code.name == "E_AFTER_CLOSE"


def test_sessions_and_violations_are_stably_sorted(corpus) -> None:
    result = analyze_path(corpus / "reneg_storm.slp", limits=LIMITS)
    assert [session.session_id for session in result.sessions] == [15, 16]
    for session in result.sessions:
        keys = [
            (violation.min_sequence, violation.code.value, violation.sync_ids)
            for violation in session.violations
        ]
        assert keys == sorted(keys)

    parsed = parse_file(corpus / "stage_jump.slp")
    direct = judge(parsed)
    assert [session.session_id for session in direct] == [10]
