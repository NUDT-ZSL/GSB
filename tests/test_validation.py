from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from capbench.cli import main
from capbench.errors import WorkbenchError
from capbench.scenario import parse_scenario


def _replace_with_short_clock(raw: dict) -> None:
    raw.pop("global_time_budget_ms", None)
    raw["logical_clock"] = {"tick_ms": 10, "global_budget_ms": 1000}


def test_valid_demo_scenario(demo_raw: dict) -> None:
    scenario = parse_scenario(copy.deepcopy(demo_raw))
    assert len(scenario.services) == 1
    assert [tenant.name for tenant in scenario.tenants] == ["alpha", "beta", "gamma"]
    assert scenario.global_time_budget_ms == 27000
    assert scenario.plan_map["alpha"].gradients[-1].concurrency == 20


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda raw: raw["plans"][0].__setitem__(
                "concurrency_gradients",
                [5, 5, 15],
            ),
            "E_BAD_PLAN",
        ),
        (
            lambda raw: raw["plans"][0].__setitem__("pace_ms", 0),
            "E_BAD_PLAN",
        ),
        (
            lambda raw: raw["plans"][0].__setitem__("duration_ms", -1),
            "E_BAD_PLAN",
        ),
        (
            lambda raw: raw["services"][0].__setitem__("host", "8.8.8.8"),
            "E_BAD_SERVICE",
        ),
        (
            lambda raw: raw["services"][0]["tenants"][0]["slo"].__setitem__(
                "min_success_rate",
                1.5,
            ),
            "E_BAD_SLO",
        ),
        (
            lambda raw: raw["services"][0]["tenants"][1].__setitem__(
                "name",
                "alpha",
            ),
            "E_BAD_SERVICE",
        ),
        (
            _replace_with_short_clock,
            "E_BAD_CLOCK",
        ),
    ],
)
def test_invalid_scenario_business_codes(
    demo_raw: dict,
    mutate,
    code: str,
) -> None:
    raw = copy.deepcopy(demo_raw)
    mutate(raw)
    with pytest.raises(WorkbenchError) as exc_info:
        parse_scenario(raw)
    assert exc_info.value.code == code


def test_cli_invalid_scenario_writes_no_database(
    demo_raw: dict,
    tmp_path: Path,
) -> None:
    raw = copy.deepcopy(demo_raw)
    raw["plans"][0]["concurrency_gradients"] = [5, 4, 3]
    scenario_path = tmp_path / "invalid.json"
    scenario_path.write_text(json.dumps(raw), encoding="utf-8")
    db_path = tmp_path / "must-not-exist.sqlite"
    code = main(
        [
            "run",
            "--scenario",
            str(scenario_path),
            "--db",
            str(db_path),
            "--transport",
            "simulated",
        ]
    )
    assert code == 2
    assert not db_path.exists()
    assert not db_path.with_name(db_path.name + ".lock").exists()


def test_duplicate_batch_id_rejected(demo_raw: dict) -> None:
    raw = copy.deepcopy(demo_raw)
    raw["plans"][1]["batch_id"] = raw["plans"][0]["batch_id"]
    with pytest.raises(WorkbenchError) as exc_info:
        parse_scenario(raw)
    assert exc_info.value.code == "E_BAD_PLAN"

