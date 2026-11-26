from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .hashing import sha256_digest, sha256_hex, sha256_object


# This fixed instant is aligned to the seeded 60_000 ms validation windows.
EPOCH_MS = 1_699_999_980_000
DEFAULT_PORT = 18_080
OFFLINE_PORT = 18_099


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def content_bodies() -> Dict[str, str]:
    return {
        "origin-auth": "origin-auth|deterministic-body|v1\n",
        "api-auth": "api-auth|signed-session|deterministic\n",
        "origin-catalog": "origin-catalog|skus=42|epoch=fixed\n",
        "catalog-list": "catalog-list|items=a,b,c|digest=fixed\n",
        "catalog-page": "catalog-page|title=Deterministic CDN\n",
        "shared-config": "shared-config|ttl=60|stale=false\n",
    }


def digest_for(bodies: Dict[str, str], name: str) -> str:
    return sha256_digest(bodies[name].encode("utf-8"))


def build_v1(bodies: Dict[str, str]) -> Dict[str, Any]:
    rules = [
        {
            "name": "origin-auth",
            "generation": 1,
            "logical_clock_ms": EPOCH_MS - 100_000,
            "origin_verify_type": "origin_digest",
            "relative_effective_seconds": 0,
            "content_digest": digest_for(bodies, "origin-auth"),
        },
        {
            "name": "origin-catalog",
            "generation": 1,
            "logical_clock_ms": EPOCH_MS - 100_000,
            "origin_verify_type": "origin_digest",
            "relative_effective_seconds": 0,
            "content_digest": digest_for(bodies, "origin-catalog"),
        },
        {
            "name": "api-auth",
            "generation": 2,
            "logical_clock_ms": EPOCH_MS - 50_000,
            "origin_verify_type": "origin_digest",
            "relative_effective_seconds": 30,
            "content_digest": digest_for(bodies, "api-auth"),
        },
        {
            "name": "catalog-list",
            "generation": 2,
            "logical_clock_ms": EPOCH_MS - 50_000,
            "origin_verify_type": "origin_digest",
            "relative_effective_seconds": 45,
            "content_digest": digest_for(bodies, "catalog-list"),
        },
        {
            "name": "catalog-page",
            "generation": 3,
            "logical_clock_ms": EPOCH_MS - 20_000,
            "origin_verify_type": "origin_digest",
            "relative_effective_seconds": 60,
            "content_digest": digest_for(bodies, "catalog-page"),
        },
        {
            "name": "shared-config",
            "generation": 3,
            "logical_clock_ms": EPOCH_MS - 10_000,
            "origin_verify_type": "content_digest",
            "relative_effective_seconds": 120,
            "content_digest": digest_for(bodies, "shared-config"),
        },
    ]
    return {
        "format": "cdn-rules-v1",
        "logical_clock_ms": EPOCH_MS - 100_000,
        "rules": rules,
        "forward_dependencies": {
            "api-auth": "origin-auth",
            "catalog-list": "origin-catalog",
            "catalog-page": "catalog-list",
        },
    }


def v2_rules(bodies: Dict[str, str]) -> List[Dict[str, Any]]:
    return [
        {
            "canonical_name": "legacy-banner",
            "cache_generation": 1,
            "logical_clock_ms": EPOCH_MS - 200_000,
            "effective_at_ms": EPOCH_MS - 199_000,
            "upstream_rules": [],
            "tombstone": True,
            "content_digest": None,
        },
        {
            "canonical_name": "shared-config",
            "cache_generation": 3,
            "logical_clock_ms": EPOCH_MS - 10_000,
            "effective_at_ms": EPOCH_MS - 9_000,
            "upstream_rules": [],
            "tombstone": False,
            "verify_type": "content_digest",
            "content_digest": digest_for(bodies, "shared-config"),
        },
        {
            "canonical_name": "catalog-list",
            "cache_generation": 3,
            "logical_clock_ms": EPOCH_MS - 20_000,
            "effective_at_ms": EPOCH_MS - 18_000,
            "upstream_rules": ["origin-catalog"],
            "tombstone": False,
            "verify_type": "origin_digest",
            "content_digest": digest_for(bodies, "catalog-list"),
        },
        {
            "canonical_name": "catalog-page",
            "cache_generation": 4,
            "logical_clock_ms": EPOCH_MS - 20_000,
            "effective_at_ms": EPOCH_MS - 17_000,
            "upstream_rules": ["catalog-list"],
            "tombstone": False,
            "verify_type": "origin_digest",
            "content_digest": digest_for(bodies, "catalog-page"),
        },
        {
            "canonical_name": "edge-personalization",
            "cache_generation": 4,
            "logical_clock_ms": EPOCH_MS - 15_000,
            "effective_at_ms": EPOCH_MS - 14_000,
            "upstream_rules": ["shared-config"],
            "tombstone": False,
            "verify_type": "status",
            "content_digest": None,
        },
    ]


def write_v2_package(
    package_dir: Path,
    rules: List[Dict[str, Any]],
    *,
    package_version: int,
    min_generation: int,
    declared_clock_ms: int,
    tombstones: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    shard_dir = package_dir / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    shard_groups = {
        "shard-00-core.json": rules[:3],
        "shard-01-edge.json": rules[3:],
    }
    shard_hashes: Dict[str, str] = {}
    for filename, shard_rules in shard_groups.items():
        path = shard_dir / filename
        write_json(path, {"shard": filename, "rules": shard_rules})
        shard_hashes[filename] = sha256_hex(path.read_bytes())

    ruleset_digest = sha256_object(
        [
            {
                "name": rule.get("canonical_name", rule.get("name")),
                "generation": rule.get("cache_generation", rule.get("generation")),
                "effective_at_ms": rule["effective_at_ms"],
                "dependencies": rule.get("upstream_rules", rule.get("dependencies", [])),
                "tombstone": rule.get("tombstone", False),
                "content_digest": rule.get("content_digest"),
            }
            for rule in rules
        ]
    )
    db_path = package_dir / "metadata.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE package_manifest(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE shard_hashes(shard_path TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
        CREATE TABLE tombstones(
            rule_name TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            logical_clock_ms INTEGER NOT NULL
        );
        """
    )
    manifest_rows = {
        "package_version": str(package_version),
        "min_generation": str(min_generation),
        "declared_clock_ms": str(declared_clock_ms),
        "ruleset_digest": ruleset_digest,
    }
    conn.executemany("INSERT INTO package_manifest(key, value) VALUES(?, ?)", manifest_rows.items())
    conn.executemany("INSERT INTO shard_hashes(shard_path, sha256) VALUES(?, ?)", shard_hashes.items())
    tombstone_rows = tombstones or [
        {"rule_name": rule["canonical_name"], "reason": "seed tombstone", "logical_clock_ms": EPOCH_MS - 200_000}
        for rule in rules
        if rule.get("tombstone")
    ]
    conn.executemany(
        "INSERT INTO tombstones(rule_name, reason, logical_clock_ms) VALUES(:rule_name, :reason, :logical_clock_ms)",
        tombstone_rows,
    )
    conn.commit()
    conn.close()

    package_manifest = {
        "package_version": package_version,
        "min_generation": min_generation,
        "declared_clock_ms": declared_clock_ms,
        "ruleset_digest": ruleset_digest,
        "update_mode": "full_replace",
    }
    write_json(package_dir / "package-manifest.json", package_manifest)
    return package_manifest


def build_observations(bodies: Dict[str, str]) -> List[Dict[str, Any]]:
    wrong_digest = sha256_digest(b"catalog-page|stale-tampered-body\n")
    return [
        {
            "seq": 1,
            "clock_ms": EPOCH_MS + 5_000,
            "observation_id": "obs-000001",
            "node": "edge-a",
            "rule": "catalog-page",
            "result": "hit",
            "content_digest": digest_for(bodies, "catalog-page"),
        },
        {
            "seq": 2,
            "clock_ms": EPOCH_MS + 10_000,
            "observation_id": "obs-000002",
            "node": "edge-a",
            "rule": "catalog-list",
            "result": "hit",
            "content_digest": digest_for(bodies, "catalog-list"),
        },
        {
            "seq": 3,
            "clock_ms": EPOCH_MS + 15_000,
            "observation_id": "obs-000003",
            "node": "edge-b",
            "rule": "catalog-page",
            "result": "hit",
            "content_digest": wrong_digest,
        },
        {
            "seq": 4,
            "clock_ms": EPOCH_MS + 20_000,
            "observation_id": "obs-000004",
            "node": "edge-b",
            "rule": "api-auth",
            "result": "hit",
            "content_digest": digest_for(bodies, "api-auth"),
        },
        {
            "seq": 5,
            "clock_ms": EPOCH_MS + 25_000,
            "observation_id": "obs-000005",
            "node": "edge-offline",
            "rule": "origin-auth",
            "result": "hit",
            "content_digest": digest_for(bodies, "origin-auth"),
        },
        {
            "seq": 6,
            "clock_ms": EPOCH_MS + 30_000,
            "observation_id": "obs-000006",
            "node": "edge-a",
            "rule": "shared-config",
            "result": "hit",
            "content_digest": digest_for(bodies, "shared-config"),
        },
        {
            "seq": 7,
            "clock_ms": EPOCH_MS + 70_000,
            "observation_id": "obs-000007",
            "node": "edge-a",
            "rule": "catalog-page",
            "result": "hit",
            "content_digest": digest_for(bodies, "catalog-page"),
        },
    ]


def generate_sample(out: Path, *, port: int = DEFAULT_PORT, offline_port: int = OFFLINE_PORT) -> Dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    bodies = content_bodies()
    write_json(out / "origin-content.json", {"content": bodies})
    write_json(out / "v1" / "rules.json", build_v1(bodies))
    write_v2_package(out / "v2", v2_rules(bodies), package_version=1, min_generation=1, declared_clock_ms=EPOCH_MS)

    nodes = {
        "schema": "cdn-edge-nodes/v1",
        "defaults": {"window_size_ms": 60_000, "window_offset_ms": 0},
        "nodes": [
            {
                "name": "edge-a",
                "origin_base_url": f"http://127.0.0.1:{port}",
                "window_size_ms": 60_000,
                "window_offset_ms": 0,
                "probe_timeout_ms": 1_000,
            },
            {
                "name": "edge-b",
                "origin_base_url": f"http://127.0.0.1:{port}",
                "window_size_ms": 60_000,
                "window_offset_ms": 0,
                "probe_timeout_ms": 1_000,
            },
            {
                "name": "edge-offline",
                "origin_base_url": f"http://127.0.0.1:{offline_port}",
                "window_size_ms": 60_000,
                "window_offset_ms": 0,
                "probe_timeout_ms": 300,
            },
        ],
    }
    write_json(out / "nodes" / "edge-nodes.json", nodes)
    write_json(out / "observations" / "probe-stream.json", {"observations": build_observations(bodies)})

    patch = {
        "patch_id": "patch-origin-catalog-generation-bump",
        "submitted_clock_ms": EPOCH_MS + 125_000,
        "changes": [
            {
                "op": "upsert",
                "name": "origin-catalog",
                "rule": {
                    "generation": 2,
                    "logical_clock_ms": EPOCH_MS + 125_000,
                    "effective_at_ms": EPOCH_MS + 125_000,
                    "verify_type": "origin_digest",
                    "dependencies": [],
                    "content_digest": digest_for(bodies, "origin-catalog"),
                    "change_reason": "deterministic seed patch: bump invalidated origin generation",
                },
            }
        ],
    }
    write_json(out / "patches" / "patch-origin-catalog.json", patch)

    package_v3_rules = v2_rules(bodies) + [
        {
            "canonical_name": "image-optimizer",
            "cache_generation": 5,
            "logical_clock_ms": EPOCH_MS + 140_000,
            "effective_at_ms": EPOCH_MS + 140_000,
            "upstream_rules": ["origin-catalog"],
            "tombstone": False,
            "verify_type": "status",
            "content_digest": None,
        }
    ]
    # Bump one existing rule generation so v3 is a genuine full replacement.
    for rule in package_v3_rules:
        if rule["canonical_name"] == "catalog-page":
            rule["cache_generation"] = 5
        if rule["canonical_name"] == "edge-personalization":
            rule["cache_generation"] = 5
    write_v2_package(
        out / "packages" / "rules-package-3",
        package_v3_rules,
        package_version=3,
        min_generation=2,
        declared_clock_ms=EPOCH_MS + 140_000,
    )

    write_json(
        out / "sample-manifest.json",
        {
            "sample_id": "cdn-deterministic-demo",
            "seed": "cdn_engine.seed",
            "manifest_version": 1,
            "min_generation": 1,
            "epoch_ms": EPOCH_MS,
            "window_horizon_ms": EPOCH_MS,
            "merge_clock_ms": EPOCH_MS,
            "first_window_clock_ms": EPOCH_MS + 60_000,
            "final_clock_ms": EPOCH_MS + 120_000,
            "patch_clock_ms": EPOCH_MS + 125_000,
            "package_clock_ms": EPOCH_MS + 140_000,
            "default_clock_ms": EPOCH_MS + 120_000,
            "origin_port": port,
            "offline_port": offline_port,
        },
    )
    return {"out": str(out), "epoch_ms": EPOCH_MS, "port": port, "offline_port": offline_port}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic CDN rule-engine samples")
    parser.add_argument("--out", default="samples/demo")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--offline-port", type=int, default=OFFLINE_PORT)
    args = parser.parse_args(argv)
    result = generate_sample(Path(args.out), port=args.port, offline_port=args.offline_port)
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
