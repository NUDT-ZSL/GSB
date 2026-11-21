from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from netdiag.errors import BusinessError
from netdiag.scenario import parse_scenario
from tests.conftest import ROOT, SAMPLE_SCENARIO, write_scenario


def _base() -> dict:
    return json.loads(SAMPLE_SCENARIO.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda data: data["targets"].append(dict(data["targets"][0])),
            "TARGET_DUPLICATE_NAME",
        ),
        (
            lambda data: data["targets"][0].update(endpoint="http://192.0.2.1:18080/healthy"),
            "TARGET_NON_LOOPBACK",
        ),
        (
            lambda data: data["probe_script"]["rounds"][0]["probes"][1].update(
                seq=data["probe_script"]["rounds"][0]["probes"][0]["seq"]
            ),
            "PROBE_DUPLICATE_SEQ",
        ),
        (
            lambda data: data["targets"][0].update(period_ms=50, timeout_ms=100),
            "TARGET_PERIOD_LT_TIMEOUT",
        ),
        (
            lambda data: data["config"].update(window_size=3, window_step=4),
            "CONFIG_WINDOW_STEP_RANGE",
        ),
        (
            lambda data: data["config"].update(loss_rate_threshold=1.25),
            "CONFIG_LOSS_THRESHOLD_RANGE",
        ),
        (
            lambda data: data["targets"][0].update(timeout_ms=0),
            "TARGET_TIMEOUT_RANGE",
        ),
        (
            lambda data: data["targets"][0].update(period_ms=1000),
            "PROBE_PERIOD_VIOLATION",
        ),
        (
            lambda data: data["probe_script"]["rounds"][0]["probes"][0].update(
                target="missing-target"
            ),
            "PROBE_UNKNOWN_TARGET",
        ),
    ],
)
def test_invalid_scenario_has_business_code(mutate, code: str) -> None:
    data = _base()
    mutate(data)
    with pytest.raises(BusinessError) as exc:
        parse_scenario(data)
    assert exc.value.code == code


def test_invalid_cli_load_does_not_create_state(tmp_path: Path) -> None:
    data = _base()
    data["targets"][0]["endpoint"] = "http://203.0.113.7:18080/healthy"
    scenario_path = write_scenario(tmp_path / "bad.json", data)
    db_path = tmp_path / "must-not-exist.db"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "netdiag",
            "--db",
            str(db_path),
            "load",
            str(scenario_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "TARGET_NON_LOOPBACK"
    assert not db_path.exists()
    assert not (tmp_path / ".must-not-exist.db.locks").exists()


def test_valid_scenario_parses() -> None:
    scenario = parse_scenario(_base())
    assert len(scenario.targets) == 10
    assert len(scenario.rounds) == 8
    assert sum(len(item.probes) for item in scenario.rounds) == 80
    assert scenario.config.max_concurrency == 3
