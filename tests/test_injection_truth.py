from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from slpguard import (
    InjectionSpec,
    ResourceLimits,
    analyze_bytes,
    analyze_path,
    encode_file,
    inject,
    parse_bytes,
)
from slpguard.protocol import Phase
from slpguard.samples import RecordBuilder


LIMITS = ResourceLimits(max_steps_per_session=1000, max_file_seconds=15.0)


def _codes(result) -> set[str]:
    return {
        violation.code.name
        for session in result.sessions
        for violation in session.violations
    }


def test_stage_jump_and_physical_swap_injection(corpus, tmp_path: Path) -> None:
    base = (corpus / "valid_slp.slp").read_bytes()
    jump_path = tmp_path / "jump.slp"
    inject(base, InjectionSpec(stage_jump_sequence=2), jump_path)
    jump = analyze_path(jump_path, limits=LIMITS)
    assert _codes(jump) == {"E_TRANSITION", "E_INCOMPLETE"}
    assert jump.truth.all_consistent

    swap_path = tmp_path / "swap.slp"
    inject(base, InjectionSpec(physical_swap_positions=(0, 3)), swap_path)
    swapped = analyze_path(swap_path, limits=LIMITS)
    assert _codes(swapped) == set()
    assert swapped.truth.all_consistent


def test_half_close_and_storm_injection(corpus, tmp_path: Path) -> None:
    base = (corpus / "valid_slp.slp").read_bytes()
    half_path = tmp_path / "half.slp"
    inject(
        base,
        InjectionSpec(target_session=1, half_close_append=True),
        half_path,
    )
    half = analyze_path(half_path, limits=LIMITS)
    assert _codes(half) == {"E_AFTER_CLOSE"}
    assert half.truth.all_consistent

    storm_path = tmp_path / "storm.slp"
    inject(
        base,
        InjectionSpec(
            target_session=1,
            renegotiation_count=5,
            max_renegotiations=3,
        ),
        storm_path,
    )
    storm = analyze_path(storm_path, limits=LIMITS)
    assert _codes(storm) == {"E_STORM"}
    assert storm.truth.all_consistent
    assert storm.truth.matched == 2


def test_cross_session_mixin_injection(tmp_path: Path) -> None:
    builder = RecordBuilder()
    source = builder.session(1, 200)
    target = [
        builder.record(2, 100, Phase.HELLO),
        builder.record(2, 101, Phase.AUTH),
        builder.record(2, 303, Phase.DATA),
        builder.record(2, 304, Phase.CLOSE),
    ]
    base = encode_file(source + target)
    output = tmp_path / "mix.slp"
    inject(
        base,
        InjectionSpec(
            mixin_source_session=1,
            mixin_target_session=2,
            mixin_sequence=202,
        ),
        output,
    )
    result = analyze_path(output, limits=LIMITS)
    assert _codes(result) == {"E_MIXUP"}
    assert result.truth.all_consistent
    assert result.truth.matched == 2


def test_verifier_uses_only_embedded_truth(corpus) -> None:
    parsed = parse_bytes((corpus / "half_close_violation.slp").read_bytes())
    stripped = [replace(record, truth_mask=0) for record in parsed.records]
    result = analyze_bytes(encode_file(stripped), limits=LIMITS)
    assert _codes(result) == {"E_AFTER_CLOSE"}
    assert result.status == "verification_failed"
    assert result.truth.matched == 0
    assert result.truth.unexpected == 1
