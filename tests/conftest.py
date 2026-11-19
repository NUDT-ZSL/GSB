from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from prefworkbench.canonical import read_json
from prefworkbench.generator import generate_workspace, read_config, write_config


@pytest.fixture
def generated(tmp_path: Path) -> dict[str, Any]:
    return generate_workspace(tmp_path / "data", seed=20261120, count=28)


@pytest.fixture
def config(generated: dict[str, Any]) -> dict[str, Any]:
    value, _ = read_config(generated["config_path"])
    return value


def write_config_variant(
    path: Path,
    base: dict[str, Any],
    **updates: Any,
) -> Path:
    import copy

    config = copy.deepcopy(base)
    for dotted, value in updates.items():
        cursor = config
        parts = dotted.split(".")
        for part in parts[:-1]:
            cursor = cursor[part]
        cursor[parts[-1]] = value
    write_config(path, config)
    return path


def config_json(path: str | Path) -> dict[str, Any]:
    return read_json(path)
