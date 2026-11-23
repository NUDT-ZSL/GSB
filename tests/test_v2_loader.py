import hashlib
import json
import os
import sqlite3

import pytest

from recovery_engine.errors import BizError, ErrCode
from recovery_engine.v2_loader import load_v2
from tests.conftest import V2_DIR, V2_META


def make_v2(tmp_path, shards, floor=1, tamper=None, skip_hash_for=None):
    """构造 v2 分片目录与元数据库。tamper: 写入元库后修改指定分片内容。"""
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    hashes = {}
    for shard_id, services in shards.items():
        path = shard_dir / (shard_id + ".json")
        path.write_text(json.dumps(
            {"shard_id": shard_id, "services": services},
            sort_keys=True, indent=2) + "\n", encoding="utf-8")
        hashes[shard_id] = hashlib.sha256(path.read_bytes()).hexdigest()
    meta = tmp_path / "meta.sqlite"
    conn = sqlite3.connect(str(meta))
    conn.execute("CREATE TABLE shards (shard_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
    conn.execute("CREATE TABLE generation_floor (scope TEXT PRIMARY KEY, generation_floor INTEGER NOT NULL)")
    for shard_id, digest in hashes.items():
        conn.execute("INSERT INTO shards VALUES (?, ?)", (shard_id, digest))
    conn.execute("INSERT INTO generation_floor VALUES ('global', ?)", (floor,))
    conn.commit()
    conn.close()
    if tamper:
        path = shard_dir / (tamper + ".json")
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    return str(shard_dir), str(meta)


def svc(name, **kw):
    base = {"name": name, "effective_ms": 100, "generation": 1,
            "upstreams": [], "tombstoned": False,
            "actions": [{"id": name + "-go", "kind": "switch", "at_ms": 110}]}
    base.update(kw)
    return base


def test_load_samples_valid():
    cfg = load_v2(V2_DIR, V2_META)
    names = set(cfg["services"])
    assert names == {"db", "api", "cache", "web", "tie", "oldsvc"}
    assert cfg["services"]["oldsvc"].tombstoned is True
    assert cfg["services"]["api"].upstreams == ["db"]
    assert cfg["services"]["api"].generation == 3
    assert cfg["services"]["api"].actions[0].priority == "P1"


def test_hash_mismatch(tmp_path):
    shard_dir, meta = make_v2(tmp_path, {"s1": [svc("a")]}, tamper="s1")
    with pytest.raises(BizError) as exc:
        load_v2(shard_dir, meta)
    assert exc.value.code == ErrCode.E_HASH_MISMATCH
    assert exc.value.details["shard"] == "s1"
    assert "expected" in exc.value.details and "actual" in exc.value.details


def test_missing_shard_file(tmp_path):
    shard_dir, meta = make_v2(tmp_path, {"s1": [svc("a")]})
    os.remove(os.path.join(shard_dir, "s1.json"))
    with pytest.raises(BizError) as exc:
        load_v2(shard_dir, meta)
    assert exc.value.code == ErrCode.E_HASH_MISMATCH


def test_generation_conflict(tmp_path):
    shard_dir, meta = make_v2(tmp_path, {"s1": [svc("a", generation=1)]}, floor=2)
    with pytest.raises(BizError) as exc:
        load_v2(shard_dir, meta)
    assert exc.value.code == ErrCode.E_GENERATION_CONFLICT
    assert exc.value.details["floor"] == 2


def test_non_integer_effective_ms(tmp_path):
    shard_dir, meta = make_v2(tmp_path, {"s1": [svc("a", effective_ms="100")]})
    with pytest.raises(BizError) as exc:
        load_v2(shard_dir, meta)
    assert exc.value.code == ErrCode.E_BAD_CONFIG


def test_missing_upstream(tmp_path):
    shard_dir, meta = make_v2(tmp_path, {"s1": [svc("a", upstreams=["ghost"])]})
    with pytest.raises(BizError) as exc:
        load_v2(shard_dir, meta)
    assert exc.value.code == ErrCode.E_BAD_CONFIG
    assert exc.value.details["upstream"] == "ghost"


def test_self_loop(tmp_path):
    shard_dir, meta = make_v2(tmp_path, {"s1": [svc("a", upstreams=["a"])]})
    with pytest.raises(BizError) as exc:
        load_v2(shard_dir, meta)
    assert exc.value.code == ErrCode.E_BAD_CONFIG


def test_cycle_detected(tmp_path):
    shard_dir, meta = make_v2(
        tmp_path, {"s1": [svc("a", upstreams=["b"]), svc("b", upstreams=["a"])]})
    with pytest.raises(BizError) as exc:
        load_v2(shard_dir, meta)
    assert exc.value.code == ErrCode.E_CYCLE
    assert sorted(exc.value.details["remaining"]) == ["a", "b"]


def test_invalid_input_writes_nothing(tmp_path, store):
    """非法输入不得写入任何持久化状态。"""
    from recovery_engine.engine import Engine
    from recovery_engine.executor import SimulatedExecutor
    from tests.conftest import make_clock, V1_PATH

    shard_dir, meta = make_v2(tmp_path, {"s1": [svc("a")]}, tamper="s1")
    engine = Engine(store, make_clock(), SimulatedExecutor())
    with pytest.raises(BizError) as exc:
        engine.build_plan(V1_PATH, shard_dir, meta)
    assert exc.value.code == ErrCode.E_HASH_MISMATCH
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM migration_decisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM service_state").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0] == 0
