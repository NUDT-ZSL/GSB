from __future__ import annotations

from fractions import Fraction

from tlsdiag.clock import PrecisionConfig
from tlsdiag.precision import Participant, PrecisionLedger, compare_remaining


def test_exact_scale_has_zero_deviation():
    config = PrecisionConfig(1000, 1, 12, "ROUND_HALF_EVEN", 500, 1000)
    ledger = PrecisionLedger(config)
    result = ledger.convert_relative_seconds("a", "v1", "step", 3600, 1_700_000_000_000)
    assert result.abs_deviation_ms == "0"
    assert result.direction == "exact"
    assert result.fixed_abs_ms == 1_700_003_600_000


def test_fractional_scale_produces_bounded_error():
    # 1003 ms per 1000 s -> non-integral factor forces quantisation in Q4.
    config = PrecisionConfig(1003, 1000, 4, "ROUND_HALF_EVEN", 500, 1000)
    ledger = PrecisionLedger(config)
    result = ledger.convert_relative_seconds("a", "v1", "step", 10_000, 0)
    truth = Fraction(10_000 * 1003, 1000)
    assert Fraction(result.abs_deviation_ms) == abs(Fraction(result.fixed_abs_ms) - truth)
    assert Fraction(result.error_bound_ms) >= Fraction(result.abs_deviation_ms)
    assert result.direction in {"up", "down", "exact"}
    assert Fraction(ledger.cumulative_bound) > 0


def test_error_source_chain_contains_provenance():
    config = PrecisionConfig(1003, 1000, 4, "ROUND_HALF_EVEN", 500, 1000)
    ledger = PrecisionLedger(config)
    ledger.convert_relative_seconds("anchor-x", "v1", "tick0+scale", 7, 100)
    chain = ledger.error_source_chain()
    assert chain[0]["alias"] == "anchor-x"
    assert chain[0]["source_version"] == "v1"
    assert "round_direction" in chain[0]


def test_sensitive_pair_inside_tolerance():
    config = PrecisionConfig(1000, 1, 12, "ROUND_HALF_EVEN", 500, 1000)
    a = Participant("a", "v1", 200, Fraction(200), Fraction(0), Fraction(250))
    b = Participant("b", "v2", 450, Fraction(450), Fraction(0), Fraction(250))
    result = compare_remaining("reverify", [a, b], config)
    assert result.precision_sensitive is True
    assert result.difference_fixed_ms == -250


def test_pair_outside_tolerance_not_sensitive():
    config = PrecisionConfig(1000, 1, 12, "ROUND_HALF_EVEN", 500, 1000)
    a = Participant("a", "v1", 0, Fraction(0), Fraction(0), Fraction(250))
    b = Participant("b", "v2", 5000, Fraction(5000), Fraction(0), Fraction(250))
    result = compare_remaining("expiry", [a, b], config)
    assert result.precision_sensitive is False


def test_truth_path_flips_decision_near_expiry():
    config = PrecisionConfig(1003, 1000, 2, "ROUND_HALF_EVEN", 500, 1000)
    # Fixed path says 0 (expired), truth path is slightly positive (valid).
    p = Participant("edge", "v1", 0, Fraction(2), Fraction(2), Fraction(2))
    result = compare_remaining("expiry", [p], config)
    assert result.fixed_decisions["edge"] != result.truth_decisions["edge"]
    assert result.precision_sensitive is True
    assert result.flips_at_higher_scale is True
