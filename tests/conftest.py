from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_samples import generate  # noqa: E402
from slo_workbench.loader import load_sample_bundle  # noqa: E402
from slo_workbench.storage import Workspace  # noqa: E402


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    ws = Workspace(tmp_path / "wb")
    ws.bootstrap()
    return ws


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "samples"
    generate(path)
    return path


@pytest.fixture()
def loaded_workspace(workspace: Workspace, sample_dir: Path) -> Workspace:
    load_sample_bundle(workspace, sample_dir)
    return workspace


@pytest.fixture()
def clock_config(sample_dir: Path):
    from slo_workbench.models import parse_clock_config

    return parse_clock_config(json.loads((sample_dir / "clock.json").read_text()))
