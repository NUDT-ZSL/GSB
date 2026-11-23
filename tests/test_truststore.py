from __future__ import annotations

import json
import sqlite3

import pytest

from tlsdiag.clock import LogicalClock, PrecisionConfig
from tlsdiag.engine import SampleLayout, build_trust_view
from tlsdiag.errors import DiagError
from tlsdiag.truststore import load_v1, load_v2


def test_valid_snapshots_load(sample_dir):
    layout = SampleLayout.load(sample_dir)
    v1 = load_v1(layout.v1_path)
    v2 = load_v2(layout.v2_manifest)
    assert {a.alias for a in v1.anchors} >= {"root-ca", "intermediate-ca"}
    assert {a.alias for a in v2.anchors} >= {"root-ca", "intermediate-ca", "revoked-leaf-anchor"}


def test_v1_duplicate_alias(sample_dir):
    path = sample_dir / "truststore_v1" / "truststore.json"
    data = json.loads(path.read_text())
    data["entries"].append(dict(data["entries"][0]))
    path.write_text(json.dumps(data))
    with pytest.raises(DiagError) as exc:
        load_v1(path)
    assert exc.value.code == "E1001"


def test_v1_bad_fingerprint(sample_dir):
    path = sample_dir / "truststore_v1" / "truststore.json"
    data = json.loads(path.read_text())
    data["entries"][0]["fingerprint_sha256"] = "deadbeef"
    path.write_text(json.dumps(data))
    with pytest.raises(DiagError) as exc:
        load_v1(path)
    assert exc.value.code == "E1002"


def test_v1_negative_seconds(sample_dir):
    path = sample_dir / "truststore_v1" / "truststore.json"
    data = json.loads(path.read_text())
    data["entries"][0]["relative_trust_seconds"] = -1
    path.write_text(json.dumps(data))
    with pytest.raises(DiagError) as exc:
        load_v1(path)
    assert exc.value.code == "E1003"


def test_v1_bad_enabled_flag(sample_dir):
    path = sample_dir / "truststore_v1" / "truststore.json"
    data = json.loads(path.read_text())
    data["entries"][0]["enabled"] = "yes"
    path.write_text(json.dumps(data))
    with pytest.raises(DiagError) as exc:
        load_v1(path)
    assert exc.value.code == "E1006"


def test_v2_shard_hash_mismatch(sample_dir):
    manifest_path = sample_dir / "truststore_v2" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    shard_rel = manifest["shards"][0]["file"]
    shard_path = sample_dir / shard_rel
    shard = json.loads(shard_path.read_text())
    shard["generation"] = 99
    shard_path.write_text(json.dumps(shard))
    with pytest.raises(DiagError) as exc:
        load_v2(manifest_path)
    assert exc.value.code == "E2001"


def test_v2_non_integer_effective_ms(sample_dir):
    manifest_path = sample_dir / "truststore_v2" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    shard_rel = manifest["shards"][0]["file"]
    shard_path = sample_dir / shard_rel
    shard = json.loads(shard_path.read_text())
    shard["effective_ms"] = 1.5
    body = json.dumps(shard).encode()
    import hashlib

    shard_path.write_bytes(body)
    manifest["shards"][0]["sha256"] = hashlib.sha256(body).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(DiagError) as exc:
        load_v2(manifest_path)
    assert exc.value.code == "E2004"


def test_v2_non_monotonic_generation(sample_dir):
    db_path = sample_dir / "truststore_v2" / "meta.sqlite3"
    db = sqlite3.connect(db_path)
    db.execute("INSERT INTO anchor_meta VALUES ('root-ca', 1, 0, NULL, 0)")
    db.commit()
    db.close()
    with pytest.raises(DiagError) as exc:
        load_v2(sample_dir / "truststore_v2" / "manifest.json")
    assert exc.value.code == "E2003"


def test_v2_bad_tombstone(sample_dir):
    manifest_path = sample_dir / "truststore_v2" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    target = None
    for info in manifest["shards"]:
        shard = json.loads((sample_dir / info["file"]).read_text())
        if shard.get("alert_tombstone"):
            target = (info, shard)
            break
    info, shard = target
    shard["alert_tombstone"] = {"code": "x"}
    import hashlib

    body = json.dumps(shard).encode()
    (sample_dir / info["file"]).write_bytes(body)
    info["sha256"] = hashlib.sha256(body).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(DiagError) as exc:
        load_v2(manifest_path)
    assert exc.value.code == "E2005"


def test_invalid_snapshot_does_not_touch_storage(sample_dir, db_path):
    from tlsdiag.storage import Storage

    storage = Storage(db_path)
    before = storage.current_version()
    path = sample_dir / "truststore_v1" / "truststore.json"
    data = json.loads(path.read_text())
    data["entries"][0]["relative_trust_seconds"] = -7
    path.write_text(json.dumps(data))
    with pytest.raises(DiagError):
        build_trust_view(SampleLayout.load(sample_dir))
    assert storage.current_version() == before
    storage.close()


def test_merge_arbitration_prefers_newer_clock(sample_dir):
    layout = SampleLayout.load(sample_dir)
    view, _, _ = build_trust_view(layout)
    root = view.anchors["root-ca"]
    assert root.source_version == "v2"
    assert "logical clock" in root.decision_basis


def test_v1_conversion_records_scale_and_rounding(sample_dir):
    layout = SampleLayout.load(sample_dir)
    clock = LogicalClock.from_file(sample_dir / "clock.json")
    config = PrecisionConfig.from_file(sample_dir / "precision.json")
    view, _, _ = build_trust_view(layout)
    boundary = view.anchors["intermediate-boundary"]
    assert boundary.source_version == "v1"
    expected = clock.instant_ms(0) + 3600 * (config.ms_num // config.ms_den)
    assert boundary.absolute_trust_ms_fixed == expected
    assert "Q12" in boundary.scale
    assert boundary.rounding == config.rounding
