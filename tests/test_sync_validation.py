from __future__ import annotations

from pathlib import Path

from scripts.validate_samples import validate


def test_truth_is_compared_by_sync_markers(sample_dir: Path) -> None:
    result = validate(sample_dir)
    assert result["ok"] is True
    assert result["mismatches"] == []
    assert result["marker_errors"] == []
    assert result["checked_sync_cases"] >= 20
