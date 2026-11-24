"""Deterministic fixed-seed sample workspace generator."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

from .codes import (
    SCHEMA_CLOCK,
    SCHEMA_DEPENDENCIES,
    SCHEMA_REVOCATIONS,
    SCHEMA_V1,
    SCHEMA_V2_MANIFEST,
    SCHEMA_V2_SHARD,
)
from .io_utils import atomic_write_json

FIXED_SEED = "uid2d1b8ac8-fixed-seed-20261125"
CLOCK_ID = "fixed-observation-clock-v1"
OBSERVED_AT_MS = 1767225600000
ORIGIN_MS = 1767139200000


def _safe_key(key: str) -> str:
    return key.replace("@", "__at__").replace("/", "_")


def _payload(key: str, generation: int) -> bytes:
    return (
        f"seed={FIXED_SEED}\nartifact={key}\ngeneration={generation}\n"
        "deterministic local supply-chain payload\n"
    ).encode("utf-8")


def _materialize_content(root: Path, key: str, generation: int) -> tuple[str, str]:
    relative = f"contents/{_safe_key(key)}-g{generation}.bin"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_payload(key, generation))
    digest = hashlib.sha256(_payload(key, generation)).hexdigest()
    return digest, relative


def _specs() -> list[dict[str, Any]]:
    return [
        {"key": "foundation/core@1.4.0", "relative": 120, "signature": "signed", "upper_v1": True},
        {"key": "platform/codec@2.1.0", "relative": 240, "signature": "signed"},
        {"key": "security/crypto@3.0.1", "relative": 360, "signature": "signed", "v2_generation": 2, "digest_changes": True},
        {"key": "infra/log@1.8.2", "relative": 480, "signature": "signed", "time_conflict": True},
        {"key": "net/tls@4.2.0", "relative": 600, "signature": "signed", "algorithm_conflict": True, "omit_content": True},
        {"key": "service/api@5.0.0", "relative": 720, "signature": "signed"},
        {"key": "app/portal@6.1.0", "relative": 840, "signature": "signed"},
        {"key": "cli/auditctl@2.4.0", "relative": 960, "signature": "signed"},
        {"key": "legacy/token@0.9.3", "relative": 1080, "signature": "invalid_signature", "tombstone": True},
        {"key": "service/gateway@1.2.0", "relative": 1200, "signature": "unsigned"},
        {"key": "batch/worker@1.0.0", "relative": 1320, "signature": "signed"},
    ]


def _dependency_edges() -> list[dict[str, str]]:
    rows = [
        ("platform/codec@2.1.0", "foundation/core@1.4.0", "^1.0.0"),
        ("security/crypto@3.0.1", "foundation/core@1.4.0", "^1.0.0"),
        ("infra/log@1.8.2", "foundation/core@1.4.0", "^1.0.0"),
        ("net/tls@4.2.0", "security/crypto@3.0.1", "^3.0.0"),
        ("net/tls@4.2.0", "platform/codec@2.1.0", "^2.0.0"),
        ("service/api@5.0.0", "net/tls@4.2.0", "^4.0.0"),
        ("service/api@5.0.0", "infra/log@1.8.2", "^1.0.0"),
        ("app/portal@6.1.0", "service/api@5.0.0", "^5.0.0"),
        ("app/portal@6.1.0", "net/tls@4.2.0", "^4.0.0"),
        ("cli/auditctl@2.4.0", "service/api@5.0.0", "^5.0.0"),
        ("service/gateway@1.2.0", "service/api@5.0.0", "^5.0.0"),
        ("service/gateway@1.2.0", "legacy/token@0.9.3", "^0.9.0"),
        ("batch/worker@1.0.0", "service/gateway@1.2.0", "^1.0.0"),
    ]
    return [
        {"dependent_key": dependent, "dependency_key": dependency, "version_constraint": constraint}
        for dependent, dependency, constraint in rows
    ]


def generate_seed_workspace(root: str | Path) -> dict[str, str]:
    target = Path(root)
    target.mkdir(parents=True, exist_ok=True)
    (target / "batches").mkdir(exist_ok=True)

    clock = {
        "schema": SCHEMA_CLOCK,
        "clock_id": CLOCK_ID,
        "fixed_seed": FIXED_SEED,
        "observed_at_ms": OBSERVED_AT_MS,
        "relative_second_origin_ms": ORIGIN_MS,
        "source": "fixed local declaration; never use system clock for conversion",
    }
    clock_path = target / "observation_clock.json"
    atomic_write_json(clock_path, clock)

    specs = _specs()
    v1_artifacts = []
    v2_artifacts = []
    metadata_rows = []
    digests_by_key: dict[str, dict[int, str]] = {}

    for spec in specs:
        key = spec["key"]
        name, version = key.rsplit("@", 1)
        gen1_digest, content_path = _materialize_content(target, key, 1)
        digests_by_key[key] = {1: gen1_digest}
        v1_digest = gen1_digest.upper() if spec.get("upper_v1") else gen1_digest
        algorithm_v1 = "sha256"
        v1_record = {
            "name": name,
            "version": version,
            "digest": v1_digest,
            "algorithm": algorithm_v1,
            "signature_status": spec["signature"],
            "published_relative_seconds": spec["relative"],
            "digest_generation": 1,
        }
        if not spec.get("omit_content"):
            v1_record["content_path"] = content_path
        v1_artifacts.append(v1_record)

        v2_generation = int(spec.get("v2_generation", 1))
        if v2_generation == 2:
            v2_digest, v2_content_path = _materialize_content(target, key, 2)
            digests_by_key[key][2] = v2_digest
        else:
            v2_digest = gen1_digest
            v2_content_path = content_path

        algorithm_v2 = "hmac-sha256" if spec.get("algorithm_conflict") else "sha256"
        published_v2 = ORIGIN_MS + spec["relative"] * 1000 + 500
        if spec.get("time_conflict"):
            published_v2 += 5000
        v2_record = {
            "artifact_key": key,
            "published_at_ms": published_v2,
            "digest_generation": v2_generation,
            "algorithm": algorithm_v2,
            "digest": v2_digest,
            "signature_status": spec["signature"],
            "tombstoned": bool(spec.get("tombstone", False)),
        }
        if not spec.get("omit_content"):
            v2_record["content_path"] = v2_content_path
        v2_artifacts.append(v2_record)
        metadata_rows.append(
            (
                key,
                published_v2,
                v2_generation,
                algorithm_v2,
                1 if spec.get("tombstone") else 0,
            )
        )

    v1_artifacts.extend(
        [
            {
                "name": "broken/hashwidth",
                "version": "0.1.0",
                "digest": "abcd",
                "algorithm": "sha256",
                "signature_status": "signed",
                "published_relative_seconds": 1440,
                "digest_generation": 1,
            },
            {
                "name": "broken/signature",
                "version": "0.2.0",
                "digest": "0" * 64,
                "algorithm": "sha256",
                "signature_status": "maybe",
                "published_relative_seconds": 1500,
                "digest_generation": 1,
            },
            {
                "name": "dup/key",
                "version": "1.0.0",
                "digest": "1" * 64,
                "algorithm": "sha256",
                "signature_status": "unsigned",
                "published_relative_seconds": 1560,
                "digest_generation": 1,
            },
            {
                "name": "dup/key",
                "version": "1.0.0",
                "digest": "2" * 64,
                "algorithm": "sha256",
                "signature_status": "unsigned",
                "published_relative_seconds": 1561,
                "digest_generation": 1,
            },
        ]
    )

    v1_payload = {
        "schema": SCHEMA_V1,
        "snapshot_id": "seed-v1-single-file",
        "fixed_seed": FIXED_SEED,
        "artifacts": v1_artifacts,
    }
    v1_path = target / "v1_snapshot.json"
    atomic_write_json(v1_path, v1_payload)

    shard_size = 6
    shard_paths = []
    for shard_index, start in enumerate(range(0, len(v2_artifacts), shard_size)):
        shard_name = f"v2_shard_{shard_index:03d}.json"
        shard_payload = {
            "schema": SCHEMA_V2_SHARD,
            "shard_id": f"seed-v2-shard-{shard_index:03d}",
            "artifacts": v2_artifacts[start : start + shard_size],
        }
        atomic_write_json(target / shard_name, shard_payload)
        shard_paths.append(shard_name)

    db_path = target / "metadata.sqlite3"
    temporary_db = target / "metadata.sqlite3.tmp"
    temporary_db.unlink(missing_ok=True)
    with sqlite3.connect(temporary_db) as connection:
        connection.execute(
            """
            CREATE TABLE artifact_metadata (
                artifact_key TEXT PRIMARY KEY,
                published_at_ms INTEGER NOT NULL,
                digest_generation INTEGER NOT NULL,
                algorithm TEXT NOT NULL,
                tombstoned INTEGER NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO artifact_metadata VALUES (?, ?, ?, ?, ?)", metadata_rows
        )
        connection.commit()
    os.replace(temporary_db, db_path)

    manifest = {
        "schema": SCHEMA_V2_MANIFEST,
        "snapshot_id": "seed-v2-sharded-sqlite",
        "fixed_seed": FIXED_SEED,
        "shards": shard_paths,
        "metadata_db": db_path.name,
    }
    manifest_path = target / "v2_manifest.json"
    atomic_write_json(manifest_path, manifest)

    dependencies = {
        "schema": SCHEMA_DEPENDENCIES,
        "snapshot_id": "seed-dependencies",
        "edges": _dependency_edges(),
    }
    dependencies_path = target / "dependencies.json"
    atomic_write_json(dependencies_path, dependencies)

    cycle_dependencies = {
        "schema": SCHEMA_DEPENDENCIES,
        "snapshot_id": "seed-cycle-dependencies",
        "edges": [
            {"dependent_key": "cycle/a@1.0.0", "dependency_key": "cycle/b@1.0.0", "version_constraint": "^1.0.0"},
            {"dependent_key": "cycle/b@1.0.0", "dependency_key": "cycle/c@1.0.0", "version_constraint": "^1.0.0"},
            {"dependent_key": "cycle/c@1.0.0", "dependency_key": "cycle/a@1.0.0", "version_constraint": "^1.0.0"},
            {"dependent_key": "self/loop@1.0.0", "dependency_key": "self/loop@1.0.0", "version_constraint": "=1.0.0"},
        ],
    }
    cycle_path = target / "dependencies.cycle.json"
    atomic_write_json(cycle_path, cycle_dependencies)

    codec_digest_v2, codec_content = _materialize_content(target, "platform/codec@2.1.0", 2)
    update_batch = {
        "schema": SCHEMA_V1,
        "batch_id": "batch-codec-digest-g2",
        "snapshot_id": "batch-codec-digest-g2",
        "artifacts": [
            {
                "name": "platform/codec",
                "version": "2.1.0",
                "digest": codec_digest_v2,
                "algorithm": "sha256",
                "signature_status": "signed",
                "published_relative_seconds": 240,
                "digest_generation": 2,
                "content_path": codec_content,
            }
        ],
    }
    update_path = target / "batches" / "update_codec_digest.json"
    atomic_write_json(update_path, update_batch)

    revocation_batch = {
        "schema": SCHEMA_REVOCATIONS,
        "batch_id": "batch-revoke-log",
        "fixed_seed": FIXED_SEED,
        "revocations": [
            {
                "artifact_key": "infra/log@1.8.2",
                "reason": "deterministic-seed-key-compromise",
                "revoked_at_ms": ORIGIN_MS + 2_000_000,
            }
        ],
    }
    revocation_path = target / "batches" / "revoke_log.json"
    atomic_write_json(revocation_path, revocation_batch)

    corrupt_path = target / "batches" / "corrupt_snapshot.json"
    corrupt_path.write_text('{"snapshot_id": "corrupt", "artifacts": [', encoding="utf-8")

    return {
        "root": str(target),
        "clock": str(clock_path),
        "v1": str(v1_path),
        "v2_manifest": str(manifest_path),
        "dependencies": str(dependencies_path),
        "cycle_dependencies": str(cycle_path),
        "update_batch": str(update_path),
        "revocation_batch": str(revocation_path),
        "corrupt_batch": str(corrupt_path),
        "sqlite_metadata": str(db_path),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic seed samples")
    parser.add_argument("output", nargs="?", default="examples/seed_workspace")
    args = parser.parse_args()
    paths = generate_seed_workspace(args.output)
    for key, value in paths.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
