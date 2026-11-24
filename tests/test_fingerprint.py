from offline_authz.fingerprint import (
    align_fixed_point,
    compare_canonical_fingerprints,
    normalize_fingerprint,
)


def test_fixed_point_alignment_is_exact_for_coarser_platform_scale() -> None:
    aligned = align_fixed_point(123, 2, 6)
    assert aligned["canonical_value"] == 1_230_000
    assert aligned["error_bound_canonical_units"] == 0


def test_fine_scale_alignment_reports_conservative_bound() -> None:
    aligned = align_fixed_point(123_456_789, 8, 6)
    assert aligned["canonical_value"] == 1_234_567
    assert aligned["upper"] == 1_234_568
    assert aligned["error_bound_canonical_units"] == 1


def test_ambiguous_when_uncertainty_interval_crosses_threshold() -> None:
    fingerprint = normalize_fingerprint(
        "LINUX",
        8,
        [{"seq": 1, "components": {"cpu_score": 123_456_789}}],
        canonical_decimals=6,
    )
    points = [
        {
            "seq": 1,
            "components": [
                {
                    "name": "cpu_score",
                    "canonical_value": 1_234_578,
                    "lower": 1_234_578,
                    "upper": 1_234_578,
                }
            ],
        }
    ]
    right = {"canonical_fingerprint_key": "right", "points": points}
    comparison = compare_canonical_fingerprints(fingerprint, right, 10)
    assert comparison["comparison"] == "AMBIGUOUS"
    assert comparison["lower_bound_canonical_units"] <= 10
    assert comparison["upper_bound_canonical_units"] > 10


def test_clear_mismatch_when_lower_bound_exceeds_threshold() -> None:
    left = normalize_fingerprint(
        "MACOS", 3, [{"seq": 1, "components": {"cpu_score": 987_654}}], 6
    )
    right_points = [
        {
            "seq": 1,
            "components": [
                {"name": "cpu_score", "canonical_value": left["points"][0]["components"][0]["canonical_value"] + 1000}
            ],
        }
    ]
    comparison = compare_canonical_fingerprints(
        left, {"canonical_fingerprint_key": "right", "points": right_points}, 10
    )
    assert comparison["comparison"] == "MISMATCH"
    assert comparison["same_device"] is False
