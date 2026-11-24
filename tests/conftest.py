from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from haptic_engine.engine import load_bundle_from_workspace
from haptic_engine.samples import generate_documents


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def make_workspace(
    path: Path,
    seed: int = 20260214,
    config_updates: dict[str, Any] | None = None,
    mutate: Callable[[dict, dict, dict], None] | None = None,
) -> dict[str, Any]:
    devices_doc, events_doc, config_doc = generate_documents(seed)
    devices_doc = copy.deepcopy(devices_doc)
    events_doc = copy.deepcopy(events_doc)
    config_doc = copy.deepcopy(config_doc)
    if config_updates:
        config_doc.update(config_updates)
    if mutate:
        mutate(devices_doc, events_doc, config_doc)
    path.mkdir(parents=True, exist_ok=True)
    write_json(path / "devices.json", devices_doc)
    write_json(path / "events.json", events_doc)
    write_json(path / "config.json", config_doc)
    return {
        "devices": devices_doc,
        "events": events_doc,
        "config": config_doc,
    }


def load_bundle(path: Path):
    return load_bundle_from_workspace(path)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    target = tmp_path / "workspace"
    make_workspace(target)
    return target


@pytest.fixture
def documents() -> tuple[dict, dict, dict]:
    return generate_documents()
