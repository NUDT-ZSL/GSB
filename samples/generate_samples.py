#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lbworkbench.util import canonical_json, sha256_file, sha256_json  # noqa: E402


BASE_CLOCK = 1_764_000_000_000
PACKAGE_VERSION = "2026.11.1"
VARIANT_VERSION = "2026.11.2"
PRIORITY = 2


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def v1_instance(
    instance_id: str,
    relative_seconds: int,
    failures: int,
    status: str,
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "relative_failure_seconds": relative_seconds,
        "consecutive_failures": failures,
        "status": status,
    }


def v2_instance(
    canonical_id: str,
    original_id: str,
    failure_ms: int,
    generation: int,
    failures: int,
    successes: int,
    weight: int,
    tombstone: bool,
    status: str,
) -> dict[str, Any]:
    return {
        "canonical_instance_id": canonical_id,
        "original_instance_id": original_id,
        "absolute_failure_ms": failure_ms,
        "state_generation": generation,
        "consecutive_failures": failures,
        "consecutive_successes": successes,
        "effective_weight": weight,
        "tombstone": tombstone,
        "status": status,
    }


def baseline_v1() -> dict[str, Any]:
    return {
        "schema": "lb-v1-snapshot/v1",
        "snapshot_id": "v1-edge-a",
        "logical_clock_ms": BASE_CLOCK,
        "state_generation": 10,
        "failure_threshold": 3,
        "default_effective_weight": 100,
        "source_priority": PRIORITY,
        "instances": [
            v1_instance("API-01", 0, 0, "healthy"),
            v1_instance("API-02", 45, 2, "healthy"),
            v1_instance("CACHE-01", 30, 0, "healthy"),
            v1_instance("DB-01", 0, 0, "healthy"),
            v1_instance("MANUAL-01", 0, 0, "healthy"),
            v1_instance("TIE-01", 0, 0, "healthy"),
            v1_instance("Worker-01", 0, 0, "healthy"),
        ],
    }


def baseline_v2_shards() -> list[dict[str, Any]]:
    return [
        {
            "shard_id": "v2-shard-000",
            "generation_min": 10,
            "generation_max": 12,
            "instances": [
                v2_instance(
                    "manual-01", "MANUAL-01", BASE_CLOCK + 60_000,
                    10, 4, 0, 100, False, "unhealthy",
                ),
                v2_instance(
                    "tie-01", "TIE-01", BASE_CLOCK + 60_000,
                    10, 0, 0, 100, False, "healthy",
                ),
                v2_instance(
                    "api-02", "API-02", BASE_CLOCK + 30_000,
                    11, 3, 0, 100, False, "unhealthy",
                ),
                v2_instance(
                    "api-01", "API-01", BASE_CLOCK + 60_000,
                    12, 0, 2, 100, False, "healthy",
                ),
            ],
        },
        {
            "shard_id": "v2-shard-001",
            "generation_min": 13,
            "generation_max": 15,
            "instances": [
                v2_instance(
                    "worker-01", "Worker-01", BASE_CLOCK - 1_000,
                    13, 3, 0, 0, True, "unhealthy",
                ),
                v2_instance(
                    "db-01", "DB-01", BASE_CLOCK + 60_000,
                    14, 0, 3, 100, False, "healthy",
                ),
                v2_instance(
                    "edge-01", "EDGE-01", BASE_CLOCK + 60_000,
                    15, 0, 4, 100, False, "healthy",
                ),
            ],
        },
    ]


def write_meta(
    path: Path,
    *,
    snapshot_id: str,
    package_version: str,
    clock: int,
    floor: int,
    shards: list[dict[str, Any]],
    v2_ids: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(
            """
            CREATE TABLE snapshot_meta (
                snapshot_id TEXT NOT NULL,
                package_version TEXT NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                state_generation_floor INTEGER NOT NULL,
                shard_count INTEGER NOT NULL,
                instance_set_sha256 TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE shard_meta (
                shard_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                generation_min INTEGER NOT NULL,
                generation_max INTEGER NOT NULL,
                record_count INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO snapshot_meta VALUES(?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                package_version,
                clock,
                floor,
                len(shards),
                sha256_json(sorted(v2_ids)),
            ),
        )
        for shard in sorted(shards, key=lambda item: item["shard_id"]):
            conn.execute(
                "INSERT INTO shard_meta VALUES(?, ?, ?, ?, ?, ?)",
                (
                    shard["shard_id"],
                    shard["path"],
                    shard["sha256"],
                    shard["generation_min"],
                    shard["generation_max"],
                    shard["record_count"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def build_v2(
    root: Path,
    *,
    package_version: str,
    clock: int,
    floor: int,
    shards: list[dict[str, Any]],
) -> dict[str, Any]:
    shard_specs = []
    v2_ids: list[str] = []
    for shard in shards:
        path = root / "shards" / f"{shard['shard_id'].replace('v2-', '')}.json"
        shard_doc = {
            "schema": "lb-v2-shard/v1",
            "shard_id": shard["shard_id"],
            "generation_min": shard["generation_min"],
            "generation_max": shard["generation_max"],
            "instances": shard["instances"],
        }
        write_json(path, shard_doc)
        relative = f"shards/{path.name}"
        shard_specs.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "generation_min": shard["generation_min"],
                "generation_max": shard["generation_max"],
                "record_count": len(shard["instances"]),
            }
        )
        v2_ids.extend(item["canonical_instance_id"] for item in shard["instances"])
    manifest = {
        "schema": "lb-v2-snapshot/v1",
        "snapshot_id": "v2-edge-a",
        "logical_clock_ms": clock,
        "state_generation_floor": floor,
        "failure_threshold": 2,
        "success_threshold": 2,
        "source_priority": PRIORITY,
        "shards": shard_specs,
    }
    write_json(root / "manifest.json", manifest)
    meta_path = root / "meta.sqlite"
    meta_shards = []
    for spec, shard in zip(shard_specs, shards):
        meta_shards.append({**shard, **spec})
    write_meta(
        meta_path,
        snapshot_id=manifest["snapshot_id"],
        package_version=package_version,
        clock=clock,
        floor=floor,
        shards=meta_shards,
        v2_ids=v2_ids,
    )
    return {
        "manifest": f"{root.name}/manifest.json",
        "manifest_sha256": sha256_file(root / "manifest.json"),
        "meta_db": f"{root.name}/meta.sqlite",
        "meta_sha256": sha256_file(meta_path),
        "shards": shard_specs,
        "ids": sorted(v2_ids),
    }


def build_package(
    samples: Path,
    *,
    package_version: str,
    clock: int,
    floor: int,
    v1: dict[str, Any],
    v1_relative: str,
    v2: dict[str, Any],
) -> Path:
    union_ids = sorted(set(item["instance_id"].lower() for item in v1["instances"]) | set(v2["ids"]))
    package = {
        "schema": "lb-workbench-package/v1",
        "package_version": package_version,
        "logical_clock_ms": clock,
        "state_generation_floor": floor,
        "v1": {
            "path": v1_relative,
            "sha256": sha256_file(samples / v1_relative),
        },
        "v2": {
            "manifest": v2["manifest"],
            "manifest_sha256": v2["manifest_sha256"],
            "meta_db": v2["meta_db"],
            "meta_sha256": v2["meta_sha256"],
        },
        "instance_set_sha256": sha256_json(union_ids),
    }
    path = samples / f"package-{package_version}.json"
    write_json(path, package)
    return path


def check_plan() -> dict[str, Any]:
    def probe(instance_id: str, target: str) -> dict[str, Any]:
        return {
            "instance_id": instance_id,
            "target": target,
            "expected_status": 200,
            "require_json": True,
        }

    rounds = [
        {
            "round_index": 1,
            "logical_clock_ms": BASE_CLOCK + 1_000,
            "probes": [
                probe("api-01", "/ok"),
                probe("api-02", "/ok"),
                probe("cache-01", "/fail"),
                probe("db-01", "/fail"),
                probe("edge-01", "/ok"),
                probe("tie-01", "/ok"),
                probe("worker-01", "/ok"),
            ],
        },
        {
            "round_index": 2,
            "logical_clock_ms": BASE_CLOCK + 2_000,
            "probes": [
                probe("api-01", "/fail"),
                probe("api-02", "/ok"),
                probe("cache-01", "/slow"),
                probe("db-01", "/malformed"),
                probe("edge-01", "/fail"),
                probe("tie-01", "/ok"),
                probe("worker-01", "/reset"),
            ],
        },
        {
            "round_index": 3,
            "logical_clock_ms": BASE_CLOCK + 3_000,
            "probes": [
                probe("api-01", "/ok"),
                probe("api-02", "/ok"),
                probe("cache-01", "/fail"),
                probe("db-01", "/ok"),
                probe("edge-01", "/ok"),
                probe("tie-01", "/fail"),
                probe("worker-01", "/ok"),
            ],
        },
    ]
    return {
        "schema": "lb-check-plan/v1",
        "plan_id": "rollout-2026-11",
        "interval_ms": 1_000,
        "timeout_ms": 200,
        "window_size": 3,
        "failure_threshold": 2,
        "success_threshold": 2,
        "rounds": rounds,
    }


def drift_injections() -> dict[str, Any]:
    return {
        "schema": "lb-drift-injections/v1",
        "description": "Expected first-trigger attributions for the deterministic probe plan.",
        "expected": {
            "api-01": {
                "cause": "real_state_change",
                "first_round_index": 2,
                "probe_error_code": "E_PROBE_STATUS_MISMATCH",
            },
            "api-02": {
                "cause": "generation_conflict",
                "first_round_index": 1,
                "probe_error_code": None,
            },
            "cache-01": {
                "cause": "time_conversion_error",
                "first_round_index": 1,
                "probe_error_code": "E_PROBE_STATUS_MISMATCH",
            },
            "db-01": {
                "cause": "real_state_change",
                "first_round_index": 1,
                "probe_error_code": "E_PROBE_STATUS_MISMATCH",
            },
            "edge-01": {
                "cause": "real_state_change",
                "first_round_index": 2,
                "probe_error_code": "E_PROBE_STATUS_MISMATCH",
            },
            "tie-01": {
                "cause": "real_state_change",
                "first_round_index": 3,
                "probe_error_code": "E_PROBE_STATUS_MISMATCH",
            },
            "worker-01": {
                "cause": "stale_tombstone",
                "first_round_index": 1,
                "probe_error_code": None,
            },
        },
        "manual_review": ["manual-01"],
        "probe_error_codes": [
            "E_PROBE_MALFORMED",
            "E_PROBE_REFUSED",
            "E_PROBE_STATUS_MISMATCH",
            "E_PROBE_TIMEOUT",
        ],
    }


def generate(samples: Path) -> None:
    samples.mkdir(parents=True, exist_ok=True)
    v1 = baseline_v1()
    write_json(samples / "v1" / "snapshot.json", v1)
    v2 = build_v2(
        samples / "v2",
        package_version=PACKAGE_VERSION,
        clock=BASE_CLOCK,
        floor=10,
        shards=baseline_v2_shards(),
    )
    build_package(
        samples,
        package_version=PACKAGE_VERSION,
        clock=BASE_CLOCK,
        floor=10,
        v1=v1,
        v1_relative="v1/snapshot.json",
        v2=v2,
    )

    variant_shards = [
        {
            "shard_id": "v2-shard-100",
            "generation_min": 16,
            "generation_max": 20,
            "instances": [
                v2_instance(
                    "api-01", "API-01", BASE_CLOCK + 65_000,
                    16, 0, 4, 100, False, "healthy",
                ),
                v2_instance(
                    "api-02", "API-02", BASE_CLOCK + 65_000,
                    17, 0, 2, 100, False, "healthy",
                ),
                v2_instance(
                    "cache-01", "CACHE-01", BASE_CLOCK + 65_000,
                    18, 0, 2, 100, False, "healthy",
                ),
                v2_instance(
                    "db-01", "DB-01", BASE_CLOCK + 65_000,
                    19, 0, 5, 100, False, "healthy",
                ),
                v2_instance(
                    "edge-02", "EDGE-02", BASE_CLOCK + 65_000,
                    20, 0, 1, 80, False, "healthy",
                ),
            ],
        }
    ]
    variant = build_v2(
        samples / "variants" / VARIANT_VERSION / "v2",
        package_version=VARIANT_VERSION,
        clock=BASE_CLOCK + 5_000,
        floor=16,
        shards=variant_shards,
    )
    variant_manifest = json.loads(
        (samples / "variants" / VARIANT_VERSION / "v2" / "manifest.json").read_text()
    )
    variant_manifest["snapshot_id"] = "v2-edge-b"
    write_json(samples / "variants" / VARIANT_VERSION / "v2" / "manifest.json", variant_manifest)
    # Rebuild metadata after changing the snapshot ID so both sources stay consistent.
    v2_root = samples / "variants" / VARIANT_VERSION / "v2"
    shard_specs = variant_manifest["shards"]
    variant["manifest_sha256"] = sha256_file(v2_root / "manifest.json")
    write_meta(
        v2_root / "meta.sqlite",
        snapshot_id="v2-edge-b",
        package_version=VARIANT_VERSION,
        clock=BASE_CLOCK + 5_000,
        floor=16,
        shards=[
            {
                "shard_id": "v2-shard-100",
                "path": "shards/shard-100.json",
                "sha256": shard_specs[0]["sha256"],
                "generation_min": 16,
                "generation_max": 20,
                "record_count": 5,
            }
        ],
        v2_ids=[item["canonical_instance_id"] for item in variant_shards[0]["instances"]],
    )
    variant["meta_sha256"] = sha256_file(v2_root / "meta.sqlite")
    variant["manifest"] = f"variants/{VARIANT_VERSION}/v2/manifest.json"
    variant["meta_db"] = f"variants/{VARIANT_VERSION}/v2/meta.sqlite"
    # The v2 manifest lives deeper than the package root.
    variant["manifest_sha256"] = sha256_file(v2_root / "manifest.json")
    build_package(
        samples,
        package_version=VARIANT_VERSION,
        clock=BASE_CLOCK + 5_000,
        floor=16,
        v1=v1,
        v1_relative="v1/snapshot.json",
        v2=variant,
    )

    write_json(samples / "check_plan.json", check_plan())
    write_json(samples / "drift_injections.json", drift_injections())
    write_json(
        samples / "manifest.json",
        {
            "schema": "lb-samples/v1",
            "package": f"package-{PACKAGE_VERSION}.json",
            "replacement_package": f"package-{VARIANT_VERSION}.json",
            "check_plan": "check_plan.json",
            "drift_injections": "drift_injections.json",
            "base_logical_clock_ms": BASE_CLOCK,
            "generated_by": "generate_samples.py",
            "digest": sha256_json(
                {
                    "v1": v1,
                    "v2": baseline_v2_shards(),
                    "plan": check_plan(),
                    "drift": drift_injections(),
                }
            ),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent),
        help="sample output directory",
    )
    args = parser.parse_args()
    generate(Path(args.out).resolve())


if __name__ == "__main__":
    main()
