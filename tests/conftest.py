from __future__ import annotations

from pathlib import Path

import pytest

from config_forensics.samplegen import generate_samples


@pytest.fixture
def sample_root(tmp_path: Path) -> Path:
    root = tmp_path / "samples"
    generate_samples(root)
    return root
