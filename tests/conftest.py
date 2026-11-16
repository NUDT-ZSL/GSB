from __future__ import annotations

from pathlib import Path

import pytest

from video_pipeline.samples import generate_sample_corpus


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_CONFIG = PROJECT_ROOT / "configs" / "model.json"
FULL_MATCHING_CONFIG = PROJECT_ROOT / "configs" / "matching_full.json"
SERIAL_MATCHING_CONFIG = PROJECT_ROOT / "configs" / "matching_serial.json"
BUDGET_MATCHING_CONFIG = PROJECT_ROOT / "configs" / "matching.json"


@pytest.fixture(scope="session")
def corpus_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("synthetic-corpus")
    generate_sample_corpus(root, root / "ground_truth.json")
    return root
