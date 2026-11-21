from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meeting_vad.config import PathConfig, load_config
from meeting_vad.session import AnalysisSession
from meeting_vad.synth import generate_samples


@pytest.fixture()
def repo_root() -> Path:
    return ROOT


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    output = tmp_path / "samples"
    generate_samples(ROOT / "config" / "synthesis.json", output, clean=True)
    return output


@pytest.fixture()
def app_config(tmp_path: Path, sample_dir: Path):
    config = load_config(ROOT / "config" / "app.json", base_dir=tmp_path)
    paths = PathConfig(
        samples_manifest=sample_dir / "samples.jsonl",
        annotations_manifest=sample_dir / "annotations.jsonl",
        snapshots_dir=tmp_path / "runtime" / "snapshots",
        replay_dir=tmp_path / "runtime" / "replay",
        runtime_annotations=tmp_path / "runtime" / "annotations.current.jsonl",
        runtime_config=tmp_path / "runtime" / "config.current.json",
    )
    return replace(config, paths=paths)


@pytest.fixture()
def session(app_config) -> AnalysisSession:
    return AnalysisSession(app_config, use_runtime_annotations=False)
