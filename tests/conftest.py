from __future__ import annotations

import json
from pathlib import Path

import pytest

from firewall_audit.samplegen import DEFAULT_SEED, generate


@pytest.fixture(scope="session")
def generated_sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("generated-sample")
    generate(root, DEFAULT_SEED)
    return root


@pytest.fixture
def sample_manifest(generated_sample: Path) -> Path:
    return generated_sample / "sample_manifest.json"


@pytest.fixture
def incremental_manifest(generated_sample: Path) -> Path:
    return generated_sample / "incremental" / "sample_manifest.json"


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
