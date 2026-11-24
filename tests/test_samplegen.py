from __future__ import annotations

from pathlib import Path

from mfa_guard.samplegen import generate_samples


def test_fixed_seed_generation_is_reproducible(tmp_path: Path):
    first = generate_samples(tmp_path, seed=20_261_125)
    first_manifest = (tmp_path / "samples" / "manifest.json").read_bytes()
    first_attempts = (tmp_path / "samples" / "attempts.jsonl").read_bytes()
    first_registrations = (tmp_path / "samples" / "registrations.json").read_bytes()

    second = generate_samples(tmp_path, seed=20_261_125)
    assert first["manifest"]["sample_sha256"] == second["manifest"]["sample_sha256"]
    assert first_manifest == (tmp_path / "samples" / "manifest.json").read_bytes()
    assert first_attempts == (tmp_path / "samples" / "attempts.jsonl").read_bytes()
    assert (
        first_registrations
        == (tmp_path / "samples" / "registrations.json").read_bytes()
    )

    different = generate_samples(tmp_path, seed=20_261_126)
    assert different["manifest"]["sample_sha256"] != first["manifest"]["sample_sha256"]

