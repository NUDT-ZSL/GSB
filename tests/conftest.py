from __future__ import annotations

import pytest

from supply_chain_audit.generator import generate_seed_workspace
from supply_chain_audit.parsing import load_clock, load_dependencies, load_snapshot
from supply_chain_audit.service import build_seed_engine


@pytest.fixture()
def sample_paths(tmp_path):
    root = tmp_path / "seed"
    paths = generate_seed_workspace(root)
    return paths


@pytest.fixture()
def clock(sample_paths):
    return load_clock(sample_paths["clock"])


@pytest.fixture()
def dependencies(sample_paths):
    return load_dependencies(sample_paths["dependencies"])


@pytest.fixture()
def parsed_v1(sample_paths, clock):
    return load_snapshot(sample_paths["v1"], clock)


@pytest.fixture()
def parsed_v2(sample_paths, clock):
    return load_snapshot(sample_paths["v2_manifest"], clock)


@pytest.fixture()
def engine(sample_paths):
    return build_seed_engine(sample_paths["root"])
