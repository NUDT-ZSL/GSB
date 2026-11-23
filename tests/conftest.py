from __future__ import annotations

import copy
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scaler.loader import load_bundle
from scaler.models import ScalerSpec
from scaler.stub import StubServer, load_scenario


@pytest.fixture
def project_root() -> Path:
    return ROOT


@pytest.fixture
def base_spec() -> ScalerSpec:
    return load_bundle(
        ROOT / "samples/services.json",
        ROOT / "samples/nodes.json",
        ROOT / "samples/trace.json",
        ROOT / "samples/config.json",
    )


def fast_scenario(*, slow_gamma: bool = False) -> dict:
    scenario = copy.deepcopy(load_scenario(ROOT / "samples/scenario.json"))
    for behavior in scenario["services"].values():
        behavior["delay_ms"] = 100 if slow_gamma and behavior.get("then", {}).get("mode") == "health_fail" else 2
        if "then" in behavior:
            behavior["then"]["delay_ms"] = 2
    return scenario


@pytest.fixture
def stub():
    server = StubServer(port=0, scenario=fast_scenario())
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def spec(stub, base_spec) -> ScalerSpec:
    return replace(
        base_spec,
        config=replace(base_spec.config, launch_target_url=stub.url),
    )
