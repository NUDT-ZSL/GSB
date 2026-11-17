from __future__ import annotations

from slpguard import JudgeConfig, ResourceLimits, analyze_path, judge, parse_file

from .conftest import violation_codes


LIMITS = ResourceLimits(max_steps_per_session=1000, max_file_seconds=15.0)


def test_cross_session_sequence_mixup_lists_conflict(corpus) -> None:
    result = analyze_path(corpus / "mixup_cross_session.slp", limits=LIMITS)
    assert violation_codes(result) == {"E_MIXUP"}
    assert result.truth.all_consistent
    mixups = [
        violation
        for session in result.sessions
        for violation in session.violations
        if violation.code.name == "E_MIXUP"
    ]
    assert len(mixups) == 2
    assert all(violation.sequences == (202,) for violation in mixups)
    assert {session.session_id for session in result.sessions} == {1, 2}


def test_direction_conflict(corpus) -> None:
    result = analyze_path(corpus / "mixup_direction.slp", limits=LIMITS)
    assert violation_codes(result) == {"E_MIXUP"}
    assert result.truth.all_consistent
    violation = result.sessions[0].violations[0]
    assert violation.sequences == (4,)
    assert violation.sync_ids == (1003,)


def test_renegotiation_missing_base_or_declaration(corpus) -> None:
    no_base = analyze_path(corpus / "reneg_without_base.slp", limits=LIMITS)
    assert violation_codes(no_base) == {"E_MIXUP"}
    assert no_base.truth.all_consistent

    missing = analyze_path(corpus / "reneg_missing_declaration.slp", limits=LIMITS)
    assert violation_codes(missing) == {"E_MIXUP", "E_TRANSITION"}
    assert missing.truth.all_consistent


def test_storm_is_per_session_and_limit_change_does_not_leak(corpus) -> None:
    parsed = parse_file(corpus / "reneg_storm.slp")

    default = judge(parsed, JudgeConfig(max_renegotiations=3))
    assert [session.session_id for session in default] == [15, 16]
    storm = [violation for violation in default[0].violations if violation.code.name == "E_STORM"]
    assert len(storm) == 1
    assert storm[0].sequences == (11, 13)
    assert default[1].violations == []

    strict = judge(parsed, JudgeConfig(max_renegotiations=0))
    assert strict[0].renegotiations == 5
    assert any(violation.code.name == "E_STORM" for violation in strict[0].violations)
    assert strict[1].violations == []

    loose = judge(parsed, JudgeConfig(max_renegotiations=10))
    assert not any(violation.code.name == "E_STORM" for violation in loose[0].violations)
    assert loose[1].violations == []
