from __future__ import annotations

from pathlib import Path

from slpguard import ResourceLimits, analyze_path

from .conftest import violation_codes


LIMITS = ResourceLimits(max_steps_per_session=1000, max_file_seconds=15.0)


def test_all_semantic_samples_match_embedded_truth(corpus: Path) -> None:
    expected = {
        "duplicate.slp": {"E_TRANSITION"},
        "logical_reorder.slp": {"E_TRANSITION"},
        "stage_jump.slp": {"E_TRANSITION", "E_INCOMPLETE"},
        "auth_before_data.slp": {"E_TRANSITION", "E_INCOMPLETE"},
        "mixup_cross_session.slp": {"E_MIXUP"},
        "mixup_direction.slp": {"E_MIXUP"},
        "reneg_without_base.slp": {"E_MIXUP"},
        "reneg_missing_declaration.slp": {"E_MIXUP", "E_TRANSITION"},
        "reneg_storm.slp": {"E_STORM"},
        "half_close_violation.slp": {"E_AFTER_CLOSE"},
        "incomplete.slp": {"E_INCOMPLETE"},
        "valid_slp.slp": set(),
        "valid_v2.slp": set(),
        "physical_reorder.slp": set(),
    }
    for name, codes in expected.items():
        result = analyze_path(corpus / name, limits=LIMITS)
        assert result.status == "ok", (name, result.to_dict())
        assert violation_codes(result) == codes, name
        assert result.truth.all_consistent, name


def test_structural_error_samples_have_stable_codes(corpus: Path) -> None:
    expected = {
        "invalid_magic.slp": "E_MAGIC",
        "invalid_version.slp": "E_VERSION",
        "invalid_phase.slp": "E_PHASE",
        "invalid_extension.slp": "E_EXT",
        "invalid_ext_combination.slp": "E_EXT",
        "bounds_payload.slp": "E_BOUNDS",
        "bounds_total_length.slp": "E_BOUNDS",
    }
    for name, code in expected.items():
        result = analyze_path(corpus / name, limits=LIMITS)
        assert result.status == "parse_error"
        assert result.parse_error is not None
        assert result.parse_error["code"] == code
        assert result.termination == code
