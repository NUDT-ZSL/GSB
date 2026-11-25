from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diagnostic_engine.config import default_config
from diagnostic_engine.generator import generate_fixture, generate_records, read_jsonl


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("sample")
    generate_fixture(path, seed=440)
    return path


@pytest.fixture()
def config(fixture_dir: Path) -> dict:
    return json.loads((fixture_dir / "scenario.json").read_text(encoding="utf-8"))


@pytest.fixture()
def records(fixture_dir: Path):
    return read_jsonl(fixture_dir / "observations.jsonl")


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "runtime"
    path.mkdir()
    return path


@pytest.fixture()
def engine(config: dict, records: list[dict], workdir: Path):
    from diagnostic_engine.engine import DiagnosticEngine

    return DiagnosticEngine(copy.deepcopy(config), copy.deepcopy(records), workdir=workdir)


@pytest.fixture(scope="session")
def generated_nine_windows():
    cfg = default_config(440)
    cfg["time"]["windows"] = 9
    return cfg, generate_records(cfg)
