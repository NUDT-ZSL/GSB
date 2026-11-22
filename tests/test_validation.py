from __future__ import annotations

import sqlite3

import pytest
from conftest import read_jsonl, write_jsonl

from log_retention.config import Config
from log_retention.engine import Engine
from log_retention.errors import ArchiveError


def _load_error(config: Config, code: str) -> None:
    with pytest.raises(ArchiveError) as caught:
        Engine(config).load()
    assert caught.value.code == code
    assert not config.ledger_db.exists(), "invalid input must not create ledger state"


def test_primary_txn_order_rejected_without_persistence(config: Config) -> None:
    rows = read_jsonl(config.primary_stream)
    rows[1]["txn_id"] = rows[0]["txn_id"]
    write_jsonl(config.primary_stream, rows)
    _load_error(config, "E_PRIMARY_TXN_ORDER")


def test_primary_segment_gap_rejected(config: Config) -> None:
    rows = [row for row in read_jsonl(config.primary_stream) if row["segment_id"] != 3]
    write_jsonl(config.primary_stream, rows)
    _load_error(config, "E_PRIMARY_SEGMENT_GAP")


def test_replay_txn_order_is_per_instance(config: Config) -> None:
    rows = read_jsonl(config.replay_stream)
    for row in rows:
        if row["instance_id"] == "replica-a" and row["txn_id"] == 3:
            row["txn_id"] = 1
    write_jsonl(config.replay_stream, rows)
    _load_error(config, "E_REPLAY_TXN_ORDER")


def test_relative_seconds_must_be_non_negative_integer(config: Config) -> None:
    rows = read_jsonl(config.v1_policy)
    rows[0]["relative_retention_seconds"] = -1
    write_jsonl(config.v1_policy, rows)
    _load_error(config, "E_POLICY_RELATIVE_SECONDS")


def test_policy_tombstone_must_match_replay(config: Config) -> None:
    rows = read_jsonl(config.replay_stream)
    for row in rows:
        if row["instance_id"] == "replica-d" and row["txn_id"] == 3:
            row["result"] = "active"
            row["tombstone"] = False
    write_jsonl(config.replay_stream, rows)
    _load_error(config, "E_POLICY_TOMBSTONE_REPLAY_MISMATCH")


def test_generation_must_be_monotonic_along_parent(config: Config) -> None:
    import json

    shard = config.v2_shards[0]
    document = json.loads(shard.read_text(encoding="utf-8"))
    for policy in document["policies"]:
        if policy["policy_id"] == "v2-a-g2":
            policy["retention_generation"] = 0
    shard.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    conn = sqlite3.connect(config.v2_meta_db)
    try:
        conn.execute(
            "UPDATE policy_meta SET retention_generation = 0 WHERE policy_id = 'v2-a-g2'"
        )
        conn.commit()
    finally:
        conn.close()
    _load_error(config, "E_POLICY_GENERATION_ORDER")


def test_v2_shard_and_metadata_mismatch_is_rejected(config: Config) -> None:
    conn = sqlite3.connect(config.v2_meta_db)
    try:
        conn.execute(
            "UPDATE policy_meta SET absolute_retention_cutoff_ms = 999 "
            "WHERE policy_id = 'v2-b-root'"
        )
        conn.commit()
    finally:
        conn.close()
    _load_error(config, "E_POLICY_V2_SOURCE_MISMATCH")


def test_v2_metadata_booleans_can_supply_missing_shard_fields(config: Config) -> None:
    import json

    shard = config.v2_shards[1]
    document = json.loads(shard.read_text(encoding="utf-8"))
    for policy in document["policies"]:
        if policy["policy_id"] == "v2-d-tombstone":
            policy.pop("policy_tombstone")
    shard.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    engine = Engine(config)
    try:
        result = engine.load()
        assert result["policies"] == 10
    finally:
        engine.close()


def test_load_version_regression_keeps_pointer(config: Config) -> None:
    first = Engine(config)
    try:
        first.load()
    finally:
        first.close()
    second_config = config.with_overrides(load_version=2)
    second = Engine(second_config)
    try:
        second.load()
    finally:
        second.close()
    with pytest.raises(ArchiveError) as caught:
        Engine(config).load()
    assert caught.value.code == "E_LOAD_VERSION_REGRESSION"
    with sqlite3.connect(config.ledger_db) as conn:
        version = conn.execute(
            "SELECT load_version FROM load_pointer WHERE singleton = 1"
        ).fetchone()[0]
    assert version == 2
