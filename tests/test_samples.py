from __future__ import annotations

from pathlib import Path

from h2diag.samplecheck import verify_samples
from h2diag.samples import generate_samples


def test_sample_generation_is_deterministic(tmp_path: Path) -> None:
    first = generate_samples(tmp_path / "first")
    second = generate_samples(tmp_path / "second")
    assert first["manifest_digest"] == second["manifest_digest"]
    assert first["scenario_digest"] == second["scenario_digest"]
    for entry in first["files"]:
        first_bytes = (tmp_path / "first" / entry["path"]).read_bytes()
        second_bytes = (tmp_path / "second" / entry["path"]).read_bytes()
        assert first_bytes == second_bytes


def test_checked_in_samples_validate_and_regenerate(tmp_path: Path) -> None:
    report = verify_samples(Path(__file__).resolve().parents[1] / "samples")
    assert report["ok"] is True
    assert report["deterministic_regeneration_ok"] is True
