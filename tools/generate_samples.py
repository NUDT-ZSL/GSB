#!/usr/bin/env python3
"""Generate the deterministic CertLab sample corpus.

All keys are Ed25519 keys derived from fixed seeds. All certificate and CRL
times, serials, shard contents, and logical clocks are fixed constants.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

BASE_MS = 1_760_000_000_000
NOT_BEFORE = datetime(2025, 1, 1, tzinfo=timezone.utc)
NOT_AFTER = datetime(2031, 1, 1, tzinfo=timezone.utc)
EXPIRED_BEFORE = datetime(2024, 1, 1, tzinfo=timezone.utc)
EXPIRED_AFTER = datetime(2025, 9, 30, tzinfo=timezone.utc)
FUTURE_BEFORE = datetime(2026, 6, 1, tzinfo=timezone.utc)
FUTURE_AFTER = datetime(2030, 6, 1, tzinfo=timezone.utc)
CRL_THIS_UPDATE = datetime(2025, 1, 2, tzinfo=timezone.utc)
CRL_NEXT_UPDATE = datetime(2031, 1, 2, tzinfo=timezone.utc)


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def write_json(path: Path, value: Any) -> bytes:
    raw = (stable_json(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def key_for(role: str) -> ed25519.Ed25519PrivateKey:
    seed = hashlib.sha256(f"certlab-sample-key:{role}".encode("utf-8")).digest()
    return ed25519.Ed25519PrivateKey.from_private_bytes(seed)


def name(common_name: str, organization: str = "CertLab") -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def make_certificate(
    *,
    subject: x509.Name,
    public_key: ed25519.Ed25519PublicKey,
    signing_key: ed25519.Ed25519PrivateKey,
    issuer: x509.Name,
    serial: int,
    not_before: datetime,
    not_after: datetime,
    ca: bool,
    path_length: int | None,
    key_cert_sign: bool,
    crl_sign: bool,
    dns_names: list[str] | None,
    eku: list[x509.ObjectIdentifier] | None,
    issuer_public_key: ed25519.Ed25519PublicKey | None = None,
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(serial)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=ca, path_length=path_length), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=key_cert_sign,
                crl_sign=crl_sign,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    )
    if issuer_public_key is not None:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_public_key),
            critical=False,
        )
    if dns_names is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(value) for value in dns_names]),
            critical=False,
        )
    if eku is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
    return builder.sign(signing_key, algorithm=None)


def make_root(role: str, common_name: str, serial: int) -> tuple[ed25519.Ed25519PrivateKey, x509.Certificate]:
    key = key_for(role)
    subject = name(common_name)
    cert = make_certificate(
        subject=subject,
        public_key=key.public_key(),
        signing_key=key,
        issuer=subject,
        serial=serial,
        not_before=NOT_BEFORE,
        not_after=NOT_AFTER,
        ca=True,
        path_length=4,
        key_cert_sign=True,
        crl_sign=True,
        dns_names=None,
        eku=None,
    )
    return key, cert


def make_intermediate(
    role: str,
    common_name: str,
    serial: int,
    parent_key: ed25519.Ed25519PrivateKey,
    parent_cert: x509.Certificate,
    *,
    path_length: int | None = 2,
    ca: bool = True,
    not_before: datetime = NOT_BEFORE,
    not_after: datetime = NOT_AFTER,
) -> tuple[ed25519.Ed25519PrivateKey, x509.Certificate]:
    key = key_for(role)
    cert = make_certificate(
        subject=name(common_name),
        public_key=key.public_key(),
        signing_key=parent_key,
        issuer=parent_cert.subject,
        serial=serial,
        not_before=not_before,
        not_after=not_after,
        ca=ca,
        path_length=path_length if ca else None,
        key_cert_sign=ca,
        crl_sign=ca,
        dns_names=None,
        eku=None,
        issuer_public_key=parent_cert.public_key(),
    )
    return key, cert


def make_leaf(
    role: str,
    common_name: str,
    serial: int,
    parent_key: ed25519.Ed25519PrivateKey,
    parent_cert: x509.Certificate,
    *,
    dns_names: list[str],
    eku: list[x509.ObjectIdentifier] | None,
    signing_key: ed25519.Ed25519PrivateKey | None = None,
    not_before: datetime = NOT_BEFORE,
    not_after: datetime = NOT_AFTER,
) -> x509.Certificate:
    key = key_for(role)
    return make_certificate(
        subject=name(common_name, organization="CertLab Leaf"),
        public_key=key.public_key(),
        signing_key=signing_key or parent_key,
        issuer=parent_cert.subject,
        serial=serial,
        not_before=not_before,
        not_after=not_after,
        ca=False,
        path_length=None,
        key_cert_sign=False,
        crl_sign=False,
        dns_names=dns_names,
        eku=eku,
        issuer_public_key=parent_cert.public_key(),
    )


def make_crl(
    issuer_key: ed25519.Ed25519PrivateKey,
    issuer_cert: x509.Certificate,
    revoked_serials: list[tuple[int, x509.ReasonFlags | None]],
) -> x509.CertificateRevocationList:
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuer_cert.subject)
        .last_update(CRL_THIS_UPDATE)
        .next_update(CRL_NEXT_UPDATE)
    )
    for serial, reason in revoked_serials:
        entry = (
            x509.RevokedCertificateBuilder()
            .serial_number(serial)
            .revocation_date(CRL_THIS_UPDATE)
        )
        if reason is not None:
            entry = entry.add_extension(x509.CRLReason(reason), critical=False)
        builder = builder.add_revoked_certificate(entry.build())
    return builder.sign(issuer_key, algorithm=None)


def ocsp_signature(
    payload: dict[str, Any], signing_key: ed25519.Ed25519PrivateKey
) -> str:
    signature = signing_key.sign(stable_json(payload).encode("utf-8"))
    return base64.b64encode(signature).decode("ascii")


def ocsp_record(
    *,
    record_id: str,
    issuer_cert: x509.Certificate,
    signing_key: ed25519.Ed25519PrivateKey,
    serial: int,
    status: str,
    reason: str | None = None,
) -> dict[str, Any]:
    payload = {
        "record_id": record_id,
        "issuer_fingerprint_sha256": fingerprint(issuer_cert),
        "serial_number": str(serial),
        "status": status,
        "reason": reason,
        "produced_at_ms": BASE_MS - 60_000,
        "this_update_ms": BASE_MS - 86_400_000,
        "next_update_ms": BASE_MS + 86_400_000,
    }
    result = dict(payload)
    result["signature_algorithm"] = "ed25519"
    result["signature_b64"] = ocsp_signature(payload, signing_key)
    return result


def generate(output: Path) -> dict[str, Any]:
    sample = output
    cas = sample / "cas"
    leaves = sample / "leaves"
    revocation = sample / "revocation"
    v2 = sample / "trust-v2"
    for directory in (cas, leaves, revocation, v2):
        directory.mkdir(parents=True, exist_ok=True)

    root_alpha_key, root_alpha = make_root("root-alpha", "Root Alpha", 0x1001)
    root_beta_key, root_beta = make_root("root-beta", "Root Beta", 0x1002)
    root_legacy_key, root_legacy = make_root("root-legacy", "Root Legacy", 0x1003)
    root_shared_v1_key, root_shared_v1 = make_root("root-shared-v1", "Root Shared", 0x1004)
    root_shared_v2_key, root_shared_v2 = make_root("root-shared-v2", "Root Shared", 0x1005)
    root_alpha_legacy_key, root_alpha_legacy = make_root(
        "root-alpha-legacy", "Root Alpha", 0x1006
    )

    int_alpha_key, int_alpha = make_intermediate(
        "int-alpha", "Intermediate Alpha", 0x2001, root_alpha_key, root_alpha, path_length=3
    )
    int_beta_key, int_beta = make_intermediate(
        "int-beta", "Intermediate Beta", 0x2002, root_beta_key, root_beta, path_length=2
    )
    int_unknown_key, int_unknown = make_intermediate(
        "int-unknown", "Intermediate Unknown", 0x2003, root_alpha_key, root_alpha, path_length=2
    )
    int_revoked_key, int_revoked = make_intermediate(
        "int-revoked", "Intermediate Revoked", 0x2004, root_alpha_key, root_alpha, path_length=2
    )
    int_pathlen_key, int_pathlen = make_intermediate(
        "int-pathlen", "Intermediate PathLen Zero", 0x2005, int_alpha_key, int_alpha, path_length=0
    )
    int_child_key, int_child = make_intermediate(
        "int-child", "Intermediate Child", 0x2006, int_pathlen_key, int_pathlen, path_length=0
    )
    int_notca_key, int_notca = make_intermediate(
        "int-notca", "Intermediate Not CA", 0x2007, int_alpha_key, int_alpha,
        path_length=None, ca=False
    )

    all_cas = [
        ("root-alpha", root_alpha),
        ("root-beta", root_beta),
        ("root-legacy", root_legacy),
        ("root-shared-v1", root_shared_v1),
        ("root-shared-v2", root_shared_v2),
        ("root-alpha-legacy", root_alpha_legacy),
        ("int-alpha", int_alpha),
        ("int-beta", int_beta),
        ("int-unknown", int_unknown),
        ("int-revoked", int_revoked),
        ("int-pathlen", int_pathlen),
        ("int-child", int_child),
        ("int-notca", int_notca),
    ]
    for role, cert in all_cas:
        (cas / f"{role}.pem").write_text(pem(cert), encoding="ascii")

    server_auth = [ExtendedKeyUsageOID.SERVER_AUTH]
    client_auth = [ExtendedKeyUsageOID.CLIENT_AUTH]
    serials: dict[str, int] = {}

    def add_leaf(
        leaf_id: str,
        serial: int,
        hostname: str,
        purpose: str,
        issuer_key: ed25519.Ed25519PrivateKey,
        issuer_cert: x509.Certificate,
        *,
        expected_hostname: str | None = None,
        expect_error: str | None = None,
        dns_names: list[str] | None = None,
        eku: list[x509.ObjectIdentifier] | None = None,
        signing_key: ed25519.Ed25519PrivateKey | None = None,
        not_before: datetime = NOT_BEFORE,
        not_after: datetime = NOT_AFTER,
    ) -> dict[str, Any]:
        serials[leaf_id] = serial
        cert = make_leaf(
            leaf_id,
            f"Leaf {leaf_id}",
            serial,
            issuer_key,
            issuer_cert,
            dns_names=dns_names or [hostname],
            eku=eku if eku is not None else server_auth,
            signing_key=signing_key,
            not_before=not_before,
            not_after=not_after,
        )
        (leaves / f"{leaf_id}.pem").write_text(pem(cert), encoding="ascii")
        return {
            "id": leaf_id,
            "file": f"{leaf_id}.pem",
            "hostname": expected_hostname or hostname,
            "purpose": purpose,
            "expect_error": expect_error,
            "serial_number": str(serial),
            "issuer_fingerprint_sha256": fingerprint(issuer_cert),
        }

    leaf_entries = [
        add_leaf(
            "leaf-good", 0x3001, "good.example.test", "serverAuth", int_alpha_key, int_alpha
        ),
        add_leaf(
            "leaf-wild",
            0x3002,
            "api.wild.example.test",
            "serverAuth",
            int_alpha_key,
            int_alpha,
            dns_names=["*.wild.example.test"],
        ),
        add_leaf(
            "leaf-expired",
            0x3003,
            "expired.example.test",
            "serverAuth",
            int_alpha_key,
            int_alpha,
            expect_error="E_CERT_EXPIRED",
            not_before=EXPIRED_BEFORE,
            not_after=EXPIRED_AFTER,
        ),
        add_leaf(
            "leaf-badhost",
            0x3004,
            "right.example.test",
            "serverAuth",
            int_alpha_key,
            int_alpha,
            expected_hostname="wrong.example.test",
            expect_error="E_HOSTNAME_MISMATCH",
        ),
        add_leaf(
            "leaf-revoked-crl",
            0x3005,
            "revoked-crl.example.test",
            "serverAuth",
            int_alpha_key,
            int_alpha,
            expect_error="E_REVOKED",
        ),
        add_leaf(
            "leaf-revoked-ocsp",
            0x3006,
            "revoked-ocsp.example.test",
            "serverAuth",
            int_beta_key,
            int_beta,
            expect_error="E_REVOKED",
        ),
        add_leaf(
            "leaf-unknown",
            0x3007,
            "unknown.example.test",
            "serverAuth",
            int_unknown_key,
            int_unknown,
            expect_error="E_REVOCATION_UNKNOWN",
        ),
        add_leaf(
            "leaf-badsig",
            0x3008,
            "badsig.example.test",
            "serverAuth",
            int_alpha_key,
            int_alpha,
            expect_error="E_SIGNATURE_INVALID",
            signing_key=key_for("rogue-signer"),
        ),
        add_leaf(
            "leaf-under-revoked-int",
            0x3009,
            "child-of-revoked.example.test",
            "serverAuth",
            int_revoked_key,
            int_revoked,
            expect_error="E_REVOKED",
        ),
        add_leaf(
            "leaf-wrongku",
            0x300A,
            "wrongku.example.test",
            "serverAuth",
            int_alpha_key,
            int_alpha,
            expect_error="E_KEY_USAGE",
            eku=client_auth,
        ),
        add_leaf(
            "leaf-notyetvalid",
            0x300B,
            "future.example.test",
            "serverAuth",
            int_alpha_key,
            int_alpha,
            expect_error="E_CERT_NOT_YET_VALID",
            not_before=FUTURE_BEFORE,
            not_after=FUTURE_AFTER,
        ),
        add_leaf(
            "leaf-pathlen",
            0x300C,
            "pathlen.example.test",
            "serverAuth",
            int_child_key,
            int_child,
            expect_error="E_PATH_LENGTH",
        ),
        add_leaf(
            "leaf-notca",
            0x300D,
            "notca.example.test",
            "serverAuth",
            int_notca_key,
            int_notca,
            expect_error="E_BASIC_CONSTRAINTS",
        ),
    ]
    write_json(
        leaves / "index.json",
        {"schema": "leaf-index-v1", "clock_ms": BASE_MS, "leaves": leaf_entries},
    )

    crl_alpha = make_crl(
        int_alpha_key,
        int_alpha,
        [(serials["leaf-revoked-crl"], x509.ReasonFlags.key_compromise)],
    )
    crl_beta = make_crl(int_beta_key, int_beta, [])
    crl_root_alpha = make_crl(root_alpha_key, root_alpha, [])
    crl_root_beta = make_crl(root_beta_key, root_beta, [])
    crl_revoked_int = make_crl(int_revoked_key, int_revoked, [])
    crl_child = make_crl(int_child_key, int_child, [])
    (revocation / "crl-int-alpha.pem").write_bytes(crl_alpha.public_bytes(serialization.Encoding.PEM))
    (revocation / "crl-int-beta.pem").write_bytes(crl_beta.public_bytes(serialization.Encoding.PEM))
    (revocation / "crl-root-alpha.pem").write_bytes(
        crl_root_alpha.public_bytes(serialization.Encoding.PEM)
    )
    (revocation / "crl-root-beta.pem").write_bytes(
        crl_root_beta.public_bytes(serialization.Encoding.PEM)
    )
    (revocation / "crl-int-revoked.pem").write_bytes(
        crl_revoked_int.public_bytes(serialization.Encoding.PEM)
    )
    (revocation / "crl-int-child.pem").write_bytes(
        crl_child.public_bytes(serialization.Encoding.PEM)
    )
    ocsp_records = [
        ocsp_record(
            record_id="ocsp-good",
            issuer_cert=int_alpha,
            signing_key=int_alpha_key,
            serial=serials["leaf-good"],
            status="good",
        ),
        ocsp_record(
            record_id="ocsp-revoked-leaf",
            issuer_cert=int_beta,
            signing_key=int_beta_key,
            serial=serials["leaf-revoked-ocsp"],
            status="revoked",
            reason="key_compromise",
        ),
        ocsp_record(
            record_id="ocsp-revoked-intermediate",
            issuer_cert=root_alpha,
            signing_key=root_alpha_key,
            serial=int_revoked.serial_number,
            status="revoked",
            reason="ca_compromise",
        ),
    ]
    write_json(
        revocation / "ocsp-records.json",
        {"schema": "ocsp-records-v1", "records": ocsp_records},
    )

    v1_entries = [
        {
            "alias": "root-alpha",
            "pem": pem(root_alpha_legacy),
            "fingerprint_sha256": fingerprint(root_alpha_legacy),
            "trust_seconds": 120,
            "enabled": True,
        },
        {
            "alias": "root-shared",
            "pem": pem(root_shared_v1),
            "fingerprint_sha256": fingerprint(root_shared_v1),
            "trust_seconds": 60,
            "enabled": True,
        },
        {
            "alias": "root-legacy",
            "pem": pem(root_legacy),
            "fingerprint_sha256": fingerprint(root_legacy),
            "trust_seconds": 5,
            "enabled": True,
        },
    ]
    write_json(
        sample / "trust-v1.json",
        {
            "schema": "trust-v1",
            "logical_clock_ms": 1_700_000_000_000,
            "certificates": v1_entries,
        },
    )

    shard_zero_entries = [
        {
            "alias": "root-alpha",
            "canonical_alias": "root-alpha",
            "pem": pem(root_alpha),
            "fingerprint_sha256": fingerprint(root_alpha),
            "effective_at_ms": BASE_MS - 86_400_000,
            "trust_generation": 3,
            "enabled": True,
            "tombstone": "none",
            "revocation": "unknown",
        },
        {
            "alias": "root-shared",
            "canonical_alias": "root-shared",
            "pem": pem(root_shared_v2),
            "fingerprint_sha256": fingerprint(root_shared_v2),
            "effective_at_ms": BASE_MS - 43_200_000,
            "trust_generation": 3,
            "enabled": True,
            "tombstone": "none",
            "revocation": "unknown",
        },
    ]
    shard_one_entries = [
        {
            "alias": "root-beta",
            "canonical_alias": "root-beta",
            "pem": pem(root_beta),
            "fingerprint_sha256": fingerprint(root_beta),
            "effective_at_ms": BASE_MS - 21_600_000,
            "trust_generation": 3,
            "enabled": True,
            "tombstone": "none",
            "revocation": "unknown",
        },
        {
            "alias": "root-legacy",
            "canonical_alias": "root-legacy",
            "pem": pem(root_legacy),
            "fingerprint_sha256": fingerprint(root_legacy),
            "effective_at_ms": BASE_MS - 10_800_000,
            "trust_generation": 3,
            "enabled": False,
            "tombstone": "tombstoned",
            "revocation": "unknown",
        },
    ]
    shard_zero_path = v2 / "shard-000.json"
    shard_one_path = v2 / "shard-001.json"
    write_json(
        shard_zero_path,
        {
            "schema": "trust-v2-shard",
            "logical_clock_ms": 1_750_000_000_000,
            "generation": 3,
            "entries": shard_zero_entries,
        },
    )
    write_json(
        shard_one_path,
        {
            "schema": "trust-v2-shard",
            "logical_clock_ms": 1_750_000_000_000,
            "generation": 3,
            "entries": shard_one_entries,
        },
    )

    database_path = v2 / "metadata.sqlite"
    if database_path.exists():
        database_path.unlink()
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE snapshot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE generation_history (
                generation INTEGER PRIMARY KEY,
                logical_clock_ms INTEGER NOT NULL
            );
            CREATE TABLE anchors (
                alias TEXT PRIMARY KEY,
                shard_path TEXT NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                trust_generation INTEGER NOT NULL,
                tombstone TEXT NOT NULL,
                revocation TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                fingerprint_sha256 TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO snapshot_meta(key, value) VALUES (?, ?)",
            [
                ("schema_version", "trust-v2"),
                ("logical_clock_ms", "1750000000000"),
                ("trust_generation", "3"),
            ],
        )
        connection.executemany(
            "INSERT INTO generation_history(generation, logical_clock_ms) VALUES (?, ?)",
            [
                (1, 1_730_000_000_000),
                (2, 1_740_000_000_000),
                (3, 1_750_000_000_000),
            ],
        )
        for shard_path, entries in (
            (shard_zero_path, shard_zero_entries),
            (shard_one_path, shard_one_entries),
        ):
            for entry in entries:
                connection.execute(
                    "INSERT INTO anchors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["alias"],
                        shard_path.name,
                        entry["effective_at_ms"],
                        entry["trust_generation"],
                        entry["tombstone"],
                        entry["revocation"],
                        1 if entry["enabled"] else 0,
                        entry["fingerprint_sha256"],
                    ),
                )
        connection.commit()
    finally:
        connection.close()

    write_json(
        v2 / "manifest.json",
        {
            "schema": "trust-v2",
            "logical_clock_ms": 1_750_000_000_000,
            "trust_generation": 3,
            "database": "metadata.sqlite",
            "shards": [
                {
                    "path": shard_zero_path.name,
                    "sha256": hashlib.sha256(shard_zero_path.read_bytes()).hexdigest(),
                },
                {
                    "path": shard_one_path.name,
                    "sha256": hashlib.sha256(shard_one_path.read_bytes()).hexdigest(),
                },
            ],
        },
    )

    summary = {
        "sample_dir": str(sample),
        "clock_ms": BASE_MS,
        "v1_snapshot": str(sample / "trust-v1.json"),
        "v2_snapshot": str(v2),
        "leaf_count": len(leaf_entries),
        "cas": [role for role, _ in all_cas],
        "expected_errors": {
            entry["id"]: entry["expect_error"]
            for entry in leaf_entries
            if entry["expect_error"]
        },
    }
    write_json(sample / "sample-manifest.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="sample")
    args = parser.parse_args()
    summary = generate(Path(args.output))
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

