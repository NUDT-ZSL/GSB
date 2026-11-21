"""Requirement 8 (part): resource limits and degradation."""

import pytest

from meeting_align.config import AlignConfig, ResourceLimits
from meeting_align.errors import ResourceLimitError
from meeting_align.features import check_frame_memory
from meeting_align.pipeline import Pipeline


def test_frame_memory_strict_policy_raises():
    limits = ResourceLimits(max_frame_feature_bytes=100,
                            on_frame_memory_exceeded="strict")
    with pytest.raises(ResourceLimitError) as exc:
        check_frame_memory(1_000_000, 400, 160, limits)
    assert exc.value.code == "E_RESOURCE_LIMIT"
    assert exc.value.context["limit"] == "max_frame_feature_bytes"


def test_frame_memory_degrade_policy_restricts_hop():
    limits = ResourceLimits(max_frame_feature_bytes=50_000,
                            on_frame_memory_exceeded="degrade")
    hop, degradation = check_frame_memory(1_000_000, 400, 160, limits)
    assert hop > 160
    assert degradation["category"] == "frame_hop_restricted"
    assert degradation["requested_hop"] == 160
    assert degradation["applied_hop"] == hop


def test_pipeline_marks_degradation(sample_data, tmp_path):
    cfg = AlignConfig()
    cfg.resource.max_frame_feature_bytes = 30_000  # far below the need
    pipe = Pipeline(tmp_path / "p", cfg)
    r = pipe.run(sample_data["batches"](), ["A", "B"])
    assert r["applied_hop"] > cfg.frame_hop
    categories = [d["category"] for d in r["degradations"]]
    assert "frame_hop_restricted" in categories


def test_seam_candidate_cap_marks_degradation(sample_data, tmp_path):
    cfg = AlignConfig(max_seam_candidates=3)
    pipe = Pipeline(tmp_path / "p", cfg)
    r = pipe.run(sample_data["batches"](), ["A", "B"])
    assert len(r["seams"]["global"]["candidates"]) <= 3
    categories = [d["category"] for d in r["degradations"]]
    assert "seam_candidates_capped" in categories


def test_cache_entry_limit_raises(sample_data, tmp_path):
    cfg = AlignConfig()
    cfg.resource.max_cache_entries = 2  # seams alone need more entries
    pipe = Pipeline(tmp_path / "p", cfg)
    with pytest.raises(ResourceLimitError) as exc:
        pipe.run(sample_data["batches"](), ["A", "B"])
    assert exc.value.code == "E_RESOURCE_LIMIT"
