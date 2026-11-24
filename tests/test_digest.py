"""快照防篡改、版本识别与失效回退测试."""

import sqlite3

import pytest

from castdiag.digest import select_valid_generations, verify_generation, load_digest_manifest
from castdiag.errors import DiagError, E_DIGEST
from castdiag.pipeline import prepare


def _tamper_file(path, marker=b'"tampered":true,'):
    data = path.read_bytes()
    path.write_bytes(data[:2] + marker + data[2:])


def test_valid_sample_passes(sample_dir):
    snap = sample_dir / "snapshots"
    manifest = load_digest_manifest(snap)
    verify_generation(snap, manifest, "v1")
    verify_generation(snap, manifest, "v2")


def test_tampered_shard_rejected(sample_dir):
    snap = sample_dir / "snapshots"
    _tamper_file(snap / "v2" / "shard_0.json")
    manifest = load_digest_manifest(snap)
    with pytest.raises(DiagError) as exc:
        verify_generation(snap, manifest, "v2")
    assert exc.value.code == E_DIGEST


def test_missing_digest_record_rejected(sample_dir):
    snap = sample_dir / "snapshots"
    # 被记录的文件缺失 => 失效
    (snap / "v2" / "shard_1.json").unlink()
    manifest = load_digest_manifest(snap)
    with pytest.raises(DiagError) as exc:
        verify_generation(snap, manifest, "v2")
    assert exc.value.code == E_DIGEST


def test_digest_manifest_missing_rejected(sample_dir):
    snap = sample_dir / "snapshots"
    (snap / "digests.json").unlink()
    with pytest.raises(DiagError) as exc:
        load_digest_manifest(snap)
    assert exc.value.code == E_DIGEST


def test_declared_screen_count_mismatch(sample_dir):
    snap = sample_dir / "snapshots"
    db = snap / "v2" / "meta.db"
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM screens WHERE screen_id='bedroom-tv'")
    conn.commit()
    conn.close()
    # meta.db 内容变化 => 摘要即不匹配(E_DIGEST)
    manifest = load_digest_manifest(snap)
    with pytest.raises(DiagError) as exc:
        verify_generation(snap, manifest, "v2")
    assert exc.value.code == E_DIGEST


def test_fallback_to_v1_when_v2_invalid(sample_dir):
    snap = sample_dir / "snapshots"
    _tamper_file(snap / "v2" / "meta.db")
    valid, rejected = select_valid_generations(snap)
    assert valid == ["v1"]
    assert rejected and rejected[0]["generation"] == "v2"
    prep = prepare(sample_dir)
    assert prep["fallback"]["to"] == "v1"
    assert all(r["generation"] == 1 for r in prep["view"].values())


def test_all_invalid_raises(sample_dir):
    snap = sample_dir / "snapshots"
    _tamper_file(snap / "v2" / "meta.db")
    _tamper_file(snap / "v1" / "snapshot_v1.json")
    with pytest.raises(DiagError) as exc:
        select_valid_generations(snap)
    assert exc.value.code == E_DIGEST
