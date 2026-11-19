"""Requirement 1: v1/v2 parsing and validation with business error codes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import build_plan
from recovery_pipeline.errors import BizError, Err
from recovery_pipeline.v1_loader import load_v1
from recovery_pipeline.v2_loader import load_v2


def _write_v1(tmp_path, mutate) -> Path:
    from samples.gen_samples import V1_CONFIG
    cfg = json.loads(json.dumps(V1_CONFIG))
    mutate(cfg)
    path = tmp_path / "bad_v1.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_v1_valid(samples):
    cfg = load_v1(samples["v1"])
    assert [s.name for s in cfg.services] == ["svc-alpha", "svc-beta"]
    assert cfg.action_map["svc-alpha"] == [
        "alpha-stop", "alpha-switch", "alpha-start"]


def test_v1_duplicate_service(tmp_path):
    path = _write_v1(tmp_path, lambda c: c["services"].append(
        dict(c["services"][0])))
    with pytest.raises(BizError) as ei:
        load_v1(path)
    assert ei.value.code == Err.V1_DUPLICATE_SERVICE
    assert ei.value.fields["service"] == "svc-alpha"


def test_v1_negative_rollback(tmp_path):
    path = _write_v1(tmp_path, lambda c: c["services"][0].update(
        rollback_seconds=-5))
    with pytest.raises(BizError) as ei:
        load_v1(path)
    assert ei.value.code == Err.V1_INVALID_ROLLBACK_SECONDS


def test_v1_non_monotonic_steps(tmp_path):
    def mutate(cfg):
        cfg["services"][0]["actions"][1]["at_tick"] = -1
    path = _write_v1(tmp_path, mutate)
    with pytest.raises(BizError) as ei:
        load_v1(path)
    assert ei.value.code == Err.V1_NON_MONOTONIC_STEPS


def test_v1_mapping_inconsistent(tmp_path):
    path = _write_v1(tmp_path, lambda c: c["action_map"].pop("svc-beta"))
    with pytest.raises(BizError) as ei:
        load_v1(path)
    assert ei.value.code == Err.V1_MAPPING_INCONSISTENT


def test_v2_valid(samples):
    cfg = load_v2(samples["v2"])
    names = {s.name for s in cfg.services}
    assert names == {"svc-beta", "svc-gamma", "svc-delta", "svc-legacy"}
    assert cfg.manifest_version == 2
    assert len(cfg.shard_digests) == 2


def test_v2_shard_hash_mismatch(samples):
    shard = Path(samples["v2"]) / "shards" / "shard-0.json"
    shard.write_text(shard.read_text() + " ", encoding="utf-8")
    with pytest.raises(BizError) as ei:
        load_v2(samples["v2"])
    assert ei.value.code == Err.V2_SHARD_HASH_MISMATCH
    assert "expected" in ei.value.fields and "actual" in ei.value.fields


def test_v2_generation_regression(samples):
    meta = Path(samples["v2"]) / "meta.sqlite3"
    conn = sqlite3.connect(str(meta))
    conn.execute(
        "INSERT INTO service_meta (service, generation, effective_ms)"
        " VALUES ('svc-beta', 1, 910000)")
    conn.commit()
    conn.close()
    with pytest.raises(BizError) as ei:
        load_v2(samples["v2"])
    assert ei.value.code == Err.V2_GENERATION_REGRESSION
    assert ei.value.fields["service"] == "svc-beta"


def test_v2_negative_effective_ms(samples, tmp_path):
    shard = Path(samples["v2"]) / "shards" / "shard-0.json"
    payload = json.loads(shard.read_text())
    payload["services"][0]["effective_ms"] = -3
    shard.write_text(json.dumps(payload))
    manifest = Path(samples["v2"]) / "manifest.json"
    m = json.loads(manifest.read_text())
    import hashlib
    m["shards"][0]["sha256"] = hashlib.sha256(
        shard.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(m))
    with pytest.raises(BizError) as ei:
        load_v2(samples["v2"])
    assert ei.value.code == Err.V2_INVALID_EFFECTIVE_MS


def test_v2_dangling_dependency(samples):
    shard = Path(samples["v2"]) / "shards" / "shard-0.json"
    payload = json.loads(shard.read_text())
    payload["services"][1]["upstream"] = ["svc-ghost"]
    blob = json.dumps(payload).encode()
    shard.write_bytes(blob)
    import hashlib
    manifest = Path(samples["v2"]) / "manifest.json"
    m = json.loads(manifest.read_text())
    m["shards"][0]["sha256"] = hashlib.sha256(blob).hexdigest()
    manifest.write_text(json.dumps(m))
    with pytest.raises(BizError) as ei:
        load_v2(samples["v2"])
    assert ei.value.code == Err.V2_DANGLING_DEPENDENCY
    assert ei.value.fields["upstream"] == "svc-ghost"


def test_invalid_config_writes_nothing(samples, tmp_path):
    bad = _write_v1(tmp_path, lambda c: c["services"].append(
        dict(c["services"][0])))
    store_dir = tmp_path / "state"
    with pytest.raises(BizError):
        load_v1(bad)
    assert not store_dir.exists()  # loaders are pure, no persistent state
    plan, _, _ = build_plan(samples)
    assert plan.services  # valid samples still fine afterwards
