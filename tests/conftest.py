from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tactical_engine.scenario import parse_scenario


def simple_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scenario_id": "unit-test-scene",
        "terrain": {
            "width": 5,
            "height": 5,
            "costs": [[1.0 for _ in range(5)] for _ in range(5)],
            "blocked": [],
        },
        "search": {
            "heuristic_weight": 1.0,
            "heuristic_metric": "manhattan",
            "diagonal_rule": "never",
            "diagonal_cost_factor": 1.4142135623730951,
        },
        "precision": {
            "fixed_scale": 1000,
            "tolerance": 0.01,
            "budget_policy": "reachable",
        },
        "smoothing": {
            "enabled": True,
            "algorithm": "line_of_sight",
            "max_lookahead": 0,
        },
        "resources": {"concurrency": 3, "memory_budget_bytes": 2 * 1024 * 1024},
        "units": [
            {
                "id": "u1",
                "name": "unit one",
                "start": [0, 0],
                "goal": [4, 4],
                "budget": 8.0,
                "candidates": [
                    {"id": "a", "cell": [4, 4]},
                    {"id": "b", "cell": [3, 4]},
                ],
            }
        ],
        "replay": [
            {"event": "move", "unit_id": "u1", "to": [4, 4]},
            {"event": "arrive", "unit_id": "u1", "at": [4, 4]},
        ],
    }


@pytest.fixture
def simple_raw() -> dict[str, Any]:
    return copy.deepcopy(simple_payload())


@pytest.fixture
def simple_scenario(simple_raw: dict[str, Any]):
    return parse_scenario(simple_raw)


@pytest.fixture
def sample_scenario():
    from tactical_engine.sample_data import build_sample_payload

    return parse_scenario(build_sample_payload())


def write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
