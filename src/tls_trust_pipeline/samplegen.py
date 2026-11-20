from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .canonical import b64e, canonical_fingerprint, canonical_json, sha256_file

CLOCK_MS = 1_735_689_600_000


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def _name(common_name: str, organization: str = "Offline TLS Test") -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


@dataclass(frozen=True)
class Entity:
    name: str
    common_name: str
    key_name: str
    cert_name: str


ROOT_A = Entity("root-a", "Offline Root A", "root-a.key.pem", "root-a.pem")
ROOT_B = Entity("root-b", "Offline Root B", "root-b.key.pem", "root-b.pem")
ROOT_RETIRED = Entity(
    "root-retired", "Offline Retired Root", "root-retired.key.pem", "root-retired.pem"
)
INT_A = Entity("intermediate-a", "Offline Intermediate A", "intermediate-a.key.pem", "intermediate-a.pem")
INT_B = Entity("intermediate-b", "Offline Intermediate B", "intermediate-b.key.pem", "intermediate-b.pem")
INT_RETIRED = Entity(
    "intermediate-retired",
    "Offline Intermediate Retired",
    "intermediate-retired.key.pem",
    "intermediate-retired.pem",
)
INT_PATH = Entity(
    "intermediate-path",
    "Offline Path Violation Intermediate",
    "intermediate-path.key.pem",
    "intermediate-path.pem",
)

LEAF_GOOD = Entity("leaf-good", "api.example.test", "leaf-good.key.pem", "leaf-good.pem")
LEAF_WILDCARD = Entity(
    "leaf-wildcard", "*.example.test", "leaf-wildcard.key.pem", "leaf-wildcard.pem"
)
LEAF_CLIENT = Entity(
    "leaf-client", "client.example.test", "leaf-client.key.pem", "leaf-client.pem"
)
LEAF_REVOKED = Entity(
    "leaf-revoked", "revoked.example.test", "leaf-revoked.key.pem", "leaf-revoked.pem"
)
LEAF_UNKNOWN = Entity(
    "leaf-unknown", "unknown.example.test", "leaf-unknown.key.pem", "leaf-unknown.pem"
)
LEAF_RETIRED = Entity(
    "leaf-retired", "retired.example.test", "leaf-retired.key.pem", "leaf-retired.pem"
)
LEAF_PATH = Entity(
    "leaf-path", "path.example.test", "leaf-path.key.pem", "leaf-path.pem"
)
LEAF_EXPIRED = Entity(
    "leaf-expired", "expired.example.test", "leaf-expired.key.pem", "leaf-expired.pem"
)
LEAF_ROOT_B = Entity(
    "leaf-root-b", "rootb.example.test", "leaf-root-b.key.pem", "leaf-root-b.pem"
)

ALL_ENTITIES = [
    ROOT_A,
    ROOT_B,
    ROOT_RETIRED,
    INT_A,
    INT_B,
    INT_RETIRED,
    INT_PATH,
    LEAF_GOOD,
    LEAF_WILDCARD,
    LEAF_CLIENT,
    LEAF_REVOKED,
    LEAF_UNKNOWN,
    LEAF_RETIRED,
    LEAF_PATH,
    LEAF_EXPIRED,
    LEAF_ROOT_B,
]


def _load_or_create_key(path: Path) -> rsa.RSAPrivateKey:
    if path.exists():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise RuntimeError(f"unexpected key type in {path}")
        return loaded
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key


def _build_cert(
    *,
    subject: x509.Name,
    issuer: x509.Name,
    public_key,
    signer_key: rsa.RSAPrivateKey,
    serial: int,
    not_before_ms: int,
    not_after_ms: int,
    ca: bool,
    path_length: int | None,
    key_usage: x509.KeyUsage | None,
    eku: list[x509.ObjectIdentifier] | None,
    san: list[str] | None,
    crl_uri: str | None,
    ocsp_uri: str | None,
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(serial)
        .not_valid_before(_dt(not_before_ms))
        .not_valid_after(_dt(not_after_ms))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=path_length), critical=True)
    )
    if key_usage is not None:
        builder = builder.add_extension(key_usage, critical=True)
    if eku:
        builder = builder.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in san]), critical=False
        )
    if crl_uri:
        builder = builder.add_extension(
            x509.CRLDistributionPoints(
                [
                    x509.DistributionPoint(
                        full_name=[x509.UniformResourceIdentifier(crl_uri)],
                        relative_name=None,
                        reasons=None,
                        crl_issuer=None,
                    )
                ]
            ),
            critical=False,
        )
    if ocsp_uri:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess(
                [
                    x509.AccessDescription(
                        x509.oid.AuthorityInformationAccessOID.OCSP,
                        x509.UniformResourceIdentifier(ocsp_uri),
                    )
                ]
            ),
            critical=False,
        )
    return builder.sign(private_key=signer_key, algorithm=hashes.SHA256())


def _ca_ku() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )


def _leaf_ku() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=True,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _record_signature(record: dict, key: rsa.RSAPrivateKey) -> dict:
    signed = dict(record)
    signature = key.sign(canonical_json(signed), padding.PKCS1v15(), hashes.SHA256())
    signed["signature_algorithm"] = "rsa-pkcs1v15-sha256"
    signed["signature_b64"] = b64e(signature)
    return signed


def generate_sample(root: Path) -> dict:
    cert_dir = root / "certs"
    key_dir = root / "fixtures" / "keys"
    trust_v1_dir = root / "trust" / "v1"
    trust_v2_dir = root / "trust" / "v2"
    shard_dir = trust_v2_dir / "shards"
    request_dir = root / "requests"
    revocation_dir = root / "revocation"
    patch_dir = root / "patches"
    for directory in (
        cert_dir,
        key_dir,
        trust_v1_dir,
        trust_v2_dir,
        shard_dir,
        request_dir,
        revocation_dir,
        patch_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    keys = {entity.name: _load_or_create_key(key_dir / entity.key_name) for entity in ALL_ENTITIES}

    root_valid_from = 1_704_067_200_000
    root_valid_to = 2_019_686_400_000
    intermediate_valid_from = 1_704_067_200_000
    intermediate_valid_to = 1_956_528_000_000
    leaf_valid_from = 1_704_067_200_000
    leaf_valid_to = 1_893_456_000_000
    expired_valid_to = 1_717_200_000_000

    certs: dict[str, x509.Certificate] = {}
    for entity, path_length in ((ROOT_A, 2), (ROOT_B, 2), (ROOT_RETIRED, 2)):
        key = keys[entity.name]
        name = _name(entity.common_name)
        cert = _build_cert(
            subject=name,
            issuer=name,
            public_key=key.public_key(),
            signer_key=key,
            serial=1000 + ALL_ENTITIES.index(entity),
            not_before_ms=root_valid_from,
            not_after_ms=root_valid_to,
            ca=True,
            path_length=path_length,
            key_usage=_ca_ku(),
            eku=None,
            san=None,
            crl_uri=f"http://offline.invalid/crl/{entity.name}.crl",
            ocsp_uri=None,
        )
        certs[entity.name] = cert
        _write_cert(cert_dir / entity.cert_name, cert)

    intermediate_specs = [
        (INT_A, ROOT_A, 0),
        (INT_B, ROOT_B, 0),
        (INT_RETIRED, ROOT_RETIRED, 0),
        (INT_PATH, INT_A, 0),
    ]
    for entity, parent, path_length in intermediate_specs:
        key = keys[entity.name]
        parent_cert = certs[parent.name]
        cert = _build_cert(
            subject=_name(entity.common_name),
            issuer=parent_cert.subject,
            public_key=key.public_key(),
            signer_key=keys[parent.name],
            serial=2000 + ALL_ENTITIES.index(entity),
            not_before_ms=intermediate_valid_from,
            not_after_ms=intermediate_valid_to,
            ca=True,
            path_length=path_length,
            key_usage=_ca_ku(),
            eku=None,
            san=None,
            crl_uri=f"http://offline.invalid/crl/{entity.name}.crl",
            ocsp_uri=None,
        )
        certs[entity.name] = cert
        _write_cert(cert_dir / entity.cert_name, cert)

    leaf_specs = [
        (LEAF_GOOD, INT_A, "api.example.test", ExtendedKeyUsageOID.SERVER_AUTH, leaf_valid_to),
        (LEAF_WILDCARD, INT_A, "*.example.test", ExtendedKeyUsageOID.SERVER_AUTH, leaf_valid_to),
        (LEAF_CLIENT, INT_A, "client.example.test", ExtendedKeyUsageOID.CLIENT_AUTH, leaf_valid_to),
        (LEAF_REVOKED, INT_A, "revoked.example.test", ExtendedKeyUsageOID.SERVER_AUTH, leaf_valid_to),
        (LEAF_UNKNOWN, INT_A, "unknown.example.test", ExtendedKeyUsageOID.SERVER_AUTH, leaf_valid_to),
        (LEAF_RETIRED, INT_RETIRED, "retired.example.test", ExtendedKeyUsageOID.SERVER_AUTH, leaf_valid_to),
        (LEAF_PATH, INT_PATH, "path.example.test", ExtendedKeyUsageOID.SERVER_AUTH, leaf_valid_to),
        (LEAF_EXPIRED, INT_A, "expired.example.test", ExtendedKeyUsageOID.SERVER_AUTH, expired_valid_to),
        (LEAF_ROOT_B, INT_B, "rootb.example.test", ExtendedKeyUsageOID.SERVER_AUTH, leaf_valid_to),
    ]
    for entity, parent, hostname, purpose, not_after in leaf_specs:
        cert = _build_cert(
            subject=_name(entity.common_name),
            issuer=certs[parent.name].subject,
            public_key=keys[entity.name].public_key(),
            signer_key=keys[parent.name],
            serial=3000 + ALL_ENTITIES.index(entity),
            not_before_ms=leaf_valid_from,
            not_after_ms=not_after,
            ca=False,
            path_length=None,
            key_usage=_leaf_ku(),
            eku=[purpose],
            san=[hostname],
            crl_uri=f"http://offline.invalid/crl/{parent.name}.crl",
            ocsp_uri="http://offline.invalid/ocsp",
        )
        certs[entity.name] = cert
        _write_cert(cert_dir / entity.cert_name, cert)

    def fp(name: str) -> str:
        return canonical_fingerprint(certs[name].fingerprint(hashes.SHA256()).hex())

    def serial(name: str) -> str:
        return format(certs[name].serial_number, "x")

    records = [
        _record_signature(
            {
                "source": "crl",
                "issuer_cert_file": ROOT_A.cert_name,
                "issued_at_ms": 1_735_689_600_000,
                "next_update_ms": 1_798_000_000_000,
                "entries": [
                    {"serial_hex": serial(INT_A.name), "status": "good"},
                    {"serial_hex": serial(INT_PATH.name), "status": "good"},
                ],
            },
            keys[ROOT_A.name],
        ),
        _record_signature(
            {
                "source": "crl",
                "issuer_cert_file": ROOT_B.cert_name,
                "issued_at_ms": 1_735_689_600_000,
                "next_update_ms": 1_798_000_000_000,
                "entries": [{"serial_hex": serial(INT_B.name), "status": "good"}],
            },
            keys[ROOT_B.name],
        ),
        _record_signature(
            {
                "source": "crl",
                "issuer_cert_file": ROOT_RETIRED.cert_name,
                "issued_at_ms": 1_735_689_600_000,
                "next_update_ms": 1_798_000_000_000,
                "entries": [{"serial_hex": serial(INT_RETIRED.name), "status": "good"}],
            },
            keys[ROOT_RETIRED.name],
        ),
        _record_signature(
            {
                "source": "crl",
                "issuer_cert_file": INT_A.cert_name,
                "issued_at_ms": 1_735_689_600_000,
                "next_update_ms": 1_798_000_000_000,
                "entries": [
                    {"serial_hex": serial(LEAF_GOOD.name), "status": "good"},
                    {"serial_hex": serial(LEAF_WILDCARD.name), "status": "good"},
                    {"serial_hex": serial(LEAF_CLIENT.name), "status": "good"},
                    {
                        "serial_hex": serial(LEAF_REVOKED.name),
                        "status": "revoked",
                        "revoked_at_ms": 1_735_000_000_000,
                        "reason": "key_compromise",
                    },
                    {"serial_hex": serial(LEAF_EXPIRED.name), "status": "good"},
                    {"serial_hex": serial(INT_PATH.name), "status": "good"},
                ],
            },
            keys[INT_A.name],
        ),
        _record_signature(
            {
                "source": "ocsp",
                "issuer_cert_file": INT_A.cert_name,
                "issued_at_ms": 1_735_689_600_000,
                "next_update_ms": 1_798_000_000_000,
                "entries": [
                    {"cert_fingerprint": fp(LEAF_WILDCARD.name), "status": "good"}
                ],
            },
            keys[INT_A.name],
        ),
        _record_signature(
            {
                "source": "crl",
                "issuer_cert_file": INT_PATH.cert_name,
                "issued_at_ms": 1_735_689_600_000,
                "next_update_ms": 1_798_000_000_000,
                "entries": [{"serial_hex": serial(LEAF_PATH.name), "status": "good"}],
            },
            keys[INT_PATH.name],
        ),
        _record_signature(
            {
                "source": "crl",
                "issuer_cert_file": INT_B.cert_name,
                "issued_at_ms": 1_735_689_600_000,
                "next_update_ms": 1_798_000_000_000,
                "entries": [{"serial_hex": serial(LEAF_ROOT_B.name), "status": "good"}],
            },
            keys[INT_B.name],
        ),
        _record_signature(
            {
                "source": "crl",
                "issuer_cert_file": INT_RETIRED.cert_name,
                "issued_at_ms": 1_735_689_600_000,
                "next_update_ms": 1_798_000_000_000,
                "entries": [{"serial_hex": serial(LEAF_RETIRED.name), "status": "good"}],
            },
            keys[INT_RETIRED.name],
        ),
    ]
    evidence = {"schema": 1, "records": records}
    (revocation_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    v1_anchors = []
    for entity, seconds in ((ROOT_A, 315_360_000), (ROOT_RETIRED, 315_360_000)):
        v1_anchors.append(
            {
                "alias": entity.name,
                "cert_file": entity.cert_name,
                "fingerprint": fp(entity.name),
                "trust_seconds": seconds,
            }
        )
    v1 = {
        "schema": 1,
        "snapshot_version": "trust-v1",
        "generation": 1,
        "anchors": sorted(v1_anchors, key=lambda item: item["alias"]),
    }
    (trust_v1_dir / "trust-v1.json").write_text(
        json.dumps(v1, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    v2_anchors = []
    for entity in (ROOT_A, ROOT_B, ROOT_RETIRED):
        v2_anchors.append(
            {
                "alias": entity.name,
                "cert_file": entity.cert_name,
                "fingerprint": fp(entity.name),
                "not_before_ms": 1_704_067_200_000,
                "not_after_ms": 1_893_456_000_000,
                "trust_generation": 2,
                "tombstone": False,
                "can_issue_intermediates": True,
            }
        )
    anchor_shard = shard_dir / "anchors-000.json"
    anchor_shard.write_text(
        json.dumps(
            {"schema": 2, "anchors": sorted(v2_anchors, key=lambda item: item["alias"])},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    extra_shard = shard_dir / "anchors-001.json"
    extra_shard.write_text(
        json.dumps({"schema": 2, "anchors": []}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metadata_path = trust_v2_dir / "metadata.sqlite"
    if metadata_path.exists():
        metadata_path.unlink()
    connection = sqlite3.connect(metadata_path)
    try:
        connection.executescript(
            """
            CREATE TABLE snapshot_meta (
                version TEXT NOT NULL PRIMARY KEY,
                generation INTEGER NOT NULL,
                created_clock_ms INTEGER NOT NULL
            );
            CREATE TABLE anchors (
                seq INTEGER PRIMARY KEY,
                alias TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL UNIQUE,
                not_before_ms INTEGER NOT NULL,
                not_after_ms INTEGER NOT NULL,
                trust_generation INTEGER NOT NULL,
                tombstone INTEGER NOT NULL CHECK (tombstone IN (0, 1)),
                can_issue_intermediates INTEGER NOT NULL
                    CHECK (can_issue_intermediates IN (0, 1)),
                CHECK (tombstone = 0 OR can_issue_intermediates = 0)
            );
            """
        )
        connection.execute(
            "INSERT INTO snapshot_meta(version, generation, created_clock_ms) VALUES (?, ?, ?)",
            ("trust-v2", 2, CLOCK_MS),
        )
        for seq, anchor in enumerate(sorted(v2_anchors, key=lambda item: item["alias"]), start=1):
            connection.execute(
                """
                INSERT INTO anchors(
                    seq, alias, fingerprint, not_before_ms, not_after_ms,
                    trust_generation, tombstone, can_issue_intermediates
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    seq,
                    anchor["alias"],
                    anchor["fingerprint"],
                    anchor["not_before_ms"],
                    anchor["not_after_ms"],
                    anchor["trust_generation"],
                    int(anchor["tombstone"]),
                    int(anchor["can_issue_intermediates"]),
                ),
            )
        connection.commit()
    finally:
        connection.close()

    manifest = {
        "schema": 2,
        "snapshot_version": "trust-v2",
        "generation": 2,
        "created_clock_ms": CLOCK_MS,
        "metadata_db": "metadata.sqlite",
        "shards": [
            {
                "path": f"shards/{anchor_shard.name}",
                "sha256": sha256_file(anchor_shard),
            },
            {
                "path": f"shards/{extra_shard.name}",
                "sha256": sha256_file(extra_shard),
            },
        ],
    }
    (trust_v2_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    requests = [
        ("r01", "api.example.test", "tls-server", CLOCK_MS, False, LEAF_GOOD.cert_name),
        ("r02", "foo.example.test", "tls-server", CLOCK_MS, True, LEAF_WILDCARD.cert_name),
        ("r03", "wrong.example.test", "tls-server", CLOCK_MS, False, LEAF_GOOD.cert_name),
        ("r04", "client.example.test", "tls-client", CLOCK_MS, False, LEAF_CLIENT.cert_name),
        ("r05", "revoked.example.test", "tls-server", CLOCK_MS, True, LEAF_REVOKED.cert_name),
        ("r06", "unknown.example.test", "tls-server", CLOCK_MS, True, LEAF_UNKNOWN.cert_name),
        ("r07", "retired.example.test", "tls-server", CLOCK_MS, False, LEAF_RETIRED.cert_name),
        ("r08", "path.example.test", "tls-server", CLOCK_MS, False, LEAF_PATH.cert_name),
        ("r09", "expired.example.test", "tls-server", CLOCK_MS, False, LEAF_EXPIRED.cert_name),
        ("r10", "rootb.example.test", "tls-server", CLOCK_MS, True, LEAF_ROOT_B.cert_name),
    ]
    request_doc = {
        "schema": 1,
        "requests": [
            {
                "request_id": request_id,
                "hostname": hostname,
                "purpose": purpose,
                "logical_clock_ms": clock,
                "require_revocation": require_revocation,
                "leaf_file": leaf_file,
            }
            for request_id, hostname, purpose, clock, require_revocation, leaf_file in requests
        ],
    }
    (request_dir / "handshake-requests.json").write_text(
        json.dumps(request_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    patch_retired = {
        "schema": 1,
        "patch_id": "tombstone-root-retired",
        "base_version": "trust-v2",
        "target_version": "trust-v2-p1",
        "target_generation": 3,
        "effective_clock_ms": CLOCK_MS,
        "operations": [
            {
                "op": "tombstone",
                "alias": ROOT_RETIRED.name,
                "revoked_at_ms": CLOCK_MS,
                "reason": "retired_by_operator",
            }
        ],
    }
    patch_root_a = {
        "schema": 1,
        "patch_id": "tombstone-root-a",
        "base_version": "trust-v2",
        "target_version": "trust-v2-root-a-tombstone",
        "target_generation": 3,
        "effective_clock_ms": CLOCK_MS,
        "operations": [
            {
                "op": "tombstone",
                "alias": ROOT_A.name,
                "revoked_at_ms": CLOCK_MS,
                "reason": "anchor_compromise_drill",
            }
        ],
    }
    for filename, document in (
        ("p1-tombstone-root-retired.json", patch_retired),
        ("p2-tombstone-root-a.json", patch_root_a),
    ):
        (patch_dir / filename).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    summary = {
        "clock_ms": CLOCK_MS,
        "cert_count": len(ALL_ENTITIES),
        "v1_snapshot": "trust/v1/trust-v1.json",
        "v2_snapshot": "trust/v2/manifest.json",
        "requests": "requests/handshake-requests.json",
        "revocation_evidence": "revocation/evidence.json",
        "patches": ["patches/p1-tombstone-root-retired.json", "patches/p2-tombstone-root-a.json"],
        "fingerprints": {entity.name: fp(entity.name) for entity in ALL_ENTITIES},
    }
    (root / "sample-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def generate_default_sample(project_root: Path) -> dict:
    return generate_sample(project_root / "sample")

