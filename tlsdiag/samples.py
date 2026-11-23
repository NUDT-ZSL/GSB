"""Generate the deterministic local sample bundle.

The bundle contains two generations of trust stores, a small CA topology, leaf
certificates with expectations, CRL/OCSP evidence records, a logical clock
step table and the precision configuration.  Everything is written under a
single sample root using relative paths and requires no network access.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from cryptography import x509

from . import certgen

EPOCH_MS = 1_700_000_000_000

# Validity windows (ms).  Two leaves are deliberately placed milliseconds from
# the verification instant so that precision-sensitive boundary comparisons
# exercise the dual-path ledger.
ROOT_VALIDITY = 10 * 365 * 24 * 3600 * 1000
INT_VALIDITY = 5 * 365 * 24 * 3600 * 1000
LEAF_VALIDITIES = {
    "good": 90 * 24 * 3600 * 1000,
    "wildcard": 90 * 24 * 3600 * 1000,
    # cryptography validity is second-resolution, so boundary leaves use whole
    # seconds: expiring lasts 2 days + 300 s, expired lasts only 1 day.
    "expiring": 2 * 24 * 3600 * 1000 + 250 * 1000,
    "reverify": 2 * 24 * 3600 * 1000,
    "revoked": 30 * 24 * 3600 * 1000,
    "expired": 1 * 24 * 3600 * 1000,
}

CLOCK_STEPS_MS = [
    0,
    60_000,
    60_000,
    123,
    24 * 3600 * 1000,
    0,
    3600 * 1000 + 250,
    7,
]
# Verification happens at tick 6: epoch + 2d + 1h + 250ms.  The "expired" leaf
# (1d lifetime) is long expired; the "expiring" leaf has a whole-second expiry
# only 750 ms away, inside the 500 ms-class precision boundary analysis.
VERIFY_TICK = 6

PRECISION = {
    # Deliberately awkward fractional factor: 1000 ms/s represented in Q12,
    # combined with the bounded accumulator, yields reproducible rounding.
    "ms_per_second_num": 1000,
    "ms_per_second_den": 1,
    "frac_bits": 12,
    "rounding": "ROUND_HALF_EVEN",
    "sensitive_tolerance_ms": 500,
    "reverify_remaining_ms": 2 * 24 * 3600 * 1000 + 250 * 1000,
}


def _sha256_file_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes | str) -> None:
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def generate_sample(root: str | Path) -> dict:
    root = Path(root)
    cert_dir = root / "certs"
    v1_dir = root / "truststore_v1"
    v2_dir = root / "truststore_v2"
    rev_dir = root / "revocation"
    for directory in (root, cert_dir, v1_dir, v2_dir, rev_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # ----- CA topology -----------------------------------------------------
    root_key, root_cert = certgen.make_ca(
        "Root CA", None, None, serial=1001, epoch_ms=EPOCH_MS,
        validity_ms=ROOT_VALIDITY, path_length=2, is_root=True,
    )
    int_key, int_cert = certgen.make_ca(
        "Intermediate CA", root_key, root_cert.subject, serial=2001,
        epoch_ms=EPOCH_MS, validity_ms=INT_VALIDITY, path_length=0,
    )

    leaf_specs = {
        "good": (["good.example.com"], ["serverAuth"], "good.example.com"),
        "wildcard": (["*.example.com"], ["serverAuth"], "api.example.com"),
        "expiring": (["expiring.example.com"], ["serverAuth"], "expiring.example.com"),
        "reverify": (["reverify.example.com"], ["serverAuth"], "reverify.example.com"),
        "revoked": (["revoked.example.com"], ["serverAuth", "clientAuth"], "revoked.example.com"),
        "expired": (["expired.example.com"], ["clientAuth"], "expired.example.com"),
    }
    leaves: dict[str, tuple] = {}
    expectations = []
    serial = 3000
    for leaf_name, (sans, purposes, expected_host) in leaf_specs.items():
        serial += 1
        key, cert = certgen.make_leaf(
            subject_cn=sans[0].replace("*.", ""),
            san_hosts=sans,
            issuer_key=int_key,
            issuer_name=int_cert.subject,
            serial=serial,
            epoch_ms=EPOCH_MS,
            validity_ms=LEAF_VALIDITIES[leaf_name],
            purposes=purposes,
        )
        leaves[leaf_name] = (key, cert)
        _write(cert_dir / f"{leaf_name}.pem", certgen.pem_cert(cert))
        expectations.append(
            {
                "leaf": leaf_name,
                "pem": f"certs/{leaf_name}.pem",
                "hostname": expected_host,
                "purposes": purposes,
                "expected_purpose": purposes[0],
            }
        )

    _write(cert_dir / "root_ca.pem", certgen.pem_cert(root_cert))
    _write(cert_dir / "intermediate_ca.pem", certgen.pem_cert(int_cert))

    # ----- v1 trust store (single JSON, relative seconds) ------------------
    v1_entries = [
        {
            "alias": "root-ca",
            "pem": "certs/root_ca.pem",
            "relative_trust_seconds": 0,
            "enabled": True,
        },
        {
            "alias": "intermediate-ca",
            "pem": "certs/intermediate_ca.pem",
            "relative_trust_seconds": 3600,
            "enabled": True,
        },
        # Older representation of root-ca with a fractional-feeling relative
        # offset (still an integer number of seconds) to feed the ledger.
        {
            "alias": "legacy-root",
            "pem": "certs/root_ca.pem",
            "relative_trust_seconds": 86400,
            "enabled": False,
        },
        {
            # Boundary anchor: same intermediate subject as the v2 anchor with
            # a trust instant only ~200 ms away, inside the precision window.
            "alias": "intermediate-boundary",
            "pem": "certs/intermediate_ca.pem",
            "relative_trust_seconds": 3600,
            "enabled": True,
        },
    ]
    v1_payload = {"format": "v1", "logical_clock_tick": 0, "entries": []}
    for entry in v1_entries:
        cert = x509.load_pem_x509_certificate((root / entry["pem"]).read_bytes())
        v1_payload["entries"].append(
            {
                "alias": entry["alias"],
                "pem": entry["pem"],
                "fingerprint_sha256": certgen.sha256_fingerprint(cert),
                "relative_trust_seconds": entry["relative_trust_seconds"],
                "enabled": entry["enabled"],
            }
        )
    _write(v1_dir / "truststore.json", json.dumps(v1_payload, indent=2, sort_keys=True))

    # ----- v2 trust store (sharded JSON + SQLite) --------------------------
    shard_records = [
        {
            "alias": "root-ca",
            "pem": "certs/root_ca.pem",
            "effective_ms": EPOCH_MS + 500,
            "generation": 2,
            "alert_tombstone": None,
            "revoked": False,
        },
        {
            "alias": "intermediate-ca",
            "pem": "certs/intermediate_ca.pem",
            # 200 ms later than the v1 intermediate trust instant; the gap is
            # inside sensitive_tolerance_ms (500 ms), forcing a sensitive mark.
            "effective_ms": EPOCH_MS + 3_600_200,
            "generation": 2,
            "alert_tombstone": {"code": "T-100", "reason": "rotation scheduled", "tick": 3},
            "revoked": False,
        },
        {
            "alias": "revoked-leaf-anchor",
            "pem": "certs/revoked.pem",
            "effective_ms": EPOCH_MS,
            "generation": 3,
            "alert_tombstone": {"code": "T-200", "reason": "key compromise", "tick": 5},
            "revoked": True,
        },
    ]
    manifest = {"format": "v2", "logical_clock_tick": 5, "shards": []}
    db_file = v2_dir / "meta.sqlite3"
    if db_file.exists():
        db_file.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = v2_dir / ("meta.sqlite3" + suffix)
        if sidecar.exists():
            sidecar.unlink()
    db = sqlite3.connect(db_file)
    db.execute(
        """
        CREATE TABLE anchor_meta (
            alias TEXT NOT NULL,
            generation INTEGER NOT NULL,
            effective_ms INTEGER NOT NULL,
            alert_tombstone TEXT,
            revoked INTEGER NOT NULL CHECK (revoked IN (0, 1)),
            PRIMARY KEY (alias, generation)
        )
        """
    )
    for index, record in enumerate(shard_records, start=1):
        shard_name = f"shard-{index:02d}.json"
        cert = x509.load_pem_x509_certificate((root / record["pem"]).read_bytes())
        shard = {
            "alias": record["alias"],
            "pem": record["pem"],
            "fingerprint_sha256": certgen.sha256_fingerprint(cert),
            "effective_ms": record["effective_ms"],
            "generation": record["generation"],
            "alert_tombstone": record["alert_tombstone"],
            "revoked": record["revoked"],
        }
        body = json.dumps(shard, indent=2, sort_keys=True).encode("utf-8")
        _write(v2_dir / shard_name, body.decode("utf-8"))
        manifest["shards"].append(
            {
                "file": f"truststore_v2/{shard_name}",
                "sha256": _sha256_file_bytes(body),
                "generation": record["generation"],
            }
        )
        db.execute(
            "INSERT INTO anchor_meta VALUES (?, ?, ?, ?, ?)",
            (
                record["alias"],
                record["generation"],
                record["effective_ms"],
                json.dumps(record["alert_tombstone"], sort_keys=True),
                1 if record["revoked"] else 0,
            ),
        )
    db.commit()
    db.close()
    _write(v2_dir / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True))

    # ----- revocation evidence --------------------------------------------
    revoked_cert = leaves["revoked"][1]
    crl_records = [
        {
            "type": "crl",
            "issuer": "Intermediate CA",
            "revoked_serials": [revoked_cert.serial_number],
            "this_update_tick": 4,
            "next_update_tick": 7,
        }
    ]
    ocsp_records = [
        {
            "type": "ocsp",
            "serial": revoked_cert.serial_number,
            "status": "revoked",
            "this_update_tick": 4,
            "next_update_tick": 7,
            "hash_algorithm": "sha256",
        },
        {
            "type": "ocsp",
            "serial": leaves["good"][1].serial_number,
            "status": "good",
            "this_update_tick": 4,
            "next_update_tick": 7,
            "hash_algorithm": "sha256",
        },
    ]
    _write(rev_dir / "crl.json", json.dumps(crl_records, indent=2, sort_keys=True))
    _write(rev_dir / "ocsp.json", json.dumps(ocsp_records, indent=2, sort_keys=True))

    # ----- clock, precision and expectations ------------------------------
    _write(root / "clock.json", json.dumps({"epoch_ms": EPOCH_MS, "steps_ms": CLOCK_STEPS_MS}, indent=2))
    _write(root / "precision.json", json.dumps(PRECISION, indent=2, sort_keys=True))
    _write(
        root / "leaf_expectations.json",
        json.dumps({"verify_tick": VERIFY_TICK, "leaves": expectations}, indent=2, sort_keys=True),
    )

    summary = {
        "root": str(root),
        "verify_tick": VERIFY_TICK,
        "leaf_count": len(leaves),
        "v1_anchors": len(v1_entries),
        "v2_anchors": len(shard_records),
    }
    _write(root / "sample_manifest.json", json.dumps(summary, indent=2, sort_keys=True))
    return summary


if __name__ == "__main__":  # pragma: no cover
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "sample"
    print(json.dumps(generate_sample(target), indent=2, sort_keys=True))
