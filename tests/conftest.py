import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recovery_engine.clock import LogicalClock
from recovery_engine.engine import Engine
from recovery_engine.executor import SimulatedExecutor, SimulatedHealth
from recovery_engine.store import Store

ROOT = os.path.join(os.path.dirname(__file__), "..")
SAMPLES = os.path.join(ROOT, "samples")
V1_PATH = os.path.join(SAMPLES, "v1", "config.json")
V2_DIR = os.path.join(SAMPLES, "v2", "shards")
V2_META = os.path.join(SAMPLES, "v2", "meta.sqlite")
CLOCK_PATH = os.path.join(SAMPLES, "clock_steps.json")


def make_clock(start=10000, steps=400, step_ms=100, tolerance_ms=50):
    return LogicalClock([start + i * step_ms for i in range(steps)],
                        tolerance_ms=tolerance_ms)


@pytest.fixture
def store(tmp_path):
    return Store(os.path.join(str(tmp_path), "state.db"))


@pytest.fixture
def engine(store):
    return Engine(store, make_clock(), SimulatedExecutor(), SimulatedHealth())


@pytest.fixture
def plan(engine):
    return engine.build_plan(V1_PATH, V2_DIR, V2_META)
