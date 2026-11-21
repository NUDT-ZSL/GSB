from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from certlab.errors import CertLabError, ErrorCode

from .conftest import BASE_MS, CLOCK, leaf_requests


def _patch_with_sample_cert(sample_dir: Path, *, tombstone_beta: bool = False) -> dict:
    cert = x509.load_pem_x509_certificate(
        (sample_dir / "cas" / "root-legacy.pem").read_bytes()
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    fingerprint = hashlib.sha256(
        cert.public_bytes(serialization.Encoding.DER)
    ).hexdigest()
    operations = [
        {
            "op": "add",
            "alias": "root-extra",
            "canonical_alias": "root-extra",
            "pem": pem,
            "fingerprint_sha256": fingerprint,
            "effective_at_ms": BASE_MS,
            "enabled": True,
            "tombstone": "none",
            "revocation": "unknown",
        }
    ]
    if tombstone_beta:
        operations.append(
            {"op": "tombstone", "alias": "root-beta", "tombstone": "tombstoned"}
        )
    return {
        "schema": "trust-patch-v1",
        "patch_id": "closure-test",
        "logical_clock_ms": BASE_MS,
        "operations": operations,
    }


def test_patch_invalidation_closure_and_exact_reuse(store, sample_dir):
    initial = store.verify_requests(leaf_requests(sample_dir), clock=CLOCK)
    connection = store._connect()
    before = {
        row["leaf_id"]: row["conclusion_json"]
        for row in connection.execute(
            "SELECT leaf_id, conclusion_json FROM chain_conclusions"
        )
    }
    connection.close()

    patch = _patch_with_sample_cert(sample_dir, tombstone_beta=True)
    result = store.apply_patch(patch, sample_dir=sample_dir, clock=CLOCK)
    assert result["version"] == 2
    assert result["generation"] == 4
    assert result["summary"]["invalidated_chain_count"] == 1
    assert result["summary"]["reused_chain_count"] == 12
    affected = result["invalidation"]["invalidated"]["chains"]
    assert [item["leaf_id"] for item in affected] == ["leaf-revoked-ocsp"]
    assert any(
        item["operation"] == "tombstone" for item in result["invalidation"]["trigger_sources"]
    )
    assert result["invalidation"]["reused"]["hostnames"]

    connection = store._connect()
    after = {
        row["leaf_id"]: row["conclusion_json"]
        for row in connection.execute(
            "SELECT leaf_id, conclusion_json FROM chain_conclusions "
            "WHERE trust_generation = 4"
        )
    }
    beta_rows = connection.execute(
        "SELECT COUNT(*) FROM chain_conclusions "
        "WHERE trust_generation = 4 AND leaf_id = 'leaf-revoked-ocsp'"
    ).fetchone()[0]
    connection.close()
    assert after["leaf-good"] == before["leaf-good"]
    assert after["leaf-wild"] == before["leaf-wild"]
    assert beta_rows == 0

    repeated = store.apply_patch(patch, sample_dir=sample_dir, clock=CLOCK)
    assert repeated["already_applied"] is True
    assert repeated["version"] == 2

    after_verify = store.verify_requests(leaf_requests(sample_dir), clock=CLOCK)
    assert after_verify["computed_count"] == 1
    assert after_verify["reused_count"] == 12
    assert store.verify_audit()["ok"] is True


def test_modifying_anchor_invalidates_only_its_descendants(store, sample_dir):
    store.verify_requests(leaf_requests(sample_dir), clock=CLOCK)
    connection = store._connect()
    beta_before = connection.execute(
        "SELECT conclusion_json FROM chain_conclusions WHERE leaf_id = 'leaf-revoked-ocsp'"
    ).fetchone()["conclusion_json"]
    connection.close()

    replacement = x509.load_pem_x509_certificate(
        (sample_dir / "cas" / "root-alpha-legacy.pem").read_bytes()
    )
    pem = replacement.public_bytes(serialization.Encoding.PEM).decode()
    fingerprint = hashlib.sha256(
        replacement.public_bytes(serialization.Encoding.DER)
    ).hexdigest()
    patch = {
        "schema": "trust-patch-v1",
        "patch_id": "replace-alpha",
        "logical_clock_ms": BASE_MS,
        "operations": [
            {
                "op": "modify",
                "alias": "root-alpha",
                "pem": pem,
                "fingerprint_sha256": fingerprint,
            }
        ],
    }
    result = store.apply_patch(patch, sample_dir=sample_dir, clock=CLOCK)
    invalidated = {item["leaf_id"] for item in result["invalidation"]["invalidated"]["chains"]}
    assert "leaf-good" in invalidated
    assert "leaf-revoked-ocsp" not in invalidated
    connection = store._connect()
    beta_after = connection.execute(
        "SELECT conclusion_json FROM chain_conclusions "
        "WHERE leaf_id = 'leaf-revoked-ocsp' AND trust_generation = 4"
    ).fetchone()["conclusion_json"]
    connection.close()
    assert beta_after == beta_before

    verified = store.verify_requests(
        [
            {"leaf_id": "leaf-good", "hostname": "good.example.test", "purpose": "serverAuth"},
            {
                "leaf_id": "leaf-revoked-ocsp",
                "hostname": "revoked-ocsp.example.test",
                "purpose": "serverAuth",
            },
        ],
        clock=CLOCK,
    )
    by_id = {item["leaf_id"]: item for item in verified["results"]}
    assert by_id["leaf-good"]["conclusion"]["error_code"] == ErrorCode.SIGNATURE_INVALID
    assert by_id["leaf-revoked-ocsp"]["reused"] is True


def test_lower_patch_generation_is_rollback_error(store):
    patch = {
        "schema": "trust-patch-v1",
        "patch_id": "rollback",
        "logical_clock_ms": BASE_MS,
        "trust_generation": 2,
        "operations": [{"op": "delete", "alias": "root-shared"}],
    }
    with pytest.raises(CertLabError) as caught:
        store.apply_patch(patch, sample_dir=None, clock=CLOCK)
    assert caught.value.code == ErrorCode.GENERATION_ROLLBACK
    assert store.status()["version"] == 1

