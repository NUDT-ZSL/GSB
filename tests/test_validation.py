import json
import sqlite3
from pathlib import Path

import pytest

from coord_workbench.errors import BizError, Err
from coord_workbench.validation import (
    parse_events,
    parse_manifest,
    parse_subscriptions,
    parse_topo_v1,
    parse_topo_v2,
)


def _write(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _node(name="node-a", **kw):
    base = {
        "name": name,
        "role": "CANDIDATE",
        "candidate_addr": "127.0.0.1:7000",
        "lease_epoch": 1,
        "priority": 10,
        "health": "HEALTHY",
    }
    base.update(kw)
    return base


def test_manifest_ok(tmp_path: Path) -> None:
    p = _write(tmp_path / "nodes.json", {"nodes": [_node("node-a"), _node("node-b", lease_epoch=2)]})
    assert len(parse_manifest(p)) == 2


def test_manifest_duplicate_node(tmp_path: Path) -> None:
    p = _write(tmp_path / "nodes.json", {"nodes": [_node("node-a"), _node("node-a")]})
    with pytest.raises(BizError) as ei:
        parse_manifest(p)
    assert ei.value.code == Err.NODE_DUPLICATE
    assert ei.value.fields["node"] == "node-a"


def test_manifest_bad_role(tmp_path: Path) -> None:
    p = _write(tmp_path / "nodes.json", {"nodes": [_node(role="KING")]})
    with pytest.raises(BizError) as ei:
        parse_manifest(p)
    assert ei.value.code == Err.ROLE_ENUM
    assert "allowed" in ei.value.fields


def test_manifest_non_loopback(tmp_path: Path) -> None:
    p = _write(tmp_path / "nodes.json", {"nodes": [_node(candidate_addr="8.8.8.8:7000")]})
    with pytest.raises(BizError) as ei:
        parse_manifest(p)
    assert ei.value.code == Err.ADDR_NOT_LOOPBACK


def test_manifest_epoch_regress(tmp_path: Path) -> None:
    p = _write(tmp_path / "nodes.json", {"nodes": [_node("node-a", lease_epoch=3), _node("node-b", lease_epoch=1)]})
    with pytest.raises(BizError) as ei:
        parse_manifest(p)
    assert ei.value.code == Err.EPOCH_REGRESS
    assert ei.value.fields["prev"] == 3


def test_manifest_missing_field(tmp_path: Path) -> None:
    bad = _node()
    del bad["priority"]
    p = _write(tmp_path / "nodes.json", {"nodes": [bad]})
    with pytest.raises(BizError) as ei:
        parse_manifest(p)
    assert ei.value.code == Err.FIELD_MISSING
    assert ei.value.fields["field"] == "priority"


def _event(seq, node="node-a", term=5, clock=1000):
    return {"seq": seq, "node": node, "clock_ms": clock, "candidate_term": term,
            "vote": {"for": node, "weight": 1}, "revoked": False}


def test_events_seq_not_increasing(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps(_event(1)) + "\n" + json.dumps(_event(1)) + "\n", encoding="utf-8")
    with pytest.raises(BizError) as ei:
        parse_events(p)
    assert ei.value.code == Err.EVENT_SEQ_ORDER


def test_events_term_regress(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps(_event(1, term=6)) + "\n" + json.dumps(_event(2, term=5)) + "\n", encoding="utf-8")
    with pytest.raises(BizError) as ei:
        parse_events(p)
    assert ei.value.code == Err.TERM_REGRESS


def test_subscriptions_unknown_subscriber(tmp_path: Path, sample_dir: Path) -> None:
    nodes = parse_manifest(sample_dir / "nodes.json")
    p = _write(tmp_path / "subs.json", {"subscriptions": [
        {"service": "s", "subscriber": "ghost", "callback": "http://127.0.0.1:9/h"}]})
    with pytest.raises(BizError) as ei:
        parse_subscriptions(p, nodes)
    assert ei.value.code == Err.SUBSCRIBER_UNKNOWN


def test_subscriptions_non_loopback_callback(tmp_path: Path, sample_dir: Path) -> None:
    nodes = parse_manifest(sample_dir / "nodes.json")
    p = _write(tmp_path / "subs.json", {"subscriptions": [
        {"service": "s", "subscriber": "node-a", "callback": "http://10.0.0.9:9/h"}]})
    with pytest.raises(BizError) as ei:
        parse_subscriptions(p, nodes)
    assert ei.value.code == Err.CALLBACK_NOT_LOOPBACK


def test_v1_negative_rel_seconds(tmp_path: Path) -> None:
    p = _write(tmp_path / "v1.json", {"format": "v1", "entries": [
        {"node": "node-a", "rel_publish_s": -1, "scope": "d"}]})
    with pytest.raises(BizError) as ei:
        parse_topo_v1(p)
    assert ei.value.code == Err.V1_REL_SECONDS


def test_v2_shard_hash_mismatch(sample_dir: Path, tmp_path: Path) -> None:
    import shutil

    v2 = tmp_path / "topo_v2"
    shutil.copytree(sample_dir / "topo_v2", v2)
    shard = json.loads((v2 / "shard_0.json").read_text())
    shard["entries"][0]["publish_ms"] += 1  # tamper without updating hash
    (v2 / "shard_0.json").write_text(json.dumps(shard), encoding="utf-8")
    with pytest.raises(BizError) as ei:
        parse_topo_v2(v2)
    assert ei.value.code == Err.SHARD_HASH_MISMATCH
    assert ei.value.fields["shard"] == 0


def test_v2_epoch_conflict(sample_dir: Path, tmp_path: Path) -> None:
    import shutil

    v2 = tmp_path / "topo_v2"
    shutil.copytree(sample_dir / "topo_v2", v2)
    conn = sqlite3.connect(str(v2 / "meta.sqlite"))
    with conn:
        conn.execute("UPDATE meta SET value='99' WHERE key='topology_epoch'")
    conn.close()
    with pytest.raises(BizError) as ei:
        parse_topo_v2(v2)
    assert ei.value.code == Err.EPOCH_CONFLICT
    assert ei.value.fields["meta_epoch"] == 99
