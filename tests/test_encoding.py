from __future__ import annotations

from georope.service import check_request, encode_request


def test_full_sample_encoding_and_verification(sample_payload: dict) -> None:
    result = encode_request(sample_payload)
    assert result["ok"] is True
    verification = result["verification_summary"]
    assert verification["passed"] is True
    assert verification["reference_consistent"] is True
    assert verification["cross_version_consistent"] is True
    assert verification["cross_combination_consistent"] is True
    assert verification["global_boundary_continuous"] is True
    assert result["reference_verification"]["mismatch_count"] == 0
    assert result["reference_verification"]["max_angle_error"] == 0.0
    assert result["reference_verification"]["max_encoded_error"] == 0.0
    boundary = result["boundary_continuity_summary"]
    assert boundary["seams_checked"] == 36
    assert boundary["seams_with_global_discontinuity"] == 0
    assert boundary["seams_with_local_phase_breaks"] > 0
    assert len(result["encoded_fields"]) == 9
    assert len(result["rotation_angle_records"]) == 36
    first_field = result["encoded_fields"][0]
    assert first_field["shape"] == [6, 8, 12]
    assert len(first_field["positions"]) == 48
    assert len(first_field["rotation_angles"]) == 6
    assert len(first_field["rotation_angles"][0]) == 8


def test_cross_version_field_hashes_are_equal(sample_payload: dict) -> None:
    result = encode_request(sample_payload)
    for combination in ("low_only", "mid_high", "all_bands"):
        hashes = {
            field["field_hash"]
            for field in result["encoded_fields"]
            if field["combination"] == combination
        }
        assert len(hashes) == 1


def test_check_request_reports_manual_cases(sample_payload: dict) -> None:
    result = check_request(sample_payload)
    assert result["ok"] is True
    assert result["passed"] is True
    assert result["checks"] == {
        "reference_consistency": True,
        "cross_version_consistency": True,
        "cross_combination_consistency": True,
        "global_seam_continuity": True,
        "manual_cases": True,
    }
