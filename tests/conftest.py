from __future__ import annotations

import json
from pathlib import Path

import pytest

from smuggling_engine.generator import generate_sample
from smuggling_engine.parsing import group_connections, parse_messages
from smuggling_engine.rules import load_rule_view


@pytest.fixture(scope="session")
def standard_sample(tmp_path_factory):
    directory = tmp_path_factory.mktemp("standard-sample")
    manifest = generate_sample(directory, seed=20261125, scenario="standard")
    truth = json.loads((directory / "sync_truth.json").read_text(encoding="utf-8"))
    return {
        "directory": directory,
        "manifest": manifest,
        "truth": truth,
    }


@pytest.fixture(scope="session")
def cycle_sample(tmp_path_factory):
    directory = tmp_path_factory.mktemp("cycle-sample")
    manifest = generate_sample(directory, seed=20261125, scenario="cycle")
    truth = json.loads((directory / "sync_truth.json").read_text(encoding="utf-8"))
    return {
        "directory": directory,
        "manifest": manifest,
        "truth": truth,
    }


def load_sample(sample):
    directory = Path(sample["directory"])
    paths = sample["manifest"]["paths"]
    messages, rejects = parse_messages(directory / paths["messages"])
    groups, _ = group_connections(messages)
    view = load_rule_view(
        v1_path=directory / paths["rules_v1"],
        v2_shards=[directory / item for item in paths["v2_shards"]],
        v2_database=directory / paths["v2_database"],
    )
    return messages, rejects, groups, view

