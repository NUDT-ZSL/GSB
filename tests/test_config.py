from __future__ import annotations

import copy
import json

import pytest

from relief_workbench.cli import main
from relief_workbench.config import config_from_dict, load_config, sample_config_dict
from relief_workbench.errors import WorkbenchError


def _base() -> dict:
    return copy.deepcopy(sample_config_dict())


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda data: data["canvas"].update({"width": 0}), "E_CANVAS"),
        (lambda data: data["canvas"].update({"width": 511}), "E_GRID_DIVISIBILITY"),
        (lambda data: data["grid"].update({"rows": 5}), "E_GRID_DIVISIBILITY"),
        (lambda data: data.update({"seed": -1}), "E_SEED"),
        (lambda data: data.update({"seed": True}), "E_SEED"),
        (lambda data: data.update({"octaves": []}), "E_OCTAVES"),
        (
            lambda data: data["octaves"][2].update(
                {"frequency": data["octaves"][1]["frequency"]}
            ),
            "E_FREQUENCY_ORDER",
        ),
        (
            lambda data: data["octaves"][0].update({"amplitude": 0}),
            "E_AMPLITUDE",
        ),
        (
            lambda data: data["light"].update({"direction": [1.0, 1.0, 1.0]}),
            "E_LIGHT_UNIT",
        ),
        (
            lambda data: data["light"].update({"relief_strength": 0}),
            "E_RELIEF_STRENGTH",
        ),
        (
            lambda data: data.update({"normalization_tolerance": 0}),
            "E_TOLERANCE",
        ),
        (lambda data: data.update({"concurrency": 0}), "E_CONCURRENCY"),
        (
            lambda data: data.update({"memory_budget_mb": 0}),
            "E_MEMORY_BUDGET",
        ),
        (
            lambda data: data.update({"preview_tile": [9, 0]}),
            "E_PREVIEW_TILE",
        ),
    ],
)
def test_validation_codes(mutate, expected_code) -> None:
    data = _base()
    mutate(data)
    with pytest.raises(WorkbenchError) as caught:
        config_from_dict(data)
    assert caught.value.code == expected_code


def test_valid_config_and_stable_version(tmp_path) -> None:
    data = _base()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    first = load_config(path)
    second = load_config(path)
    assert first.version == second.version
    assert first.tile_width == 128
    assert first.tile_height == 96


def test_invalid_json_no_output_files(tmp_path, capsys) -> None:
    config_path = tmp_path / "bad.json"
    config_path.write_text("{not-json", encoding="utf-8")
    state = tmp_path / "state"
    output = tmp_path / "output"
    exit_code = main(
        [
            "render",
            "--config",
            str(config_path),
            "--state",
            str(state),
            "--output",
            str(output),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert payload["error"]["code"] == "E_CONFIG_JSON"
    assert not state.exists()
    assert not output.exists()


def test_invalid_business_value_no_output_files(tmp_path, capsys) -> None:
    data = _base()
    data["light"]["direction"] = [0.2, 0.2, 0.2]
    config_path = tmp_path / "bad.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    state = tmp_path / "state"
    output = tmp_path / "output"
    exit_code = main(
        [
            "render",
            "--config",
            str(config_path),
            "--state",
            str(state),
            "--output",
            str(output),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert payload["error"]["code"] == "E_LIGHT_UNIT"
    assert not state.exists()
    assert not output.exists()
