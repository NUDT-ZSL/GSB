from __future__ import annotations

import json
from pathlib import Path

import pytest

from authdiag.samplegen import generate_samples


@pytest.fixture(scope="session")
def sample_paths(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    directory = tmp_path_factory.mktemp("authdiag-samples")
    return generate_samples(directory)


@pytest.fixture
def sample_dir(sample_paths: dict[str, str]) -> Path:
    return Path(sample_paths["policy.json"]).parent


def load_json(path: Path | str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def read_json():
    return load_json
