from __future__ import annotations

import json
from pathlib import Path

import pytest

from mixedpipe.samples import generate_dataset


@pytest.fixture()
def demo_root(tmp_path: Path) -> Path:
    generate_dataset(tmp_path, force=True)
    return tmp_path


def write_config(root: Path, values: dict) -> Path:
    path = root / "configs" / "test.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(values, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path
