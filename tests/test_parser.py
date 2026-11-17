from __future__ import annotations

from pathlib import Path

from conftest import write_json
from trustman.models import (
    CERT_SCHEMA,
    CHAIN_SCHEMA,
    SNAPSHOT_SCHEMA,
)
from trustman.parser import (
    parse_certificate_files,
    parse_chain_files,
    parse_snapshot_files,
)


def _cert(cert_id: str, issuer_id: str, fingerprint_seed: str | None = None):
    return {
        "id": cert_id,
        "issuer_id": issuer_id,
        "public_key_fingerprint": fingerprint_seed
        or "sha256:" + "a" * 64,
        "valid_from_ms": 100,
        "valid_to_ms": 200,
        "revoked": False,
    }


def test_schema_rejections_continue_and_include_source_index(tmp_path: Path):
    certs = [
        _cert("root-a", "root-a"),
        _cert("leaf-a", "root-a"),
        _cert("bad-fingerprint", "root-a", "not-a-fingerprint"),
        _cert("duplicate", "root-a"),
        _cert("duplicate", "root-a"),
        _cert("orphan", "does-not-exist"),
    ]
    cert_path = write_json(
        tmp_path / "certificates.json",
        {"schema": CERT_SCHEMA, "certificates": certs},
    )
    cert_result = parse_certificate_files([cert_path])
    assert [item.cert_id for item in cert_result.items] == ["leaf-a", "root-a"]
    assert {rejection.reason for rejection in cert_result.rejections} == {
        "invalid_fingerprint",
        "duplicate_certificate_id",
        "issuer_unresolved",
    }
    assert all(
        rejection.code == "E_SCHEMA"
        and rejection.source_file == "certificates.json"
        and rejection.entry_index >= 0
        for rejection in cert_result.rejections
    )
    assert any(
        rejection.entry_index == 2 for rejection in cert_result.rejections
    )
    assert sum(
        1
        for rejection in cert_result.rejections
        if rejection.reason == "duplicate_certificate_id"
    ) == 2

    snapshots = [
        {
            "version": 1,
            "policy_version": "1.0.0",
            "trusted_roots": ["root-a"],
            "revoked_entries": [],
            "observed_at_ms": 150,
        },
        {
            "version": 2,
            "policy_version": "not-semver",
            "trusted_roots": ["root-a"],
            "revoked_entries": [],
            "observed_at_ms": 150,
        },
    ]
    snapshot_path = write_json(
        tmp_path / "snapshots.json",
        {"schema": SNAPSHOT_SCHEMA, "snapshots": snapshots},
    )
    snapshot_result = parse_snapshot_files([snapshot_path])
    assert [item.version for item in snapshot_result.items] == [1]
    assert len(snapshot_result.rejections) == 1
    assert snapshot_result.rejections[0].entry_index == 1
    assert snapshot_result.rejections[0].code == "E_SCHEMA"

    chains = [
        {
            "chain_id": "valid-chain",
            "certificate_ids": ["leaf-a", "root-a"],
            "deadline_ms": 100,
            "expected_summary": "trusted",
        },
        {
            "chain_id": "duplicate-path",
            "certificate_ids": ["leaf-a", "leaf-a"],
            "deadline_ms": 100,
            "expected_summary": "untrusted",
        },
        {
            "chain_id": "bad-deadline",
            "certificate_ids": ["leaf-a", "root-a"],
            "deadline_ms": 0,
            "expected_summary": "untrusted",
        },
        {
            "chain_id": "empty-chain",
            "certificate_ids": [],
            "deadline_ms": 100,
            "expected_summary": "untrusted",
        },
    ]
    chain_path = write_json(
        tmp_path / "chains.json",
        {"schema": CHAIN_SCHEMA, "chains": chains},
    )
    chain_result = parse_chain_files([chain_path])
    assert [item.chain_id for item in chain_result.items] == [
        "empty-chain",
        "valid-chain",
    ]
    assert chain_result.declared_count == 4
    assert {rejection.reason for rejection in chain_result.rejections} == {
        "duplicate_chain_certificate",
        "invalid_deadline",
    }
    assert all(
        rejection.source_file == "chains.json"
        and rejection.code == "E_SCHEMA"
        and rejection.entry_index >= 0
        for rejection in chain_result.rejections
    )
