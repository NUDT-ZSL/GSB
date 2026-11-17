from __future__ import annotations

from pathlib import Path

import pytest

from intent_engine.pipeline import run_pipeline

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESOURCES = DATA / "resources"
CONFIG = RESOURCES / "config.json"
CORPUS = DATA / "corpus.jsonl"
INJECTIONS = DATA / "inject.json"


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA


@pytest.fixture(scope="session")
def resources_dir() -> Path:
    return RESOURCES


@pytest.fixture(scope="session")
def config_path() -> Path:
    return CONFIG


@pytest.fixture(scope="session")
def corpus_path() -> Path:
    return CORPUS


@pytest.fixture(scope="session")
def injection_path() -> Path:
    return INJECTIONS


def execute(
    *,
    state_dir: Path,
    resources_dir: Path = RESOURCES,
    config_path: Path = CONFIG,
    corpus_path: Path = CORPUS,
    injection_path: Path | None = None,
    workers: int | None = None,
    fresh: bool = True,
    interrupt_after: int | None = None,
):
    return run_pipeline(
        corpus_path,
        resources_dir,
        config_path=config_path,
        injection_path=injection_path,
        state_dir=state_dir,
        workers=workers,
        fresh=fresh,
        interrupt_after=interrupt_after,
    )

