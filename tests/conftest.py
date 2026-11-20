from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


@pytest.fixture
def sample_paths():
    return {
        "schemas": SAMPLES / "schemas.json",
        "migrations": SAMPLES / "migrations.json",
        "queries": SAMPLES / "queries.json",
    }


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

