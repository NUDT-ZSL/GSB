from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandbox_guard.samplegen import generate_sample


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    root = tmp_path / "sample"
    generate_sample(root, seed=361)
    return root


@pytest.fixture()
def sample_bundle():
    from sandbox_guard.samplegen import build_sample

    return build_sample(361)


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
