from __future__ import annotations

import copy
import json

import pytest

from kubesim.errors import EngineError
from kubesim.models import parse_dataset

from .conftest import CONFIG, NODES, PODS, cli, read_json


def _documents():
    return read_json(NODES), read_json(PODS), read_json(CONFIG)


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (
            lambda nodes, pods, config: nodes["nodes"][0].__setitem__(
                "allocatable", {"cpu_millicores": 0, "memory_mib": 128, "gpu": 0}
            ),
            "E_BAD_NODE",
        ),
        (
            lambda nodes, pods, config: nodes["nodes"][0].__setitem__(
                "taints",
                [{"key": "bad key", "value": "x", "effect": "NoSchedule"}],
            ),
            "E_BAD_NODE",
        ),
        (
            lambda nodes, pods, config: pods["pods"].append(copy.deepcopy(pods["pods"][0])),
            "E_BAD_POD",
        ),
        (
            lambda nodes, pods, config: pods["pods"][0].__setitem__(
                "tolerations",
                [{"key": "x", "operator": "Sometimes", "value": "y"}],
            ),
            "E_BAD_POD",
        ),
        (
            lambda nodes, pods, config: config.__setitem__(
                "filters", ["resource_feasibility", "not_a_plugin"]
            ),
            "E_BAD_CONFIG",
        ),
        (
            lambda nodes, pods, config: config.__setitem__("total_time_budget_ms", 0),
            "E_BAD_CONFIG",
        ),
        (
            lambda nodes, pods, config: config["scores"].__setitem__(
                "resource_bin_packing", -1
            ),
            "E_BAD_CONFIG",
        ),
    ],
)
def test_invalid_inputs_return_stable_business_codes(mutator, expected_code):
    nodes, pods, config = _documents()
    mutator(nodes, pods, config)
    with pytest.raises(EngineError) as caught:
        parse_dataset(nodes, pods, config, None)
    assert caught.value.code == expected_code


def test_invalid_cli_load_does_not_create_database(tmp_path):
    nodes = read_json(NODES)
    nodes["nodes"][0]["allocatable"]["cpu_millicores"] = -1
    bad_nodes = tmp_path / "bad_nodes.json"
    bad_nodes.write_text(json.dumps(nodes), encoding="utf-8")
    db_path = tmp_path / "should-not-exist.sqlite"
    result = cli(
        "load",
        "--db",
        str(db_path),
        "--nodes",
        str(bad_nodes),
        "--pods",
        str(PODS),
        "--config",
        str(CONFIG),
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == "E_BAD_NODE"
    assert not db_path.exists()
