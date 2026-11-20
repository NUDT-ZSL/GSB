import math

import pytest

from transfer_scheduler.config import GateConfig
from transfer_scheduler.errors import EConfigInvalid
from transfer_scheduler.threshold import (
    BAND,
    STRICT,
    TIE_ACCEPT,
    TIE_REJECT,
    evaluate_gate,
    sanitize_subnormal,
    validate_transfer_strength,
)


def test_strict_gate_exact_tie_uses_explicit_rule():
    accept = GateConfig(threshold=0.5, strategy=STRICT, band_on_tie=TIE_ACCEPT)
    reject = GateConfig(threshold=0.5, strategy=STRICT, band_on_tie=TIE_REJECT)
    assert evaluate_gate(0.5, accept).accepted is True
    assert evaluate_gate(0.5, reject).accepted is False
    assert evaluate_gate(0.5 + 1e-15, accept).accepted is True
    assert evaluate_gate(0.5 - 1e-15, reject).accepted is False


def test_band_gate_swallows_threshold_neighborhood():
    gate = GateConfig(
        threshold=0.5,
        strategy=BAND,
        tolerance=1e-9,
        band_on_tie=TIE_REJECT,
    )
    near_above = evaluate_gate(0.5 + 1e-12, gate)
    near_below = evaluate_gate(0.5 - 1e-12, gate)
    exact = evaluate_gate(0.5, gate)
    assert near_above.in_tie_band and near_below.in_tie_band and exact.in_tie_band
    assert {near_above.accepted, near_below.accepted, exact.accepted} == {False}
    outside = evaluate_gate(0.5 + 1e-8, gate)
    assert outside.accepted is True and outside.in_tie_band is False


def test_band_decisions_never_flip_across_repeats():
    gate = GateConfig(threshold=0.5, strategy=BAND, tolerance=1e-9, band_on_tie=TIE_ACCEPT)
    values = (0.5, 0.5 + 1e-12, 0.5 - 1e-12, 0.51)
    reference = [evaluate_gate(value, gate).accepted for value in values]
    for _ in range(100):
        assert [evaluate_gate(value, gate).accepted for value in values] == reference


def test_subnormal_protection():
    floored = sanitize_subnormal(1e-308, 1e-300)
    assert floored == 1e-300
    assert sanitize_subnormal(0.0, 1e-300) == 0.0
    assert math.copysign(1.0, sanitize_subnormal(-1e-308, 1e-300)) < 0


def test_invalid_threshold_configuration():
    with pytest.raises(EConfigInvalid) as info:
        evaluate_gate(0.1, GateConfig(threshold=float("nan"), field_name="x"))
    assert info.value.context["field"] == "x.threshold"
    with pytest.raises(EConfigInvalid):
        evaluate_gate(
            0.1,
            GateConfig(threshold=0.5, strategy=STRICT, tolerance=1e-9, field_name="x"),
        )
    with pytest.raises(EConfigInvalid):
        evaluate_gate(0.1, GateConfig(threshold=0.5, strategy="weird", field_name="x"))


def test_invalid_transfer_strength():
    gate = GateConfig(threshold=0.0, field_name="transfer_strength")
    with pytest.raises(EConfigInvalid) as info:
        validate_transfer_strength(1.5, gate)
    assert info.value.context["field"] == "transfer_strength"
    with pytest.raises(EConfigInvalid):
        validate_transfer_strength(float("inf"), gate)
    assert validate_transfer_strength(0.37, gate) == 0.37
