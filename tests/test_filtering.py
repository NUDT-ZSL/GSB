import math
from fractions import Fraction

import pytest

from mt_pipeline.errors import Codes, PipelineError
from mt_pipeline.filtering import (FilterConfig, flush_subnormal,
                                   metrics_from_details, noise_decision,
                                   ratio_decision, run_filter)


def test_flush_subnormal():
    assert flush_subnormal(5e-324) == 0.0
    assert flush_subnormal(-5e-324) == 0.0
    assert flush_subnormal(0.5) == 0.5


def test_strict_strategy_plain_compare():
    cfg = FilterConfig(strategy="strict", ratio_min=0.5, ratio_max=2.0)
    assert ratio_decision(3, 1, cfg)[0] is False  # 1/3 < 0.5
    assert ratio_decision(2, 4, cfg)[0] is True   # exactly 2.0


def test_tolerant_matches_exact_fraction_at_boundary():
    # 1/3 as a real number is greater than float(1/3); the tolerant strategy
    # arbitrates with exact Fractions, so the decision cannot flip with
    # rounding precision.
    cfg = FilterConfig(strategy="tolerant", ratio_min=1.0 / 3.0, ratio_max=2.0,
                       tolerance=1e-6)
    ok, _ = ratio_decision(3, 1, cfg)
    assert ok is True  # Fraction(1,3) >= Fraction(float(1/3))
    cfg2 = FilterConfig(strategy="tolerant", ratio_min=0.5, ratio_max=2.0,
                        tolerance=1e-6)
    ok2, _ = ratio_decision(7, 3, cfg2)  # 3/7 = 0.428571... < 0.5
    assert ok2 is False


def test_tolerant_stable_across_precision_modes():
    cfg = FilterConfig(strategy="tolerant", ratio_min=0.5, ratio_max=2.0)
    decisions = set()
    for scale in (1, 10**6, 10**12):  # same rational ratio, different magnitudes
        ok, _ = ratio_decision(2 * scale, 1 * scale, cfg)
        decisions.add(ok)
    assert decisions == {True}


def test_noise_boundary():
    cfg = FilterConfig(strategy="tolerant", noise_max=0.25)
    assert noise_decision(0.25, cfg) is True
    assert noise_decision(0.250001, cfg) is False
    strict = FilterConfig(strategy="strict", noise_max=0.25)
    assert noise_decision(0.25, strict) is True


@pytest.mark.parametrize("kwargs", [
    {"strategy": "fuzzy"},
    {"tolerance": -1.0},
    {"tolerance": float("inf")},
    {"ratio_min": 2.0, "ratio_max": 1.0},
    {"noise_max": -0.1},
])
def test_threshold_invalid(kwargs):
    with pytest.raises(PipelineError) as exc:
        FilterConfig(**kwargs).validate()
    assert exc.value.code == Codes.THRESHOLD_INVALID


def test_run_filter_details_sorted_and_metrics_recomputable():
    records = [
        {"pair_id": "p001", "domain_tag": "news", "noise_level": 0.0},
        {"pair_id": "p000", "domain_tag": "tech", "noise_level": 0.9},
    ]
    seg_pairs = [
        {"pair_id": "p000", "src_pieces": ["a", "b"], "tgt_pieces": ["c"]},
        {"pair_id": "p001", "src_pieces": ["a"], "tgt_pieces": ["c", "d"]},
    ]
    cfg = FilterConfig(strategy="strict")
    details = run_filter(records, seg_pairs, {"p001": "DUPLICATE"}, cfg)
    assert [d["pair_id"] for d in details] == ["p000", "p001"]
    assert details[0]["decision"] == "drop" and "NOISE" in details[0]["reasons"]
    assert details[1]["decision"] == "drop" and "DUPLICATE" in details[1]["reasons"]
    metrics = metrics_from_details(details)
    assert metrics["total"] == 2 and metrics["kept"] == 0
    assert metrics == metrics_from_details(details)  # independently recomputed
