from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from alertlab.service import AlertLabService


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workspace() -> Path:
    return ROOT


@pytest.fixture
def service(tmp_path: Path, workspace: Path) -> AlertLabService:
    return AlertLabService(workspace, database_path=tmp_path / "work.sqlite3")


@pytest.fixture
def loaded_service(service: AlertLabService) -> AlertLabService:
    service.load()
    return service


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
