"""Shared fixtures for the checker test-suite."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import make_sample  # noqa: E402


@pytest.fixture(scope="session")
def sample_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("sample")
    make_sample.make_inputs(root)
    return root


@pytest.fixture(scope="session")
def manifest_path(sample_root: Path) -> Path:
    return sample_root / "subtitles.json"


@pytest.fixture(scope="session")
def frames_dir(sample_root: Path) -> Path:
    return sample_root / "frames"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def base_manifest() -> dict:
    """A minimal valid manifest for mutation-based tests."""
    return {
        "canvas": {"width": 1080, "height": 1920},
        "safe_margin": {"top": 120, "right": 120, "bottom": 120, "left": 120},
        "fx_region": {"x": 0, "y": 1640, "width": 1080, "height": 200},
        "subtitles": [
            {
                "id": "T1",
                "start_ms": 0,
                "end_ms": 1000,
                "text": "示例字幕",
                "font_size": 48,
                "anchor": {"x": 540, "y": 800},
            }
        ],
    }
