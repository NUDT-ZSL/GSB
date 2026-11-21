from __future__ import annotations

import copy

import pytest

from prefworkbench.config import default_config
from prefworkbench.generation import generate_samples


@pytest.fixture()
def config() -> dict:
    return default_config(20261122)


@pytest.fixture()
def samples(config: dict) -> list[dict]:
    return generate_samples(config["seed"], 42)


@pytest.fixture()
def small_samples(config: dict) -> list[dict]:
    return generate_samples(config["seed"], 18)


@pytest.fixture()
def config_copy(config: dict) -> dict:
    return copy.deepcopy(config)

