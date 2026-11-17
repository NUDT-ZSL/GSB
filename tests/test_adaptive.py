from __future__ import annotations

from pathlib import Path

from klp_audit.config import AuditConfig

from .helpers import audit_path


def _base_config(**overrides) -> AuditConfig:
    values = {
        "concurrency": 1,
        "session_timeout_seconds": None,
        "max_derivations_per_session": 10_000,
        "scale_bytes_threshold": 10_000_000,
        "scale_derived_threshold": 10_000,
        "sampling_ratio": 0.25,
        "sample_chunk_size": 8,
    }
    values.update(overrides)
    return AuditConfig(**values)


def test_below_threshold_uses_full_validation(generated_root, manifest):
    sample = generated_root / manifest["files"]["scale_small"]["sample"]
    report = audit_path(sample, config=_base_config())
    adaptive = report["adaptive"]
    assert adaptive["mode"] == "full"
    assert adaptive["degraded"] is False
    assert adaptive["coverage"]["ratio"] == 1.0
    assert adaptive["coverage"]["covered_derived_objects"] == 4
    assert report["file"]["conclusion"] == "PASS"


def test_above_derived_threshold_degrades_with_explicit_coverage(
    generated_root, manifest
):
    sample = generated_root / manifest["files"]["scale_large"]["sample"]
    report = audit_path(
        sample,
        config=_base_config(scale_derived_threshold=10),
    )
    adaptive = report["adaptive"]
    assert adaptive["mode"] == "degraded_sampling"
    assert adaptive["degraded"] is True
    assert adaptive["triggered_by"] == ["derived_objects"]
    coverage = adaptive["coverage"]
    assert coverage["total_derived_objects"] == 60
    assert coverage["total_chunks"] == 8
    assert coverage["covered_chunks"] == 2
    assert 0 < coverage["covered_derived_objects"] <= 16
    assert coverage["ratio"] == round(
        coverage["covered_derived_objects"] / 60,
        8,
    )
    assert 0 < coverage["ratio"] < 1
    assert report["file"]["conclusion"] == "DEGRADED_PASS"


def test_byte_threshold_equality_is_full_but_exceeding_degrades(
    generated_root, manifest
):
    sample = generated_root / manifest["files"]["scale_small"]["sample"]
    size = Path(sample).stat().st_size
    at_limit = audit_path(
        sample,
        config=_base_config(scale_bytes_threshold=size, scale_derived_threshold=10_000),
    )
    over_limit = audit_path(
        sample,
        config=_base_config(
            scale_bytes_threshold=size - 1,
            scale_derived_threshold=10_000,
        ),
    )
    assert at_limit["adaptive"]["mode"] == "full"
    assert over_limit["adaptive"]["mode"] == "degraded_sampling"
    assert over_limit["adaptive"]["triggered_by"] == ["input_bytes"]


def test_same_observations_produce_identical_mode_coverage_and_conclusions(
    generated_root, manifest
):
    sample = generated_root / manifest["files"]["scale_large"]["sample"]
    first = audit_path(
        sample,
        config=_base_config(concurrency=1, scale_derived_threshold=10),
    )
    second = audit_path(
        sample,
        config=_base_config(concurrency=4, scale_derived_threshold=10),
    )
    assert first["adaptive"] == second["adaptive"]
    assert first["file"]["conclusion"] == second["file"]["conclusion"]
    first_sessions = [
        (session["session_id"], session["status"], session["checks"]["derivation"])
        for session in first["sessions"]
    ]
    second_sessions = [
        (session["session_id"], session["status"], session["checks"]["derivation"])
        for session in second["sessions"]
    ]
    assert first_sessions == second_sessions

