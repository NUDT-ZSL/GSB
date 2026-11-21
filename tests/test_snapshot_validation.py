"""Requirement 1: v1/v2 parsing & validation, business error codes, and
no persistence on invalid input."""
import json
import os
import sqlite3

import pytest

from conftest import SAMPLES, T0, NOW_MS, make_v2_package
from dnscache.errors import (
    BizError, E_V1_SCHEMA, E_V1_DUPLICATE_DOMAIN, E_V1_TTL_INVALID,
    E_V1_CLOCK_INVALID, E_V2_SHARD_HASH_MISMATCH,
    E_V2_GENERATION_REGRESSION, E_V2_DUPLICATE_ENTRY, E_V2_EXPIRY_INVALID,
)
from dnscache.service import op_load, op_status
from dnscache.snapshot_v1 import load_v1
from dnscache.snapshot_v2 import load_v2


def _write_v1(tmp_path, entries, **overrides):
    doc = {"format": "v1", "logical_clock_us": T0, "generation": 1,
           "entries": entries}
    doc.update(overrides)
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(doc))
    return str(path)


def test_v1_negative_ttl_rejected(tmp_path):
    path = _write_v1(tmp_path, [
        {"name": "a.test", "type": "A", "ttl_remaining_s": -1}])
    with pytest.raises(BizError) as exc:
        load_v1(path)
    assert exc.value.code == E_V1_TTL_INVALID


def test_v1_non_integer_ttl_rejected(tmp_path):
    for bad in (1.5, "60", True):
        path = _write_v1(tmp_path, [
            {"name": "a.test", "type": "A", "ttl_remaining_s": bad}])
        with pytest.raises(BizError) as exc:
            load_v1(path)
        assert exc.value.code == E_V1_TTL_INVALID


def test_v1_duplicate_after_canonicalization(tmp_path):
    path = _write_v1(tmp_path, [
        {"name": "Alpha.TEST.", "type": "A", "ttl_remaining_s": 1},
        {"name": "alpha.test", "type": "A", "ttl_remaining_s": 2}])
    with pytest.raises(BizError) as exc:
        load_v1(path)
    assert exc.value.code == E_V1_DUPLICATE_DOMAIN


def test_v1_schema_and_clock_errors(tmp_path):
    path = _write_v1(tmp_path, [], format="v9")
    with pytest.raises(BizError) as exc:
        load_v1(path)
    assert exc.value.code == E_V1_SCHEMA
    path = _write_v1(tmp_path, [], logical_clock_us=-5)
    with pytest.raises(BizError) as exc:
        load_v1(path)
    assert exc.value.code == E_V1_CLOCK_INVALID


def test_v1_canonicalization_and_level_mapping(tmp_path):
    path = _write_v1(tmp_path, [
        {"name": "Alpha.TEST.", "type": "A", "ttl_remaining_s": 10,
         "authoritative": True}])
    snap = load_v1(path)
    assert snap.entries[0]["name"] == "alpha.test"
    assert snap.entries[0]["level"] == "authoritative"


def test_v2_shard_hash_mismatch(tmp_path):
    directory = str(tmp_path / "v2bad")
    make_v2_package(directory, 2, 1, T0, {"shard0.json": (1, [])})
    with open(os.path.join(directory, "shard0.json"), "a") as fh:
        fh.write("\n")  # corrupt the shard after hashing
    with pytest.raises(BizError) as exc:
        load_v2(directory)
    assert exc.value.code == E_V2_SHARD_HASH_MISMATCH


def test_v2_generation_regression(tmp_path):
    directory = str(tmp_path / "v2reg")
    make_v2_package(directory, 2, 1, T0, {"shard0.json": (1, [
        {"name": "a.test", "type": "A", "level": "local",
         "expiry_ms": NOW_MS + 1000, "records": []}])})
    with pytest.raises(BizError) as exc:
        load_v2(directory, generation_high_water=2)
    assert exc.value.code == E_V2_GENERATION_REGRESSION


def test_v2_duplicate_entry(tmp_path):
    directory = str(tmp_path / "v2dup")
    entry = {"name": "a.test", "type": "A", "level": "local",
             "expiry_ms": NOW_MS + 1000, "records": []}
    make_v2_package(directory, 2, 1, T0, {"shard0.json": (1, [entry, entry])})
    with pytest.raises(BizError) as exc:
        load_v2(directory)
    assert exc.value.code == E_V2_DUPLICATE_ENTRY


def test_v2_invalid_expiry(tmp_path):
    directory = str(tmp_path / "v2exp")
    make_v2_package(directory, 2, 1, T0, {"shard0.json": (1, [
        {"name": "a.test", "type": "A", "level": "local",
         "expiry_ms": -1, "records": []}])})
    with pytest.raises(BizError) as exc:
        load_v2(directory)
    assert exc.value.code == E_V2_EXPIRY_INVALID


def test_invalid_load_persists_nothing(store, tmp_path):
    path = _write_v1(tmp_path, [
        {"name": "a.test", "type": "A", "ttl_remaining_s": -3}])
    with pytest.raises(BizError):
        op_load(store, T0, 1000, v1_path=path)
    status = op_status(store)
    assert status["current_snapshot_version"] is None
    assert status["ledger_entries"] == 0
    assert status["pending_ops"] == []
    conn = sqlite3.connect(store.db_path)
    assert conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0
    conn.close()


def test_sample_snapshots_load_cleanly():
    v1 = load_v1(os.path.join(SAMPLES, "v1_snapshot.json"))
    assert len(v1.entries) == 6
    v2 = load_v2(os.path.join(SAMPLES, "v2"))
    assert len(v2.entries) == 6
    assert v2.version == 2
