from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coref_pipeline.generator import generate_sample


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "sample"
    generate_sample(path, force=True)
    return path


@pytest.fixture()
def sample_events(sample_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (sample_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture()
def rules_payload(sample_dir: Path) -> dict:
    return json.loads((sample_dir / "coref_rules.json").read_text(encoding="utf-8"))


def write_config(path: Path, payload: dict) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def write_events(path: Path, events: list[dict]) -> Path:
    lines = [
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for event in events
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def mutate_event(sample_events):
    def _mutate(event_id: str, mutator):
        events = copy.deepcopy(sample_events)
        for event in events:
            if event["event_id"] == event_id:
                mutator(event)
                return events
        raise AssertionError(event_id)

    return _mutate
