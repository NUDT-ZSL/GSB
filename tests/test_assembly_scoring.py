from __future__ import annotations

import math

import pytest

from ctr_cvr_joint.assembly import assemble_features, decode_matrix
from ctr_cvr_joint.errors import (
    BusinessError,
    E_DUP_REQUEST,
    E_THRESHOLD_INVALID,
    E_TOLERANCE_INVALID,
)
from ctr_cvr_joint.models import Sample, ThresholdConfig
from ctr_cvr_joint.scoring import (
    _protect_probability,
    compare_probability,
    recompute_score_row,
    score_samples,
)


def test_assembly_is_byte_deterministic_and_order_independent(fixtures):
    first = assemble_features(
        fixtures["samples"], fixtures["spec_v3"].spec, seed=20261124
    )
    second = assemble_features(
        list(reversed(fixtures["samples"])),
        fixtures["spec_v3"].spec,
        seed=20261124,
    )
    assert first.matrix_bytes == second.matrix_bytes
    assert first.summary == second.summary
    assert first.matrix_sha256 == second.matrix_sha256
    _, rows = decode_matrix(first.matrix_bytes)
    assert len(rows) == 12
    assert all(len(row) == len(first.feature_names) for row in rows)


def test_duplicate_request_is_rejected_before_assembly(fixtures):
    duplicate = fixtures["samples"] + [fixtures["samples"][0]]
    with pytest.raises(BusinessError) as excinfo:
        assemble_features(duplicate, fixtures["spec_v3"].spec)
    assert excinfo.value.code == E_DUP_REQUEST
    assert excinfo.value.details["request_id"] == "req-0001"


def test_missing_strategy_is_applied_and_summarized(fixtures):
    sample = next(item for item in fixtures["samples"] if item.request_id == "req-0002")
    result = assemble_features([sample], fixtures["spec_v3"].spec, seed=20261124)
    detail = next(
        item
        for item in result.rows[0].details
        if item["feature"] == "numeric.price"
    )
    assert detail["imputed"] is True
    assert detail["missing_strategy"] == "default"
    assert detail["raw_value"] == 88.0
    assert detail["encoded_value"] == 0.0


def test_contribution_trace_recomputes_probabilities(fixtures):
    assembled = assemble_features(
        fixtures["samples"], fixtures["spec_v3"].spec, seed=20261124
    )
    result = score_samples(
        assembled,
        fixtures["stats_v1"],
        fixtures["spec_v3"].spec,
        ThresholdConfig(click_threshold=0.45, cvr_threshold=0.4),
    )
    for row in result.rows:
        for metric in ("click", "convert"):
            block = row[metric]
            recomputed_logit = math.fsum(
                [block["base_logit"]]
                + [item["contribution"] for item in block["contributions"]]
            )
            assert recomputed_logit == block["logit_before_clamp"]
        replay = recompute_score_row(row)
        assert replay["click"]["probability"] == row["click"]["probability"]
        assert replay["convert"]["probability"] == row["convert"]["probability"]
        assert replay["expected_conversion"] == row["expected_conversion"]


def test_threshold_policies_are_stable_at_boundary():
    assert compare_probability(
        0.45, 0.45, policy="strict", tolerance=0.02, stable_digits=12
    )["decision"] is False
    tolerance = compare_probability(
        0.45, 0.45, policy="tolerance", tolerance=0.02, stable_digits=12
    )
    assert tolerance["decision"] is True
    assert tolerance["comparison"] == "within_tolerance_ge"
    for _ in range(100):
        assert (
            compare_probability(
                0.4500000000004,
                0.45,
                policy="tolerance",
                tolerance=0.02,
                stable_digits=12,
            )["decision"]
            is True
        )


def test_invalid_threshold_and_tolerance_codes():
    with pytest.raises(BusinessError) as excinfo:
        ThresholdConfig(policy="strict", click_threshold=1.2)
    assert excinfo.value.code == E_THRESHOLD_INVALID
    with pytest.raises(BusinessError) as excinfo:
        ThresholdConfig(policy="tolerance", tolerance=-0.1)
    assert excinfo.value.code == E_TOLERANCE_INVALID
    with pytest.raises(BusinessError) as excinfo:
        ThresholdConfig(policy="unknown")
    assert excinfo.value.code == E_THRESHOLD_INVALID


def test_subnormal_probability_floor():
    floor, protection = _protect_probability(0.0, 12)
    ceiling, ceiling_protection = _protect_probability(1.0, 12)
    assert floor >= 1e-12
    assert ceiling <= 1.0 - 1e-12
    assert protection["applied"] == "floor"
    assert ceiling_protection["applied"] == "ceiling"
    assert _protect_probability(0.0, 12)[0] == floor

