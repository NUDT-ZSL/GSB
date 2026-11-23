import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alertcorr.engine import Engine

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "samples")


def load_sample(name):
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture()
def topo_dict():
    return load_sample("topology.json")


@pytest.fixture()
def events_list():
    return load_sample("events.json")["events"]


@pytest.fixture()
def patches():
    return load_sample("patches.json")["patches"]


@pytest.fixture()
def engine(tmp_path):
    eng = Engine(str(tmp_path / "test.db"))
    yield eng
    eng.close()


@pytest.fixture()
def loaded_engine(engine, topo_dict, events_list):
    engine.load_topology(topo_dict)
    engine.ingest(events_list)
    engine.run()
    return engine
