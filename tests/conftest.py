"""Shared fixtures: deterministic samples, store, clock, loopback target."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samples.gen_samples import generate  # noqa: E402
from recovery_pipeline.clock import LogicalClock  # noqa: E402
from recovery_pipeline.http_target import TargetServer  # noqa: E402
from recovery_pipeline.merge import merge_configs  # noqa: E402
from recovery_pipeline.store import Store  # noqa: E402
from recovery_pipeline.v1_loader import load_v1  # noqa: E402
from recovery_pipeline.v2_loader import load_v2  # noqa: E402

CLOCK_BASE = 1_000_000
CLOCK_SEQ = list(range(CLOCK_BASE, CLOCK_BASE + 1000 * 100, 100))


def make_clock(tolerance_ms: int = 0) -> LogicalClock:
    return LogicalClock(CLOCK_SEQ, rollback_tolerance_ms=tolerance_ms)


def build_plan(samples: dict, clock_base: int = CLOCK_BASE):
    v1 = load_v1(samples["v1"])
    v2 = load_v2(samples["v2"])
    return merge_configs(v1, v2, clock_base), v1, v2


@pytest.fixture()
def samples(tmp_path):
    return generate(tmp_path / "samples")


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "state" / "pipeline.sqlite3")


@pytest.fixture()
def target():
    with TargetServer() as srv:
        yield srv
