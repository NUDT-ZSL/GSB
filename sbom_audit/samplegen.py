"""Deterministic local sample generator.

The fixed seed is recorded in truth.json.  No system clock or random source is
used for publication times or digests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import canonical_key
from .storage import atomic_write_json


SEED = 20261125
OBSERVATION_CLOCK_MS = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp() * 1000)
SYNC_KEY = hashlib.sha256(b"sbom-audit-fixed-sync-key-v1").digest()


def _sender_digest(suffix: str, variant: str = "base") -> str:
    return hashlib.sha256(f"sender|seed={SEED}|{suffix}|{variant}".encode("utf-8")).hexdigest()


def _components() -> list[dict[str, Any]]:
    core_d1 = _sender_digest("lib-core")
    core_d2 = _sender_digest("lib-core", "rotated-2")
    net_d1 = _sender_digest("lib-net")
    rows = [
        {"suffix": "acme-app", "v1_version": "4.2.0", "v2_version": "4.2.0", "sync": "synced", "day": 120},
        {"suffix": "svc-order", "v1_version": "2.1.0", "v2_version": "2.1.0", "sync": "synced", "day": 90},
        {"suffix": "svc-billing", "v1_version": "1.8.3", "v2_version": "1.8.3", "sync": "synced", "day": 80},
        {"suffix": "lib-core", "v1_version": "1.5.0", "v2_version": "1.6.0", "d1": core_d1, "d2": core_d2, "sync": "digest-rotated", "day": 70},
        {"suffix": "lib-net", "v1_version": "2.3.1", "v2_version": "2.3.1", "algo2": "hmac-sha256", "d2": hmac.new(SYNC_KEY, bytes.fromhex(net_d1), hashlib.sha256).hexdigest(), "sync": "hmac-derived", "day": 60},
        {"suffix": "lib-util", "v1_version": "1.4.2", "v2_version": "2.0.0", "sync": "version-drift", "day": 50},
        {"suffix": "lib-tls", "v1_version": "1.8.0", "v2_version": "1.8.0", "sync": "synced", "day": 40},
        {"suffix": "lib-crypto", "v1_version": "3.2.1", "v2_version": "3.2.1", "revoked2": True, "sync": "revoked", "day": 30},
        {"suffix": "lib-legacy", "v1_version": "0.9.4", "v2_version": "0.9.4", "tombstone2": True, "sync": "tombstoned", "day": 20},
    ]
    for row in rows:
        row["key"] = canonical_key(row["suffix"])
        row.setdefault("d1", _sender_digest(row["suffix"]))
        row.setdefault("d2", row["d1"])
        row.setdefault("algo1", "sha256")
        row.setdefault("algo2", "sha256")
        row.setdefault("revoked2", False)
        row.setdefault("tombstone2", False)
    return rows


def _edges() -> list[dict[str, str]]:
    def k(name: str) -> str:
        return canonical_key(name)

    return [
        {"from": k("acme-app"), "to": k("svc-order"), "constraint": "^2.0.0"},
        {"from": k("acme-app"), "to": k("svc-billing"), "constraint": "~1.8.0"},
        {"from": k("svc-order"), "to": k("lib-core"), "constraint": ">=1.5.0,<2.0.0"},
        {"from": k("svc-order"), "to": k("lib-net"), "constraint": "^2.0.0"},
        {"from": k("svc-order"), "to": k("lib-util"), "constraint": "^1.4.0"},
        {"from": k("svc-billing"), "to": k("lib-core"), "constraint": ">=1.5.0"},
        {"from": k("svc-billing"), "to": k("lib-crypto"), "constraint": "^3.0.0"},
        {"from": k("svc-billing"), "to": k("lib-legacy"), "constraint": "*"},
        {"from": k("lib-net"), "to": k("lib-tls"), "constraint": ">=1.1.0,<2.0.0"},
        {"from": k("lib-tls"), "to": k("lib-crypto"), "constraint": "^3.0.0"},
    ]


def generate_sample(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    (root / "v2").mkdir(parents=True, exist_ok=True)
    (root / "incremental").mkdir(parents=True, exist_ok=True)
    (root / "invalid").mkdir(parents=True, exist_ok=True)
    components = _components()
    edges = _edges()

    v1_records = []
    for index, comp in enumerate(components, start=1):
        v1_records.append({
            "id": comp["suffix"],
            "ecosystem": "generic",
            "version": comp["v1_version"],
            "algorithm": comp["algo1"],
            "digest": comp["d1"],
            "signature_status": "signed",
            "published_relative_s": -comp["day"] * 86400,
        })
    v1 = {
        "schema": "sbom-audit.v1",
        "snapshot_id": "v1-baseline",
        "generation": 1,
        "ecosystem": "generic",
        "observation_clock_ms": OBSERVATION_CLOCK_MS,
        "records": v1_records,
    }
    atomic_write_json(root / "v1_snapshot.json", v1)

    v2_records = []
    metadata_rows = []
    for comp in components:
        published = OBSERVATION_CLOCK_MS - comp["day"] * 86400_000 + 3_600_000
        record = {
            "canonical_key": comp["key"],
            "component_id": comp["suffix"],
            "version": comp["v2_version"],
            "algorithm": None if comp["tombstone2"] else comp["algo2"],
            "digest": None if comp["tombstone2"] else comp["d2"],
            "signature_status": "signed",
            "published_at_ms": published,
            "tombstone": comp["tombstone2"],
            "revoked": comp["revoked2"],
        }
        v2_records.append(record)
        metadata_rows.append((comp["key"], "v2-current", 2, published, int(comp["tombstone2"]), int(comp["revoked2"]), None, "shard-000" if len(metadata_rows) < 5 else "shard-001"))

    shard0 = {"schema": "sbom-audit.v2.shard", "snapshot_id": "v2-current", "generation": 2, "shard_id": "shard-000", "records": v2_records[:5]}
    shard1 = {"schema": "sbom-audit.v2.shard", "snapshot_id": "v2-current", "generation": 2, "shard_id": "shard-001", "records": v2_records[5:]}
    atomic_write_json(root / "v2" / "v2_shard_000.json", shard0)
    atomic_write_json(root / "v2" / "v2_shard_001.json", shard1)

    db_path = root / "v2" / "metadata.sqlite3"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE snapshot_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE component_metadata(
            canonical_key TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            published_at_ms INTEGER NOT NULL,
            tombstone INTEGER NOT NULL DEFAULT 0,
            revoked INTEGER NOT NULL DEFAULT 0,
            revoked_at_ms INTEGER,
            shard TEXT NOT NULL
        );
        """
    )
    conn.executemany("INSERT INTO snapshot_meta(key,value) VALUES(?,?)", [
        ("snapshot_id", "v2-current"),
        ("generation", "2"),
        ("observation_clock_ms", str(OBSERVATION_CLOCK_MS)),
    ])
    conn.executemany("INSERT INTO component_metadata VALUES(?,?,?,?,?,?,?,?)", metadata_rows)
    conn.commit()
    conn.close()

    deps = {"schema": "sbom-audit.dependencies.v1", "declaration_id": "deps-baseline", "edges": edges}
    atomic_write_json(root / "dependencies.json", deps)

    sync_markers = {
        "schema": "sbom-audit.sync.v1",
        "markers": [
            {
                "canonical_key": canonical_key("lib-net"),
                "marker": "hmac-over-digest",
                "bits": 256,
                "key_id": "fixed-sample-key",
                "key_hex": SYNC_KEY.hex(),
            }
        ],
    }
    atomic_write_json(root / "sync_markers.json", sync_markers)

    expected_closure = sorted({
        canonical_key(name)
        for name in [
            "lib-core", "lib-crypto", "lib-legacy",
            "svc-order", "svc-billing", "lib-net", "lib-tls", "acme-app",
        ]
    })
    truth = {
        "schema": "sbom-audit.truth.v1",
        "seed": SEED,
        "observation_clock_ms": OBSERVATION_CLOCK_MS,
        "sync_key_hex": SYNC_KEY.hex(),
        "sender_digests": {comp["key"]: comp["d1"] for comp in components},
        "components": [
            {
                "canonical_key": comp["key"],
                "sender_original_digest": comp["d1"],
                "sync_marker": comp["sync"],
                "expected_v1_version": comp["v1_version"],
                "expected_v2_version": comp["v2_version"],
                "expected_revoked": comp["revoked2"],
                "expected_tombstoned": comp["tombstone2"],
            }
            for comp in components
        ],
        "expected_seeds": sorted([canonical_key("lib-core"), canonical_key("lib-crypto"), canonical_key("lib-legacy")]),
        "expected_closure": expected_closure,
        "expected_reverse_dependents": sorted(set(expected_closure) - {canonical_key("lib-core"), canonical_key("lib-crypto"), canonical_key("lib-legacy")}),
        "expected_version_violations": [
            {"from": canonical_key("svc-order"), "to": canonical_key("lib-util"), "constraint": "^1.4.0", "actual_version": "2.0.0"}
        ],
        "expected_conflict_keys": sorted([canonical_key("lib-core"), canonical_key("lib-net"), canonical_key("lib-util")]),
    }
    atomic_write_json(root / "truth.json", truth)

    util = next(comp for comp in components if comp["suffix"] == "lib-util")
    revoke_batch = {
        "batch_id": "batch-revoke-lib-util",
        "records": [{
            "canonical_key": util["key"],
            "component_id": "lib-util",
            "version": "2.0.0",
            "algorithm": "sha256",
            "digest": util["d2"],
            "signature_status": "signed",
            "published_at_ms": OBSERVATION_CLOCK_MS - 1_000,
            "generation": 3,
            "snapshot_kind": "incremental",
            "snapshot_id": "revocation-batch-1",
            "source_ref": "incremental/revoke_lib_util.json",
            "record_index": 1,
            "tombstone": False,
            "revoked": True,
        }],
    }
    atomic_write_json(root / "incremental" / "revoke_lib_util.json", revoke_batch)
    budget_probe = {
        "batch_id": "budget-probe",
        "work_items": [
            {"canonical_key": canonical_key("budget-probe"), "bytes": 16 * 1024 * 1024, "algorithm": "sha256", "seed": 37}
        ],
    }
    atomic_write_json(root / "incremental" / "budget_probe.json", budget_probe)

    invalid_v1 = {
        "schema": "sbom-audit.v1",
        "snapshot_id": "v1-invalid-sample",
        "observation_clock_ms": OBSERVATION_CLOCK_MS,
        "records": [
            {"id": "ok-lib", "version": "1.0.0", "algorithm": "sha256", "digest": _sender_digest("ok-lib"), "signature_status": "signed", "published_relative_s": -100},
            {"id": "", "version": "1.0.0", "algorithm": "sha256", "digest": "a" * 64, "signature_status": "signed", "published_relative_s": -100},
            {"id": "bad-version", "version": "1.x", "algorithm": "sha256", "digest": "a" * 64, "signature_status": "signed", "published_relative_s": -100},
            {"id": "bad-digest", "version": "1.0.0", "algorithm": "sha256", "digest": "short", "signature_status": "signed", "published_relative_s": -100},
            {"id": "bad-signature", "version": "1.0.0", "algorithm": "sha256", "digest": "a" * 64, "signature_status": "maybe", "published_relative_s": -100},
            {"id": "bad-time", "version": "1.0.0", "algorithm": "sha256", "digest": "a" * 64, "signature_status": "signed", "published_relative_s": "soon"},
            {"id": "ok-two", "version": "2.0.0", "algorithm": "hmac-sha256", "digest": "b" * 64, "signature_status": "unsigned", "published_relative_s": -10},
            {"id": "ok-lib", "version": "1.0.1", "algorithm": "sha256", "digest": "c" * 64, "signature_status": "signed", "published_relative_s": -9},
        ],
    }
    atomic_write_json(root / "invalid" / "v1_invalid_records.json", invalid_v1)
    cycles = {
        "declaration_id": "deps-cycle-sample",
        "edges": [
            {"from": canonical_key("cycle-a"), "to": canonical_key("cycle-b"), "constraint": "*"},
            {"from": canonical_key("cycle-b"), "to": canonical_key("cycle-c"), "constraint": "*"},
            {"from": canonical_key("cycle-c"), "to": canonical_key("cycle-a"), "constraint": "*"},
            {"from": canonical_key("self-loop"), "to": canonical_key("self-loop"), "constraint": "*"},
        ],
    }
    atomic_write_json(root / "invalid" / "dependency_cycles.json", cycles)
    truth["invalid_expected_codes"] = ["E_INVALID_ID", "E_VERSION_SYNTAX", "E_HASH_ALGO", "E_SIGNATURE_STATUS", "E_TIMESTAMP", "E_DUPLICATE_KEY"]
    truth["incremental_expected_closure"] = sorted(set(expected_closure) | {canonical_key("lib-util")})
    atomic_write_json(root / "truth.json", truth)
    return truth


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic SBOM audit sample")
    parser.add_argument("--root", default="samples/generated")
    args = parser.parse_args()
    truth = generate_sample(args.root)
    print(json.dumps({"generated": args.root, "components": len(truth["components"]), "closure": len(truth["expected_closure"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
