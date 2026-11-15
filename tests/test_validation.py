from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tactical_engine.errors import EngineError
from tactical_engine.pipeline import run_scenario_files
from tactical_engine.scenario import parse_scenario



def write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda raw: raw["terrain"].update(width=0), "E_GRID_SIZE"),
        (lambda raw: raw["terrain"].update(height=-2), "E_GRID_SIZE"),
        (lambda raw: raw["terrain"]["costs"].pop(), "E_GRID_SHAPE"),
        (lambda raw: raw["terrain"]["costs"][0].pop(), "E_GRID_SHAPE"),
        (lambda raw: raw["terrain"]["costs"][0].__setitem__(0, 0), "E_TERRAIN_COST"),
        (lambda raw: raw["terrain"]["blocked"].append([8, 1]), "E_BLOCKED_COORD"),
        (lambda raw: raw["terrain"]["blocked"].append([0, 0]), "E_POINT_BLOCKED"),
        (lambda raw: raw["units"][0].update(goal=[0, 0]), "E_POINT_OVERLAP"),
        (lambda raw: raw["search"].update(heuristic_weight=-1), "E_HEURISTIC_WEIGHT"),
        (lambda raw: raw["precision"].update(fixed_scale=0), "E_SCALE"),
        (lambda raw: raw["precision"].update(tolerance=0), "E_TOLERANCE"),
        (
            lambda raw: raw["units"][0]["candidates"][1].update(id="a"),
            "E_CANDIDATE_ID",
        ),
        (
            lambda raw: raw["replay"].append(
                {"event": "move", "unit_id": "u1", "to": [9, 9]}
            ),
            "E_POINT_COORD",
        ),
    ],
)
def test_business_validation_codes(simple_raw, mutator, code):
    mutator(simple_raw)
    with pytest.raises(EngineError) as caught:
        parse_scenario(simple_raw)
    assert caught.value.code == code


def test_invalid_scenario_produces_no_artifacts(simple_raw, tmp_path):
    simple_raw["terrain"]["width"] = 0
    scenario_path = write_json(tmp_path / "bad.json", simple_raw)
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    with pytest.raises(EngineError) as caught:
        run_scenario_files(scenario_path, output_dir, state_dir)
    assert caught.value.code == "E_GRID_SIZE"
    assert not any(output_dir.iterdir())
    assert not any(state_dir.iterdir())


def test_invalid_json_is_reported_without_artifacts(tmp_path):
    scenario_path = tmp_path / "bad.json"
    scenario_path.write_text("{not-json", encoding="utf-8")
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    with pytest.raises(EngineError) as caught:
        run_scenario_files(scenario_path, output_dir, state_dir)
    assert caught.value.code == "E_JSON_INVALID"
    assert not any(output_dir.iterdir())
    assert not any(state_dir.iterdir())


def test_duplicate_unit_ids_rejected(simple_raw):
    duplicate = copy.deepcopy(simple_raw["units"][0])
    duplicate["id"] = "u1"
    simple_raw["units"].append(duplicate)
    with pytest.raises(EngineError) as caught:
        parse_scenario(simple_raw)
    assert caught.value.code == "E_UNIT_ID"
