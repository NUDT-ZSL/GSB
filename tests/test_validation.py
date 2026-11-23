from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from eventbus.errors import BusinessError
from eventbus.util import canonical_json, digest_value, read_json
from eventbus.validation import load_dataset

from conftest import ROOT, SAMPLES, make_dataset, run_cli


def test_load_merges_two_generations_with_expected_decisions() -> None:
    dataset = make_dataset()
    assert len(dataset.events) == 8
    decisions = {(item["channel"], item["sequence"]): item for item in dataset.merge_decisions}
    assert decisions[("orders", 1)]["selected_source"] == "v2"
    assert "generation 2 > 1" in decisions[("orders", 1)]["decision_basis"]
    assert decisions[("orders", 2)]["selected_source"] == "v2"
    assert decisions[("orders", 3)]["selected_source"] == "v1"
    assert decisions[("orders", 5)]["selected_source"] == "v1"
    assert decisions[("orders", 5)]["logical_clock"] == 150
    assert decisions[("audit", 1)]["selected_source"] == "v2"


def test_v1_time_conversion_and_compatibility_degradations() -> None:
    dataset = make_dataset()
    events = {
        (item.channel, item.sequence): item for item in dataset.events
    }
    seq3 = events[("orders", 3)]
    assert seq3.time_conversion == {
        "source_relative_seconds": 2,
        "scale": "seconds*1000",
        "rounding_direction": "none",
        "rounding_remainder_ms": 0,
        "epoch_ms": 1_700_000_000_000,
        "absolute_ms": 1_700_000_002_000,
    }
    assert seq3.fields["currency"] == "CNY"
    assert seq3.fields["version_tag"] == "v2-only"
    degraded_fields = {
        (item["channel"], item["sequence"], item["field"])
        for item in dataset.degradations
    }
    assert ("orders", 3, "currency") in degraded_fields
    assert ("orders", 3, "version_tag") in degraded_fields
    assert ("orders", 1, "currency") not in degraded_fields


def test_invalid_field_count_does_not_create_database(tmp_path: Path) -> None:
    db_path = tmp_path / "never.sqlite"
    result = run_cli(
        db_path,
        "load",
        "--v1",
        str(SAMPLES / "invalid" / "field_count.json"),
        "--v2-shards",
        str(SAMPLES / "v2" / "shards"),
        "--v2-metadata",
        str(SAMPLES / "v2" / "metadata.sqlite"),
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "FIELD_COUNT_MISMATCH"
    assert not db_path.exists()


def test_bridge_cycle_is_rejected_before_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "never.sqlite"
    result = run_cli(
        db_path,
        "load",
        "--v1",
        str(SAMPLES / "invalid" / "bridge_cycle.json"),
        "--v2-shards",
        str(SAMPLES / "v2" / "shards"),
        "--v2-metadata",
        str(SAMPLES / "v2" / "metadata.sqlite"),
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "BRIDGE_CYCLE"
    assert not db_path.exists()


def test_subscription_range_is_validated(tmp_path: Path) -> None:
    v1 = read_json(SAMPLES / "v1" / "events.json")
    v1["channels"][0]["subscriptions"].append(
        {"name": "too-high-v1-only", "start_offset": 999}
    )
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(v1), encoding="utf-8")
    with pytest.raises(BusinessError) as captured:
        make_dataset(v1=path)
    assert captured.value.code == "SUBSCRIPTION_RANGE"


def test_duplicate_sequence_is_rejected(tmp_path: Path) -> None:
    v1 = read_json(SAMPLES / "v1" / "events.json")
    v1["channels"][0]["events"][1]["sequence"] = 1
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(v1), encoding="utf-8")
    with pytest.raises(BusinessError) as captured:
        make_dataset(v1=path)
    assert captured.value.code == "SEQUENCE_DUPLICATE"


def test_shard_hash_mismatch_has_distinct_code(tmp_path: Path) -> None:
    shards = tmp_path / "shards"
    shutil.copytree(SAMPLES / "v2" / "shards", shards)
    target = shards / "orders-000.json"
    shard = read_json(target)
    shard["events"][0]["fields"]["amount"] = 999
    target.write_text(json.dumps(shard), encoding="utf-8")
    with pytest.raises(BusinessError) as captured:
        make_dataset(shards=shards)
    assert captured.value.code == "SHARD_HASH_MISMATCH"


def test_v2_field_checksum_mismatch_has_distinct_code(tmp_path: Path) -> None:
    shards = tmp_path / "shards"
    shutil.copytree(SAMPLES / "v2" / "shards", shards)
    target = shards / "orders-000.json"
    shard = read_json(target)
    shard["events"][0]["fields"]["amount"] = 999
    shard["checksum"] = digest_value(
        {key: value for key, value in shard.items() if key != "checksum"}
    )
    target.write_text(json.dumps(shard), encoding="utf-8")
    with pytest.raises(BusinessError) as captured:
        make_dataset(shards=shards)
    assert captured.value.code == "V2_CHECKSUM_MISMATCH"
