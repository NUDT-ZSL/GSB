from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kdrift.store import StateStore
from kdrift.validation import load_bundle

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sample_bundle(
    directory: Path = SAMPLES,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[Any],
]:
    expected = read_json(directory / "expected.json")
    actual = read_json(directory / "actual.json")
    config = read_json(directory / "config.json")
    desired, actual_resources, engine_config, issues = load_bundle(
        expected, actual, config
    )
    return (
        desired,
        actual_resources,
        engine_config,
        config,
        issues,
    )


def initialized_store(
    path: Path, directory: Path = SAMPLES
) -> StateStore:
    desired, actual, config, _, issues = sample_bundle(directory)
    store = StateStore(str(path))
    store.initialize(desired, actual, config, issues)
    return store

