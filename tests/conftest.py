"""Shared fixtures building the fixed-seed sample inputs in tmp_path."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from terrain_engine import digest, engine as engine_mod, storage  # noqa: E402
from terrain_engine.model import Edit  # noqa: E402
from terrain_engine.validate import load_task  # noqa: E402
from scripts import generate_samples  # noqa: E402


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    target = tmp_path / "samples"
    generate_samples.write_json(target / "terrain.json",
                                generate_samples.build_terrain())
    generate_samples.write_json(target / "task.json",
                                generate_samples.build_task())
    generate_samples.write_json(target / "edits.json",
                                generate_samples.build_edits())
    generate_samples.write_json(target / "injections.json",
                                generate_samples.build_injections())
    return target


@pytest.fixture()
def workspace(tmp_path: Path, sample_dir: Path) -> Path:
    return tmp_path


@pytest.fixture()
def task(sample_dir: Path):
    return load_task(sample_dir / "task.json")


@pytest.fixture()
def make_engine():
    def _make(task, root, injections=None, stop_event=None):
        return engine_mod.Engine(
            task, root, injections=injections or {},
            stop_event=stop_event)
    return _make


@pytest.fixture()
def edit_list(sample_dir: Path):
    from terrain_engine.edits import load_edits
    return load_edits(sample_dir / "edits.json")

