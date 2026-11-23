import json
import os

import pytest

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")


def _load(name):
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def tables_payload():
    return _load("tables.json")


@pytest.fixture()
def plan_payload():
    return _load("plan.json")


@pytest.fixture()
def batches_payload():
    return _load("batches.json")


@pytest.fixture()
def config_payload():
    return _load("config.json")


@pytest.fixture()
def loaded_state(tmp_path, tables_payload, plan_payload, batches_payload, config_payload):
    from reopt.runner import Runner
    state_dir = str(tmp_path / "state")
    runner = Runner.load(state_dir, tables_payload, plan_payload,
                         batches_payload, config_payload)
    runner.store.close()
    return state_dir
