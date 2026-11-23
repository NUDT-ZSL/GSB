from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import (
    load_json,
    mutate_v2_manifest_and_metadata,
    update_shard_hashes,
    update_v2_node,
    write_json,
    write_shard,
)
from graphstore.errors import ErrorCode, GraphStoreError
from graphstore.snapshot import load_snapshots, load_v1, load_v2


def _v1_error(paths, code):
    with pytest.raises(GraphStoreError) as caught:
        load_v1(paths["v1"], logical_clock_ms=paths["logical_clock_ms"])
    assert caught.value.code == code


def test_v1_accepts_sample_and_records_conversion(sample_paths):
    v1 = load_v1(
        sample_paths["v1"],
        logical_clock_ms=sample_paths["logical_clock_ms"],
    )
    assert len(v1.nodes) == 9
    assert v1.linear_index == tuple(sorted(v1.nodes))
    conversion = v1.nodes["n1"].conversion
    assert conversion["raw_written_seconds"] == 1
    assert conversion["scale_ms_per_second"] == 1000
    assert conversion["rounding"] == "half_up"
    assert conversion["pre_round_ms"] == 1_700_000_001_000
    assert conversion["write_ms"] == 1_700_000_001_000


def test_v1_duplicate_node_id(sample_paths):
    snapshot = load_json(sample_paths["v1"])
    snapshot["nodes"].append(dict(snapshot["nodes"][0]))
    write_json(sample_paths["v1"], snapshot)
    _v1_error(sample_paths, ErrorCode.V1_DUPLICATE_NODE_ID)


def test_v1_adjacency_mismatch(sample_paths):
    snapshot = load_json(sample_paths["v1"])
    node = next(item for item in snapshot["nodes"] if item["id"] == "n1")
    node["adjacency"]["labels"][0] = "likes"
    write_json(sample_paths["v1"], snapshot)
    _v1_error(sample_paths, ErrorCode.V1_ADJACENCY_MISMATCH)


def test_v1_invalid_weight_including_nan(sample_paths):
    snapshot = load_json(sample_paths["v1"])
    node = next(item for item in snapshot["nodes"] if item["id"] == "n2")
    node["weight"] = float("nan")
    sample_paths["v1"].write_text(json.dumps(snapshot), encoding="utf-8")
    _v1_error(sample_paths, ErrorCode.V1_INVALID_WEIGHT)


def test_v1_invalid_written_seconds_and_index(sample_paths):
    snapshot = load_json(sample_paths["v1"])
    node = next(item for item in snapshot["nodes"] if item["id"] == "n2")
    node["written_seconds"] = True
    write_json(sample_paths["v1"], snapshot)
    _v1_error(sample_paths, ErrorCode.V1_INVALID_WRITTEN_SECONDS)

    snapshot = load_json(sample_paths["v1"])
    node = next(item for item in snapshot["nodes"] if item["id"] == "n2")
    node["written_seconds"] = 2
    snapshot["linear_index"] = list(reversed(snapshot["linear_index"]))
    write_json(sample_paths["v1"], snapshot)
    _v1_error(sample_paths, ErrorCode.V1_INDEX_MISMATCH)


def test_v2_accepts_sample(sample_paths):
    v2 = load_v2(sample_paths["v2"], known_existing_ids={"n3"})
    assert v2.version == 2
    assert len(v2.nodes) == 6
    assert v2.tombstones == ("n3",)
    assert [shard["generation"] for shard in v2.shards] == [1, 2]


def test_v2_shard_hash_mismatch(sample_paths):
    shard_path = write_shard(sample_paths["v2"], "s0001")
    shard_path.write_text(
        shard_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    with pytest.raises(GraphStoreError) as caught:
        load_v2(sample_paths["v2"], known_existing_ids={"n3"})
    assert caught.value.code == ErrorCode.SHARD_HASH_MISMATCH


def test_v2_generation_regression(sample_paths):
    manifest_path = sample_paths["v2"] / "manifest.json"
    manifest = load_json(manifest_path)
    manifest["shards"][1]["generation"] = 0
    write_json(manifest_path, manifest)
    connection = sqlite3.connect(sample_paths["v2"] / "metadata.sqlite")
    try:
        connection.execute(
            "UPDATE shards SET generation = 0 WHERE shard_id = 's0002'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(GraphStoreError) as caught:
        load_v2(sample_paths["v2"], known_existing_ids={"n3"})
    assert caught.value.code == ErrorCode.V2_GENERATION_REGRESSION


def test_v2_boolean_timestamp_rejected(sample_paths):
    update_v2_node(sample_paths["v2"], "n1", write_ms=True)
    with pytest.raises(GraphStoreError) as caught:
        load_v2(sample_paths["v2"], known_existing_ids={"n3"})
    assert caught.value.code == ErrorCode.V2_INVALID_TIMESTAMP


def test_v2_node_digest_mismatch(sample_paths):
    update_v2_node(sample_paths["v2"], "n2", node_digest="0" * 64)
    with pytest.raises(GraphStoreError) as caught:
        load_v2(sample_paths["v2"], known_existing_ids={"n3"})
    assert caught.value.code == ErrorCode.V2_NODE_DIGEST_MISMATCH


def test_v2_duplicate_node_across_shard(sample_paths):
    shard_path = write_shard(sample_paths["v2"], "s0001")
    shard = load_json(shard_path)
    shard["nodes"].append(dict(shard["nodes"][0]))
    write_json(shard_path, shard)
    manifest_path = sample_paths["v2"] / "manifest.json"
    manifest = load_json(manifest_path)
    manifest["shards"][0]["node_count"] += 1
    write_json(manifest_path, manifest)
    connection = sqlite3.connect(sample_paths["v2"] / "metadata.sqlite")
    try:
        connection.execute(
            "UPDATE shards SET node_count = node_count + 1 WHERE shard_id = 's0001'"
        )
        connection.commit()
    finally:
        connection.close()
    update_shard_hashes(sample_paths["v2"], {"s0001"})
    with pytest.raises(GraphStoreError) as caught:
        load_v2(sample_paths["v2"], known_existing_ids={"n3"})
    assert caught.value.code == ErrorCode.V2_DUPLICATE_NODE_ID


def test_v2_unknown_tombstone_against_v1(sample_paths):
    snapshot = load_json(sample_paths["v1"])
    snapshot["nodes"] = [
        node for node in snapshot["nodes"] if node["id"] != "n3"
    ]
    snapshot["linear_index"] = [
        node_id for node_id in snapshot["linear_index"] if node_id != "n3"
    ]
    write_json(sample_paths["v1"], snapshot)
    with pytest.raises(GraphStoreError) as caught:
        load_snapshots(
            sample_paths["v1"],
            sample_paths["v2"],
            logical_clock_ms=sample_paths["logical_clock_ms"],
        )
    assert caught.value.code == ErrorCode.V2_TOMBSTONE_UNKNOWN_NODE


def test_v2_manifest_node_bounds(sample_paths):
    mutate_v2_manifest_and_metadata(
        sample_paths["v2"],
        {
            "node_count_min": 100,
            "node_count_max": 101,
        },
    )
    with pytest.raises(GraphStoreError) as caught:
        load_v2(sample_paths["v2"], known_existing_ids={"n3"})
    assert caught.value.code == ErrorCode.V2_MANIFEST_INVALID

