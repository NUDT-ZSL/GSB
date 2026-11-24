from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sso_ticket_guard.normalize import load_dataset
from sso_ticket_guard.samplegen import DEFAULT_SEED, generate_samples


@pytest.fixture(scope="session")
def samples_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    samples = tmp_path_factory.mktemp("samples")
    generate_samples(samples, DEFAULT_SEED)
    return samples


@pytest.fixture(scope="session")
def dataset(samples_dir: Path):
    return load_dataset(samples_dir)


@pytest.fixture(scope="session")
def truth(samples_dir: Path) -> dict:
    return json.loads((samples_dir / "truth.json").read_text(encoding="utf-8"))

