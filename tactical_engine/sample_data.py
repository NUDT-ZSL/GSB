from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .planner import Planner
from .scenario import parse_scenario


def _json_number(value: Decimal) -> float:
    return float(value)


def _terrain_payload() -> dict[str, Any]:
    width = 13
    height = 11
    costs: list[list[float]] = []
    for row in range(height):
        values: list[float] = []
        for col in range(width):
            hundredths = 80 + ((row * 13 + col * 7 + 3) % 17) * 10
            values.append(hundredths / 100.0)
        costs.append(values)
    blocked = [
        [2, 8],
        [2, 9],
        [3, 4],
        [3, 5],
        [4, 4],
        [5, 4],
        [5, 5],
        [6, 10],
        [7, 8],
        [7, 9],
        [8, 3],
        [8, 4],
        [9, 1],
    ]
    return {"width": width, "height": height, "costs": costs, "blocked": blocked}


def _base_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scenario_id": "deterministic-tactical-sample-v1",
        "terrain": _terrain_payload(),
        "search": {
            "heuristic_weight": 1.35,
            "heuristic_metric": "octile",
            "diagonal_rule": "no_corner_cutting",
            "diagonal_cost_factor": 1.4142135623730951,
        },
        "precision": {
            "fixed_scale": 1000,
            "tolerance": 0.05,
            "budget_policy": "reachable",
        },
        "smoothing": {
            "enabled": True,
            "algorithm": "line_of_sight",
            "max_lookahead": 0,
        },
        "resources": {
            "concurrency": 4,
            "memory_budget_bytes": 8 * 1024 * 1024,
        },
        "units": [
            {
                "id": "alpha",
                "name": "先锋队",
                "start": [1, 1],
                "goal": [8, 11],
                "budget": 10000.0,
                "candidates": [
                    {"id": "north_relay", "cell": [6, 11]},
                    {"id": "east_flank", "cell": [8, 11]},
                    {"id": "south_relay", "cell": [9, 10]},
                ],
            },
            {
                "id": "bravo",
                "name": "侦察组",
                "start": [9, 7],
                "goal": [2, 11],
                "budget": 10000.0,
                "candidates": [
                    {"id": "north_pass", "cell": [2, 11]},
                    {"id": "west_cover", "cell": [2, 7]},
                    {"id": "ridge", "cell": [4, 11]},
                ],
            },
        ],
        "replay": [
            {"event": "move", "unit_id": "alpha", "to": [8, 11]},
            {"event": "arrive", "unit_id": "alpha", "at": [8, 11]},
            {"event": "move", "unit_id": "bravo", "to": [2, 11]},
            {"event": "arrive", "unit_id": "bravo", "at": [2, 11]},
            {"event": "budget_exhausted", "unit_id": "alpha"},
        ],
    }


def build_sample_payload() -> dict[str, Any]:
    payload = _base_payload()
    initial = parse_scenario(json.loads(json.dumps(payload)))
    planner = Planner(initial)
    tolerance = initial.precision.tolerance
    for unit_raw, unit in zip(payload["units"], initial.units):
        plan = planner.plan_unit(unit)
        boundary_budget = (
            Decimal(plan.cost.fixed_total) / initial.precision.scale + tolerance / 2
        )
        unit_raw["budget"] = _json_number(boundary_budget)
    return payload


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def generate_sample(destination: str | Path) -> dict[str, Path]:
    destination_path = Path(destination)
    payload = build_sample_payload()
    destination_path.mkdir(parents=True, exist_ok=True)
    combined = destination_path / "sample_scenario.json"
    terrain = destination_path / "terrain.json"
    units = destination_path / "units.json"
    replay = destination_path / "replay.json"
    options = destination_path / "scene_options.json"
    _dump(combined, payload)
    _dump(terrain, payload["terrain"])
    _dump(units, {"units": payload["units"]})
    _dump(replay, {"replay": payload["replay"]})
    _dump(
        options,
        {
            key: payload[key]
            for key in (
                "schema_version",
                "scenario_id",
                "search",
                "precision",
                "smoothing",
                "resources",
            )
        },
    )
    return {
        "scenario": combined,
        "terrain": terrain,
        "units": units,
        "replay": replay,
        "options": options,
    }
