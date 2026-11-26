from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

from .util import write_json


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key_digest(keys: list[str]) -> str:
    return _sha256("\n".join(sorted(keys)).encode("utf-8"))


def _write_shard(path: Path, rules: list[dict[str, Any]]) -> str:
    write_json(path, {"rules": rules})
    return _sha256(path.read_bytes())


def _write_meta(
    package_dir: Path,
    *,
    version: str,
    min_generation: int,
    key_digest: str,
    shard_hashes: dict[str, str],
) -> None:
    meta_path = package_dir / "meta.db"
    if meta_path.exists():
        meta_path.unlink()
    conn = sqlite3.connect(meta_path)
    try:
        conn.executescript(
            "CREATE TABLE manifest (version TEXT, min_generation INTEGER, key_digest TEXT);"
            "CREATE TABLE shards (name TEXT, sha256 TEXT);"
        )
        conn.execute(
            "INSERT INTO manifest(version, min_generation, key_digest) VALUES(?, ?, ?)",
            (version, min_generation, key_digest),
        )
        conn.executemany(
            "INSERT INTO shards(name, sha256) VALUES(?, ?)",
            sorted(shard_hashes.items()),
        )
        conn.commit()
    finally:
        conn.close()


def _write_v2_package(
    package_dir: Path,
    *,
    version: str,
    min_generation: int,
    rules: list[dict[str, Any]],
) -> None:
    package_dir.mkdir(parents=True, exist_ok=True)
    for old in package_dir.glob("shard_*.json"):
        old.unlink()
    ordered = sorted(rules, key=lambda item: item["rule_key"])
    shards: list[list[dict[str, Any]]] = [[], []]
    for index, rule in enumerate(ordered):
        shards[index % 2].append(rule)
    shard_hashes: dict[str, str] = {}
    for index, shard_rules in enumerate(shards):
        name = f"shard_{index}.json"
        shard_hashes[name] = _write_shard(package_dir / name, shard_rules)
    _write_meta(
        package_dir,
        version=version,
        min_generation=min_generation,
        key_digest=_key_digest([rule["rule_key"] for rule in ordered]),
        shard_hashes=shard_hashes,
    )


def generate(out: str | Path, *, seed: int = 42, event_count: int = 60) -> dict[str, Any]:
    out_path = Path(out)
    out_path.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    tree = {
        "d0": None,
        "d1": "d0",
        "d2": "d0",
        "d3": "d0",
        "d4": "d1",
        "d5": "d1",
        "d6": "d2",
        "d7": "d2",
        "d8": "d4",
        "d9": "d4",
        "d10": "d6",
        "d11": "d6",
    }
    dirs: list[dict[str, Any]] = []
    for dir_id in sorted(tree, key=lambda item: int(item[1:])):
        if dir_id in {"d5", "d9"}:
            base_quota = None
            ext_quotas = {
                "alpha": rng.randint(1800, 3600),
                "beta": rng.randint(1800, 3000),
            }
        else:
            base_quota = rng.randint(2600, 7800)
            ext_quotas = {}
        dirs.append(
            {
                "base_quota": base_quota,
                "dir_id": dir_id,
                "ext_quotas": ext_quotas,
                "parent_id": tree[dir_id],
            }
        )
    write_json(out_path / "dirs.json", {"dirs": dirs})

    file_keys = ("alpha", "beta", "gamma")
    dir_ids = list(tree)
    events: list[dict[str, Any]] = []
    clock_ms = 0
    for seq in range(1, event_count + 1):
        clock_ms += rng.choice((0, 250, 500, 750, 1000))
        dir_id = rng.choice(dir_ids)
        file_key = rng.choice(file_keys)
        size = rng.randint(20, 500)
        entropy = hashlib.sha256(
            f"{seed}:{seq}:{dir_id}:{file_key}".encode("utf-8")
        ).digest()
        content = (entropy * ((size + len(entropy) - 1) // len(entropy)))[:size]
        events.append(
            {
                "bytes": size,
                "clock_ms": clock_ms,
                "content_b64": base64.b64encode(content).decode("ascii"),
                "digest": _sha256(content),
                "dir_id": dir_id,
                "file_key": file_key,
                "seq": seq,
            }
        )
    with (out_path / "writes.jsonl").open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(
                json.dumps(
                    event,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )

    quota_by_dir = {
        item["dir_id"]: item["base_quota"]
        for item in dirs
        if item["base_quota"] is not None
    }
    v1_rules = [
        {
            "effective_in_seconds": 2,
            "quota_bytes": quota_by_dir["d2"],
            "rule_key": "dir:d2",
        },
        {
            "effective_in_seconds": 5,
            "quota_bytes": 1800,
            "rule_key": "dir:d4",
        },
        {
            "effective_in_seconds": 0,
            "quota_bytes": 900,
            "rule_key": "dir:d6",
        },
    ]
    v1 = {
        "base_clock_ms": 0,
        "format": "v1",
        "manifest_version": "v1-0001",
        "rules": v1_rules,
    }
    write_json(out_path / "rules_v1.json", v1)

    v2_initial_rules = [
        {
            "effective_at_ms": 3000,
            "generation": 2,
            "quota_bytes": max(1, quota_by_dir["d2"] + 1800),
            "rule_key": "dir:d2",
        },
        {
            "effective_at_ms": 1000,
            "generation": 2,
            "quota_bytes": 900,
            "rule_key": "dir:d7",
        },
        {
            "effective_at_ms": 5000,
            "generation": 1,
            "quota_bytes": 1500,
            "rule_key": "dir:d4",
        },
        {
            "effective_at_ms": 2000,
            "generation": 2,
            "quota_bytes": 800,
            "rule_key": "dir:d10",
        },
    ]
    _write_v2_package(
        out_path / "rules_v2",
        version="v2-0001",
        min_generation=2,
        rules=v2_initial_rules,
    )

    v2_next_rules = [dict(rule) for rule in v2_initial_rules]
    for rule in v2_next_rules:
        if rule["rule_key"] == "dir:d2":
            rule["generation"] = 3
            rule["quota_bytes"] = 700
    v2_next_rules.append(
        {
            "effective_at_ms": 4000,
            "generation": 3,
            "quota_bytes": 500,
            "rule_key": "dir:d3",
        }
    )
    _write_v2_package(
        out_path / "rules_v2_next",
        version="v2-0002",
        min_generation=3,
        rules=sorted(v2_next_rules, key=lambda item: item["rule_key"]),
    )

    v2_replace_rules = [
        {
            "effective_at_ms": 3000,
            "generation": 4,
            "quota_bytes": 600,
            "rule_key": "dir:d2",
        },
        {
            "effective_at_ms": 5000,
            "generation": 2,
            "quota_bytes": 1200,
            "rule_key": "dir:d4",
        },
    ]
    _write_v2_package(
        out_path / "rules_v2_replace",
        version="v2-0003",
        min_generation=2,
        rules=v2_replace_rules,
    )

    patch = {
        "ops": [
            {
                "effective_at_ms": 0,
                "generation": 4,
                "op": "rule_change",
                "quota_bytes": 300,
                "rule_key": "dir:d6",
            },
            {"dir_id": "d5", "new_name": "d5_renamed", "op": "rename"},
            {"op": "tombstone", "rule_key": "dir:d7"},
        ],
        "patch_id": "patch-0001",
    }
    write_json(out_path / "patch.json", patch)

    stats = {
        "dirs": len(dirs),
        "events": len(events),
        "out": str(out_path),
        "seed": seed,
        "v1_rules": len(v1_rules),
        "v2_next_rules": len(v2_next_rules),
        "v2_replace_rules": len(v2_replace_rules),
        "v2_rules": len(v2_initial_rules),
    }
    write_json(out_path / "seed_manifest.json", {"generated": stats})
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic quota diagnostic fixtures."
    )
    parser.add_argument("--out", required=True, help="output fixture directory")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--events", type=int, default=60)
    args = parser.parse_args(argv)
    stats = generate(args.out, seed=args.seed, event_count=args.events)
    print(json.dumps({"generated": stats}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
