from __future__ import annotations

import hashlib
from pathlib import Path

from authdiag.samplegen import generate_samples


def _digests(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def test_sample_generator_is_deterministic(tmp_path: Path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    generate_samples(first)
    generate_samples(second)
    assert _digests(first) == _digests(second)


def test_sample_manifest_uses_explicit_marker_observations(sample_dir: Path):
    manifest = (sample_dir / "manifest.json").read_text(encoding="utf-8")
    policy = (sample_dir / "policy.json").read_text(encoding="utf-8")
    assert "sync marker expected sets are explicit observations" in manifest
    assert "expected_tokens" in policy
    assert "expected_sessions" in policy
