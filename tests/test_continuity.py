from __future__ import annotations

from georope.manual import run_manual_cases


def test_manual_seam_sample_global_continuous_local_break() -> None:
    result = run_manual_cases()
    seam = result["cross_block_boundary"]
    assert result["passed"] is True
    assert seam["global_phase_continuous"] is True
    assert seam["global_discontinuity_location_count"] == 0
    assert seam["local_phase_break_detected"] is True
    assert seam["max_global_phase_difference"] == 0.0
    assert seam["max_local_phase_difference"] == 3.0


def test_manual_same_geo_different_schema_parameters_are_consistent() -> None:
    result = run_manual_cases()
    same_geo = result["same_geographic_position_different_parameters"]
    assert same_geo["normalized_origins_m"] == {
        "manual-v1": [3.0, 2.0],
        "manual-v2": [3.0, 2.0],
        "manual-v3": [3.0, 2.0],
    }
    assert same_geo["cross_version_consistent"] is True
    assert same_geo["difference_count"] == 0
    assert same_geo["coordinate_unit_drift_count"] == 0
    assert same_geo["channel_group_drift_count"] == 0
