from __future__ import annotations

from pathlib import Path

import pytest

from alert_engine.engine import Engine, InputPaths
from alert_engine.models import EngineConfig
from alert_engine.sample_gen import generate_sample


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("fixed-seed-sample")
    generate_sample(path)
    return path


def run_sample(
    sample_dir: Path,
    workspace: Path,
    *,
    workers: int = 1,
    queue_limit: int = 64,
    host_timeout_ms: int = 10_000,
    max_alerts_per_host: int = 100_000,
    resume: bool = False,
) -> tuple[Engine, dict]:
    engine = Engine(
        workspace,
        EngineConfig(
            max_workers=workers,
            pending_host_queue_limit=queue_limit,
            per_host_timeout_ms=host_timeout_ms,
            max_alerts_per_host=max_alerts_per_host,
        ),
    )
    report = engine.run(
        InputPaths.from_directory(sample_dir),
        resume=resume,
    )
    return engine, report


@pytest.fixture()
def default_report(
    sample_dir: Path, tmp_path: Path
) -> tuple[Engine, dict]:
    return run_sample(sample_dir, tmp_path / "engine-work")
