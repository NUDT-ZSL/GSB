from __future__ import annotations

from fractions import Fraction

import pytest

from supply_diag.versioning import (
    Version,
    VersionContext,
    fraction_to_dict,
    intersect_intervals,
    parse_version_expression,
)


def test_expression_forms_normalize_identically() -> None:
    forms = [
        "[1.2.0,2.0.0)",
        ">=1.2.0, <2.0.0",
        ">= 1.2.0 < 2.0.0",
        {
            "lower": "1.2.0",
            "upper": "2.0.0",
            "lower_inclusive": True,
            "upper_inclusive": False,
        },
    ]
    normalized = [parse_version_expression(value).canonical_text() for value in forms]
    assert normalized == ["[1.2.0,2.0.0)"] * len(forms)


def test_exact_and_closed_open_endpoints() -> None:
    exact = parse_version_expression("1.2.3")
    assert exact.is_point
    assert exact.canonical_text() == "[1.2.3,1.2.3]"

    closed_open = parse_version_expression("(1.0.0,2.0.0]")
    assert not closed_open.lower_inclusive
    assert closed_open.upper_inclusive
    assert not closed_open.contains(closed_open.lower)
    assert closed_open.contains(closed_open.upper)


def test_formally_empty_open_point_is_unsatisfiable_not_uncertain() -> None:
    interval = parse_version_expression("[1.0.0,1.0.0)")
    result = intersect_intervals([interval])
    assert result.core_empty
    assert result.expanded_empty
    assert result.status == "unsatisfiable"


def test_error_overlap_marks_uncertain() -> None:
    upper = parse_version_expression("<=1.0.0")
    lower = parse_version_expression(">1.0.0")
    context = VersionContext.from_versions(
        [upper.lower, upper.upper, lower.lower, lower.upper]
    )
    result = intersect_intervals([upper, lower], context)
    assert result.core_empty
    assert not result.expanded_empty
    assert result.status == "uncertain"
    assert result.reason == "ERROR_BOUND_OVERLAP"
    assert result.width_error == Fraction(2, 1)


def test_intersection_tie_prefers_open_endpoint_and_symmetric_errors() -> None:
    left = parse_version_expression("[1.0.0,2.0.0]")
    right = parse_version_expression("(1.0.0,2.0.0)")
    context = VersionContext.from_versions(
        [left.lower, left.upper, right.lower, right.upper]
    )
    forward = intersect_intervals([left, right], context)
    reverse = intersect_intervals([right, left], context)
    assert forward.interval.canonical_text() == "(1.0.0,2.0.0)"
    assert forward.to_dict(context) == reverse.to_dict(context)
    assert forward.lower_error == Fraction(3, 2)
    assert forward.upper_error == Fraction(3, 2)
    assert forward.width_error == Fraction(3, 1)
    assert not forward.interval.lower_inclusive
    assert not forward.interval.upper_inclusive


def test_fraction_serialization_is_exact_for_power_of_two() -> None:
    serialized = fraction_to_dict(Fraction(1, 2**32))
    assert serialized is not None
    assert serialized["decimal_exact"] is True
    assert serialized["numerator"] == 1
    assert serialized["denominator"] == 2**32
    assert len(serialized["decimal"].split(".")[1]) == 32


def test_unsupported_expression_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_version_expression("~>1.0")


def test_version_comparison_pads_trailing_zero_components() -> None:
    assert Version.parse("1.2") == Version.parse("1.2.0")
    assert Version.parse("1.2") < Version.parse("1.2.1")
    assert parse_version_expression("[1.2,1.2.0]").is_point
    assert parse_version_expression("[1.2.0,1.2]").is_point


def test_contradictory_constraint_list_stays_empty() -> None:
    interval = parse_version_expression(["<=1.0.0", ">=3.0.0"])
    result = intersect_intervals([interval])
    assert result.core_empty
    assert result.status == "unsatisfiable"

