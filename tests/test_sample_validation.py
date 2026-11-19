from __future__ import annotations

import json
from pathlib import Path

import pytest

from shardroute.errors import BizError
from shardroute.sample import load_sample

from .conftest import cli_json, read_sample_raw, run_cli


def test_sample_loads_complete_rule_snapshots(sample):
    assert sample.initial_version == "r1"
    assert sorted(sample.rules) == ["r1", "r2", "r3-rollback"]
    assert sorted(sample.query_templates) == [
        "events_eq",
        "events_in",
        "events_range",
        "orders_agg_range",
        "orders_all",
        "orders_eq",
        "orders_in",
        "orders_range",
        "orders_sum_count",
    ]
    assert sample.rules["r1"].snapshot.logical_tables["orders"].shard_count == 4
    assert sample.rules["r2"].snapshot.logical_tables["orders"].shard_count == 2
    assert sample.rules["r1"].snapshot.logical_tables["events"].boundaries == (
        0,
        1000,
        2000,
        3000,
        4000,
    )


def test_bad_rule_digest_writes_no_state(tmp_path: Path):
    raw = read_sample_raw()
    raw["rule_versions"][0]["digest"] = "sha256:" + "0" * 64
    bad_sample = tmp_path / "sample.json"
    bad_sample.write_text(json.dumps(raw), encoding="utf-8")
    state_path = tmp_path / "state.db"
    completed = run_cli(
        "load",
        "--sample",
        str(bad_sample),
        "--clock",
        "1000",
        state_path=state_path,
        check=False,
    )
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["error"]["code"] == "E_BAD_RULE"
    assert not state_path.exists()
    with pytest.raises(BizError) as raised:
        load_sample(bad_sample)
    assert raised.value.code == "E_BAD_RULE"


def test_missing_primary_mapping_writes_no_state(tmp_path: Path):
    raw = read_sample_raw()
    mappings = raw["rule_versions"][0]["physical_mappings"]
    raw["rule_versions"][0]["physical_mappings"] = [
        item
        for item in mappings
        if not (
            item["logical_table"] == "orders"
            and item["shard_id"] == 0
            and item["role"] == "primary"
        )
    ]
    bad_sample = tmp_path / "sample.json"
    bad_sample.write_text(json.dumps(raw), encoding="utf-8")
    state_path = tmp_path / "state.db"
    payload = cli_json(
        "load",
        "--sample",
        str(bad_sample),
        "--clock",
        "1000",
        state_path=state_path,
        check=False,
    )
    assert payload["error"]["code"] == "E_BAD_MAPPING"
    assert not state_path.exists()


def test_missing_physical_database_writes_no_state(tmp_path: Path):
    raw = read_sample_raw()
    bad_sample = tmp_path / "sample.json"
    bad_sample.write_text(json.dumps(raw), encoding="utf-8")
    state_path = tmp_path / "state.db"
    payload = cli_json(
        "load",
        "--sample",
        str(bad_sample),
        "--clock",
        "1000",
        state_path=state_path,
        check=False,
    )
    assert payload["error"]["code"] == "E_BAD_MAPPING"
    assert not state_path.exists()


def test_invalid_range_boundaries_are_rejected(tmp_path: Path):
    raw = read_sample_raw()
    raw["rule_versions"][0]["logical_tables"][1]["boundaries"] = [0, 1000, 500, 3000, 4000]
    bad_sample = tmp_path / "sample.json"
    bad_sample.write_text(json.dumps(raw), encoding="utf-8")
    completed = run_cli(
        "load",
        "--sample",
        str(bad_sample),
        "--clock",
        "1000",
        state_path=tmp_path / "state.db",
        check=False,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["error"]["code"] == "E_BAD_RULE"
    assert not (tmp_path / "state.db").exists()

