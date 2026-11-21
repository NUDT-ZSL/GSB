from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from capbench.scenario import Scenario, parse_scenario


ROOT = Path(__file__).resolve().parents[1]
DEMO_SCENARIO = ROOT / "scenarios" / "demo.json"


@pytest.fixture()
def demo_raw() -> dict:
    return json.loads(DEMO_SCENARIO.read_text(encoding="utf-8"))


@pytest.fixture()
def demo_scenario(demo_raw: dict) -> Scenario:
    return parse_scenario(copy.deepcopy(demo_raw))

