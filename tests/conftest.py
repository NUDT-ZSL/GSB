from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from smugglebench.engine import RunConfig, Workbench


@pytest.fixture(scope="session")
def sample_workspace() -> Path:
    path = Path(__file__).resolve().parents[1] / "samples"
    assert path.is_dir(), "run tools/generate_samples.py before the tests"
    return path


@pytest.fixture(scope="session")
def baseline_report(sample_workspace: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    workdir = tmp_path_factory.mktemp("baseline-work")
    return Workbench(
        sample_workspace,
        workdir,
        RunConfig(concurrency=2, resume=False),
    ).run()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def write_flow(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
