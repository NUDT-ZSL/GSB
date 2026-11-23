from __future__ import annotations

import json

from cryptography import x509

from tlsdiag.chain import wildcard_match
from tlsdiag.engine import DiagnosticEngine, SampleLayout, build_trust_view
from tlsdiag.storage import Storage


def _run_verify(sample_dir, db_path):
    layout = SampleLayout.load(sample_dir)
    storage = Storage(db_path)
    engine = DiagnosticEngine(layout, storage)
    engine.apply_snapshot(1)
    results = engine.verify_all()
    storage.close()
    return {r["hostname"]: r for r in results}


def test_wildcard_matching_rules():
    assert wildcard_match("api.example.com", "*.example.com")
    assert not wildcard_match("a.b.example.com", "*.example.com")
    assert wildcard_match("example.com", "example.com")
    assert not wildcard_match("evil.com", "*.example.com")


def test_good_chain_valid(sample_dir, db_path):
    results = _run_verify(sample_dir, db_path)
    good = results["good.example.com"]
    assert good["valid"] is True
    assert good["failure_code"] is None
    stages = {node["stage"] for node in good["trace"]}
    assert {"signature", "basic_constraints", "key_usage", "validity", "hostname", "trust_anchor"} <= stages


def test_wildcard_leaf_valid(sample_dir, db_path):
    results = _run_verify(sample_dir, db_path)
    assert results["api.example.com"]["valid"] is True


def test_expired_leaf_rejected_without_wall_clock(sample_dir, db_path):
    results = _run_verify(sample_dir, db_path)
    expired = results["expired.example.com"]
    assert expired["valid"] is False
    assert expired["failure_code"] == "E4005"


def test_revoked_leaf_distinguished_from_unknown(sample_dir, db_path):
    results = _run_verify(sample_dir, db_path)
    revoked = results["revoked.example.com"]
    assert revoked["revocation"]["status"] == "revoked"
    assert revoked["failure_code"] == "E4007"
    wildcard = results["api.example.com"]
    assert wildcard["revocation"]["status"] == "unknown"
    good = results["good.example.com"]
    assert good["revocation"]["status"] == "good"


def test_chain_includes_root_and_intermediate(sample_dir, db_path):
    results = _run_verify(sample_dir, db_path)
    subjects = results["good.example.com"]["ordered_subjects"]
    assert subjects[-1] == "CN=Root CA"
    assert "CN=Intermediate CA" in subjects


def test_generation_change_invalidates_revocation_cache(sample_dir, db_path):
    layout = SampleLayout.load(sample_dir)
    storage = Storage(db_path)
    engine = DiagnosticEngine(layout, storage)
    engine.apply_snapshot(1)
    engine.verify_all()
    first = storage.revocation_conclusions()
    assert any(item["status"] == "revoked" for item in first)

    # Bump generation in the v2 metadata + shard, then re-commit as version 2.
    manifest_path = sample_dir / "truststore_v2" / "manifest.json"
    import hashlib

    manifest = json.loads(manifest_path.read_text())
    for info in manifest["shards"]:
        path = sample_dir / info["file"]
        shard = json.loads(path.read_text())
        shard["generation"] = shard["generation"] + 1
        body = json.dumps(shard, indent=2, sort_keys=True).encode()
        path.write_bytes(body)
        info["sha256"] = hashlib.sha256(body).hexdigest()
        info["generation"] = shard["generation"]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    import sqlite3

    db = sqlite3.connect(sample_dir / "truststore_v2" / "meta.sqlite3")
    db.execute("UPDATE anchor_meta SET generation = generation + 1")
    db.commit()
    db.close()

    engine.apply_snapshot(2)
    engine.verify_all()
    generations = {item["generation"] for item in storage.revocation_conclusions()}
    # The v2 anchors advanced by one generation; recomputation must persist a
    # new revocation row keyed by the new generation rather than reuse gen 3.
    assert 4 in generations
    assert any(item["status"] == "revoked" and item["generation"] == 4 for item in storage.revocation_conclusions())
    storage.close()


def test_hostname_failure_path(sample_dir, db_path):
    layout = SampleLayout.load(sample_dir)
    view, _, _ = build_trust_view(layout)
    leaf = x509.load_pem_x509_certificate((sample_dir / "certs" / "good.pem").read_bytes())
    from tlsdiag.chain import validate_chain

    instant = layout.clock.instant_ms(6)
    ca_certs = [
        x509.load_pem_x509_certificate((sample_dir / "certs" / name).read_bytes())
        for name in ("root_ca.pem", "intermediate_ca.pem")
    ]
    result = validate_chain(leaf, "attacker.evil.com", "serverAuth", 3, instant, view, ca_certs)
    assert result.valid is False
    assert result.failure_code == "E4006"
