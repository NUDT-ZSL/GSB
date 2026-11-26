from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from entity_link_lab.pipeline import run_pipeline
from entity_link_lab.seed import generate_all


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("sample")
    generate_all(path)
    return path


@pytest.fixture(scope="session")
def run_dir(tmp_path_factory: pytest.TempPathFactory, sample_dir: Path) -> Path:
    work = tmp_path_factory.mktemp("baseline")
    report = run_pipeline(
        sample_dir,
        output_dir=work / "output",
        cache_dir=work / "cache",
        checkpoint_dir=work / "checkpoints",
    )
    (work / "report.json").write_text(
        __import__("json").dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return work


@pytest.fixture()
def report(run_dir: Path) -> dict:
    import json

    return json.loads((run_dir / "report.json").read_text(encoding="utf-8"))


@pytest.fixture()
def clean_output_dir() -> Path:
    path = Path("validation/pytest-tmp")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)
