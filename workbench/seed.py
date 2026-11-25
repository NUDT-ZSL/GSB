from __future__ import annotations

import argparse
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

from .canonical import canonical_dumps, observation_digest, sha256_bytes


SEED = 20261126
BASE_CLOCK_MS = 1_760_000_000_000


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _observation(instance_id: str, seq: int, status: int, rng: random.Random) -> dict[str, Any]:
    record = {
        "instance_id": instance_id,
        "seq": seq,
        "logical_clock_ms": BASE_CLOCK_MS + 60_000 + seq * 1_000 + rng.randint(0, 250),
        "status_code": status,
        "latency_ms": rng.randint(2, 240),
    }
    record["digest"] = observation_digest(record)
    return record


def generate_sample(out_dir: str | Path, seed: int = SEED) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "shards").mkdir(exist_ok=True)
    rng = random.Random(seed)

    registry_instances = [
        {
            "service_name": "svc-alpha",
            "instance_id": "inst-alpha",
            "endpoint": "http://127.0.0.1:18081/health",
            "expected_status_codes": [200],
            "lease_generation": 11,
            "state_generation": 101,
        },
        {
            "service_name": "svc-beta",
            "instance_id": "inst-beta",
            "endpoint": "http://127.0.0.1:18082/health",
            "expected_status_codes": [200, 204],
            "lease_generation": 8,
            "state_generation": 102,
        },
        {
            "service_name": "svc-gamma",
            "instance_id": "inst-gamma",
            "endpoint": "http://127.0.0.1:18083/health",
            "expected_status_codes": [200],
            "lease_generation": 5,
            "state_generation": 103,
        },
        {
            "service_name": "svc-delta",
            "instance_id": "inst-delta",
            "endpoint": "http://127.0.0.1:18084/health",
            "expected_status_codes": [200],
            "lease_generation": 3,
            "state_generation": 104,
        },
        {
            "service_name": "svc-epsilon",
            "instance_id": "inst-epsilon",
            "endpoint": "http://127.0.0.1:18085/health",
            "expected_status_codes": [200],
            "lease_generation": 2,
            "state_generation": 105,
        },
    ]
    registry = {
        "schema": "service-registry/v1",
        "seed": seed,
        "logical_clock_ms": BASE_CLOCK_MS,
        "instances": registry_instances,
    }
    _write_json(out / "registry.json", registry)

    patterns = {
        "inst-alpha": [200, 200, 500, 503, 500, 200, 200, 200],
        "inst-beta": [200, 500, 500, 204, 200, 200],
        "inst-gamma": [200, 200, 404, 200, 200],
        "inst-delta": [500, 500, 500, 200],
        "inst-epsilon": [200, 200, 200, 200],
    }
    observations: list[dict[str, Any]] = []
    for instance_id, statuses in patterns.items():
        for seq, status in enumerate(statuses, start=1):
            observations.append(_observation(instance_id, seq, status, rng))
    observations.sort(key=lambda item: (item["seq"], item["instance_id"]))
    observations_path = out / "observations.jsonl"
    observations_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in observations), encoding="utf-8")

    v1_rules = {
        "schema": "health-rules-v1",
        "format": "v1",
        "snapshot_id": "rules-v1-seed",
        "declared_clock_ms": BASE_CLOCK_MS,
        "effective_after_seconds": 30,
        "rules": {
            "rule-alpha": {
                "generation": 1,
                "declared_clock_ms": BASE_CLOCK_MS,
                "suspect_threshold": 1,
                "lost_threshold": 4,
                "upstream_rules": [],
            },
            "rule-beta": {
                "generation": 2,
                "declared_clock_ms": BASE_CLOCK_MS + 20_000,
                "suspect_threshold": 1,
                "lost_threshold": 2,
                "upstream_rules": ["rule-alpha"],
            },
            "rule-gamma": {
                "generation": 4,
                "declared_clock_ms": BASE_CLOCK_MS + 9_000,
                "suspect_threshold": 1,
                "lost_threshold": 3,
                "upstream_rules": ["rule-beta"],
            },
            "rule-delta": {
                "generation": 1,
                "declared_clock_ms": BASE_CLOCK_MS,
                "suspect_threshold": 1,
                "lost_threshold": 2,
                "upstream_rules": [],
            },
            "rule-epsilon": {
                "generation": 1,
                "declared_clock_ms": BASE_CLOCK_MS + 1_000,
                "suspect_threshold": 1,
                "lost_threshold": 3,
                "upstream_rules": [],
            },
        },
        "instance_rules": {
            "inst-alpha": "rule-alpha",
            "inst-beta": "rule-beta",
            "inst-gamma": "rule-gamma",
            "inst-delta": "rule-delta",
            "inst-epsilon": "rule-epsilon",
        },
    }
    _write_json(out / "rules_v1.json", v1_rules)

    v2_rule_bodies = [
        {
            "rule_key": "rule-alpha",
            "generation": 2,
            "declared_clock_ms": BASE_CLOCK_MS + 10_000,
            "effective_at_ms": BASE_CLOCK_MS + 20_000,
            "tombstone": False,
            "upstream_rules": [],
            "instance_ids": ["inst-alpha"],
            "suspect_threshold": 1,
            "lost_threshold": 3,
        },
        {
            "rule_key": "rule-beta",
            "generation": 2,
            "declared_clock_ms": BASE_CLOCK_MS + 20_000,
            "effective_at_ms": BASE_CLOCK_MS + 25_000,
            "tombstone": False,
            "upstream_rules": ["rule-alpha"],
            "instance_ids": ["inst-beta"],
            "suspect_threshold": 1,
            "lost_threshold": 3,
        },
        {
            "rule_key": "rule-gamma",
            "generation": 3,
            "declared_clock_ms": BASE_CLOCK_MS + 8_000,
            "effective_at_ms": BASE_CLOCK_MS + 15_000,
            "tombstone": False,
            "upstream_rules": ["rule-beta"],
            "instance_ids": ["inst-gamma"],
            "suspect_threshold": 1,
            "lost_threshold": 2,
        },
        {
            "rule_key": "rule-delta",
            "generation": 3,
            "declared_clock_ms": BASE_CLOCK_MS + 30_000,
            "effective_at_ms": BASE_CLOCK_MS + 12_000,
            "tombstone": True,
            "tombstone_reason": "seed-v2-tombstone",
            "upstream_rules": [],
            "instance_ids": ["inst-delta"],
        },
        {
            "rule_key": "rule-epsilon",
            "generation": 2,
            "declared_clock_ms": BASE_CLOCK_MS + 12_000,
            "effective_at_ms": BASE_CLOCK_MS + 18_000,
            "tombstone": False,
            "upstream_rules": [],
            "instance_ids": ["inst-epsilon"],
            "suspect_threshold": 1,
            "lost_threshold": 3,
        },
    ]
    shard_bodies = [
        ("rules_v2_shard_00.json", v2_rule_bodies[:3]),
        ("rules_v2_shard_01.json", v2_rule_bodies[3:]),
    ]
    shard_manifest_entries = []
    for filename, rules in shard_bodies:
        shard_path = out / filename
        payload = {"schema": "health-rules-v2-shard/v1", "shard_id": filename.removesuffix(".json"), "rules": rules}
        _write_json(shard_path, payload)
        digest = sha256_bytes(shard_path.read_bytes())
        shard_manifest_entries.append({"shard_id": payload["shard_id"], "path": filename, "sha256": digest})

    v2_manifest = {
        "schema": "health-rules-v2-manifest/v1",
        "metadata_db": "rules_v2.db",
        "shards": shard_manifest_entries,
    }
    _write_json(out / "rules_v2_manifest.json", v2_manifest)

    db_path = out / "rules_v2.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE shards(shard_id TEXT PRIMARY KEY, path TEXT NOT NULL, content_sha256 TEXT NOT NULL);
        CREATE TABLE rule_meta(
          rule_key TEXT PRIMARY KEY,
          effective_at_ms INTEGER NOT NULL,
          generation INTEGER NOT NULL,
          declared_clock_ms INTEGER NOT NULL,
          upstream_rules_json TEXT NOT NULL,
          tombstone INTEGER NOT NULL,
          tombstone_reason TEXT
        );
        CREATE TABLE rule_generation_history(
          rule_key TEXT NOT NULL,
          generation INTEGER NOT NULL,
          changed_at_ms INTEGER NOT NULL,
          ordinal INTEGER NOT NULL
        );
        """
    )
    for entry in shard_manifest_entries:
        conn.execute("INSERT INTO shards VALUES(?,?,?)", (entry["shard_id"], entry["path"], entry["sha256"]))
    for rule in v2_rule_bodies:
        conn.execute(
            "INSERT INTO rule_meta VALUES(?,?,?,?,?,?,?)",
            (
                rule["rule_key"],
                rule["effective_at_ms"],
                rule["generation"],
                rule["declared_clock_ms"],
                canonical_dumps(rule["upstream_rules"]),
                1 if rule["tombstone"] else 0,
                rule.get("tombstone_reason"),
            ),
        )
        generations = list(range(1, rule["generation"] + 1))
        for ordinal, generation in enumerate(generations):
            conn.execute(
                "INSERT INTO rule_generation_history VALUES(?,?,?,?)",
                (rule["rule_key"], generation, rule["effective_at_ms"] - (len(generations) - ordinal) * 1000, ordinal),
            )
    conn.commit()
    conn.close()

    bands = [(1, 2), (3, 4), (5, 99)]
    obs_shard_manifest = []
    for shard_index, (low, high) in enumerate(bands, start=1):
        shard_id = f"obs-shard-{shard_index:03d}"
        filename = f"shards/{shard_id}.jsonl"
        selected = [item for item in observations if low <= item["seq"] <= high]
        selected.sort(key=lambda item: (item["seq"], item["instance_id"]))
        shard_path = out / filename
        shard_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in selected), encoding="utf-8")
        coverage = []
        for instance_id in sorted({item["instance_id"] for item in selected}):
            seqs = [item["seq"] for item in selected if item["instance_id"] == instance_id]
            coverage.append({"instance_id": instance_id, "start_seq": min(seqs), "end_seq": max(seqs)})
        obs_shard_manifest.append(
            {
                "shard_id": shard_id,
                "path": filename,
                "content_digest": sha256_bytes(shard_path.read_bytes()),
                "record_count": len(selected),
                "coverage": coverage,
            }
        )
    _write_json(out / "observation_shards_manifest.json", {"schema": "observation-shard-manifest/v1", "shards": obs_shard_manifest})

    patches_dir = out / "patches"
    _write_json(
        patches_dir / "patch_alpha_tombstone.json",
        {
            "patch_id": "patch-alpha-tombstone",
            "reason": "Force root rule invalidation and dependency closure",
            "changes": [
                {
                    "op": "tombstone",
                    "rule_key": "rule-alpha",
                    "reason": "seed patch: upstream authentication probe invalid",
                    "effective_at_ms": BASE_CLOCK_MS + 120_000,
                }
            ],
        },
    )
    _write_json(
        patches_dir / "patch_beta_effective.json",
        {
            "patch_id": "patch-beta-effective-window",
            "changes": [
                {
                    "op": "update_effective_at",
                    "rule_key": "rule-beta",
                    "effective_at_ms": BASE_CLOCK_MS + 63_000,
                }
            ],
        },
    )
    _write_json(
        patches_dir / "patch_epsilon_dependency.json",
        {
            "patch_id": "patch-epsilon-depends-delta",
            "changes": [
                {
                    "op": "replace_dependencies",
                    "rule_key": "rule-epsilon",
                    "upstream_rules": ["rule-delta"],
                }
            ],
        },
    )

    summary = {
        "seed": seed,
        "base_clock_ms": BASE_CLOCK_MS,
        "injection_clock_ms": BASE_CLOCK_MS,
        "registry": "registry.json",
        "observations": "observations.jsonl",
        "observation_shard_manifest": "observation_shards_manifest.json",
        "v1_rules": "rules_v1.json",
        "v2_manifest": "rules_v2_manifest.json",
        "patches": [
            "patches/patch_alpha_tombstone.json",
            "patches/patch_beta_effective.json",
            "patches/patch_epsilon_dependency.json",
        ],
    }
    _write_json(out / "sample_manifest.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic diagnostic workbench sample")
    parser.add_argument("--out", default="samples", help="output directory")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    summary = generate_sample(args.out, args.seed)
    print(json.dumps({"ok": True, "sample": summary}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
