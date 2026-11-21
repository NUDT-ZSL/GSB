from __future__ import annotations

import json
from pathlib import Path

import pytest

from wadiag.errors import EngineError
from wadiag.validation import (
    parse_diagnostic_config,
    parse_storage_config,
    parse_trajectory,
)

from .conftest import SAMPLES, cli_json, run_cli


def _trajectory_payload() -> dict:
    return json.loads((SAMPLES / "write_trajectory.json").read_text(encoding="utf-8"))


def _storage_payload() -> dict:
    return json.loads((SAMPLES / "storage_config.json").read_text(encoding="utf-8"))


def _diagnostic_payload() -> dict:
    return json.loads((SAMPLES / "diagnostic_config.json").read_text(encoding="utf-8"))


def test_valid_samples_parse() -> None:
    trajectory = parse_trajectory(_trajectory_payload())
    storage = parse_storage_config(_storage_payload())
    diagnostic = parse_diagnostic_config(_diagnostic_payload())
    assert len(trajectory.records) == 36
    assert trajectory.records[0].seq == 1
    assert trajectory.records[-1].seq == 36
    assert storage.level_count == 4
    assert diagnostic.fixed_scale == 6


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda data: data["records"].__setitem__(3, {**data["records"][3], "seq": 3}), "E_BAD_TRAJECTORY"),
        (lambda data: data["records"].__setitem__(0, {**data["records"][0], "key": "bad key"}), "E_BAD_TRAJECTORY"),
        (lambda data: data["records"].__setitem__(0, {**data["records"][0], "value_bytes": -1}), "E_BAD_TRAJECTORY"),
        (lambda data: data["records"].__setitem__(0, {**data["records"][0], "op": "merge"}), "E_BAD_TRAJECTORY"),
        (lambda data: data["records"].__setitem__(0, {**data["records"][0], "clock_ms": -1}), "E_BAD_TRAJECTORY"),
    ],
)
def test_bad_trajectory_codes(mutate, expected_code: str) -> None:
    payload = _trajectory_payload()
    mutate(payload)
    with pytest.raises(EngineError) as exc:
        parse_trajectory(payload)
    assert exc.value.code == expected_code


def test_bad_levels_and_storage_codes() -> None:
    storage = _storage_payload()
    storage["level_trigger_thresholds"] = [2, 2]
    with pytest.raises(EngineError) as exc:
        parse_storage_config(storage)
    assert exc.value.code == "E_BAD_LEVELS"

    storage = _storage_payload()
    storage["memtable_capacity_bytes"] = 0
    with pytest.raises(EngineError) as exc:
        parse_storage_config(storage)
    assert exc.value.code == "E_BAD_STORAGE"


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("fixed_scale", -1, "E_BAD_SCALE"),
        ("fixed_scale", 13, "E_BAD_SCALE"),
        ("float_tolerance", "0", "E_BAD_TOLERANCE"),
        ("float_tolerance", "NaN", "E_BAD_TOLERANCE"),
        ("error_budget", "-0.1", "E_BAD_BUDGET"),
        ("sample_ratio", "0", "E_BAD_SAMPLE"),
        ("sample_ratio", "1.1", "E_BAD_SAMPLE"),
    ],
)
def test_bad_diagnostic_codes(field: str, value, expected_code: str) -> None:
    payload = _diagnostic_payload()
    payload[field] = value
    with pytest.raises(EngineError) as exc:
        parse_diagnostic_config(payload)
    assert exc.value.code == expected_code


def test_invalid_load_does_not_create_state(tmp_path: Path) -> None:
    trajectory = _trajectory_payload()
    trajectory["records"][2]["seq"] = 2
    trajectory_path = tmp_path / "bad_trajectory.json"
    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
    workspace = tmp_path / "must_not_exist"
    result = run_cli(
        workspace,
        "load",
        "--trajectory",
        str(trajectory_path),
        "--storage",
        str(SAMPLES / "storage_config.json"),
        "--diagnostic",
        str(SAMPLES / "diagnostic_config.json"),
        "--clock-table",
        str(SAMPLES / "clock_steps.json"),
    )
    assert result.returncode == 2
    payload = cli_json(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_BAD_TRAJECTORY"
    assert not workspace.exists()


def test_invalid_load_does_not_touch_existing_state(tmp_path: Path) -> None:
    storage = _storage_payload()
    storage["level_trigger_thresholds"] = [1]
    storage_path = tmp_path / "bad_storage.json"
    storage_path.write_text(json.dumps(storage), encoding="utf-8")
    workspace = tmp_path / "existing"
    workspace.mkdir()
    sentinel = workspace / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    result = run_cli(
        workspace,
        "load",
        "--trajectory",
        str(SAMPLES / "write_trajectory.json"),
        "--storage",
        str(storage_path),
        "--diagnostic",
        str(SAMPLES / "diagnostic_config.json"),
    )
    assert result.returncode == 2
    assert cli_json(result)["error"]["code"] == "E_BAD_LEVELS"
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not (workspace / "state.sqlite3").exists()
