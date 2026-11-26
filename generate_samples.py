#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from chaos_budget.contracts import build_summary
from chaos_budget.contracts import input_file_hash


SEED = 20261127


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def make_event(
    experiment_id: str,
    sequence: int,
    observation_type: str,
    clock_ms: int,
    detail: Dict[str, Any],
    *,
    generation: int = 1,
) -> Dict[str, Any]:
    event = {
        "event_id": f"EVT-{experiment_id}-{sequence:02d}",
        "sequence": sequence,
        "experiment_id": experiment_id,
        "observation_type": observation_type,
        "observation_logical_clock_ms": clock_ms,
        "generation": generation,
        "detail": detail,
    }
    event["summary"] = build_summary(event)
    return event


def build_experiments(rng: np.random.Generator) -> List[Dict[str, Any]]:
    specs = [
        ("EXP-001", 10, 1000, 200, "checkout-service", "latency_injection"),
        ("EXP-002", 20, 1000, 80, "payment-service", "error_injection"),
        ("EXP-003", 30, 1000, 500, "inventory-service", "cpu_pressure"),
        ("EXP-004", 40, 1000, 150, "search-service", "network_loss"),
        ("EXP-005", 50, 1500, 100, "cart-service", "latency_injection"),
        ("EXP-006", 60, 1500, 120, "pricing-service", "dependency_timeout"),
        ("EXP-007", 70, 1500, 100, "notification-service", "pause_container"),
        ("EXP-008", 80, 1500, 80, "profile-service", "disk_io_pressure"),
        ("EXP-009", 90, 1600, 60, "gateway-service", "connection_reset"),
        ("EXP-010", 100, 1800, 70, "session-service", "cache_eviction"),
        ("EXP-011", 110, 5000, 120, "analytics-service", "memory_pressure"),
    ]
    experiments: List[Dict[str, Any]] = []
    for experiment_id, priority, start, duration, service, fault_type in specs:
        jitter_ms = int(rng.integers(5, 80))
        error_rate_bps = int(rng.integers(50, 500))
        experiments.append(
            {
                "experiment_id": experiment_id,
                "target_service": service,
                "fault_type": fault_type,
                "fault_params": {
                    "deterministic_jitter_ms": jitter_ms,
                    "error_rate_bps": error_rate_bps,
                    "numpy_seed": SEED,
                },
                "start_logical_clock_ms": start,
                "expected_duration_ms": duration,
                "priority": priority,
                "round_id": "default",
            }
        )
    return experiments


def build_observations(rng: np.random.Generator) -> List[Dict[str, Any]]:
    metric = lambda low, high: int(rng.integers(low, high))
    return [
        make_event("EXP-001", 1, "blocked", 1100, {"queue_depth": metric(20, 80), "reason": "latency barrier"}),
        make_event("EXP-001", 2, "recovered", 1200, {"drain_ms": metric(10, 60)}, generation=1),
        make_event("EXP-001", 3, "completed", 1300, {"requests": metric(100, 500), "result": "stable"}),
        make_event("EXP-002", 1, "failed", 1250, {"error_code": "PAYMENT_TIMEOUT", "affected_pct": metric(5, 30)}),
        make_event(
            "EXP-002",
            2,
            "recovered",
            1400,
            {"failover": "deterministic-replica", "target_service": "payment-service"},
            generation=1,
        ),
        make_event("EXP-002", 3, "completed", 1750, {"second_attempt": True, "result": "stable"}, generation=2),
        make_event("EXP-005", 1, "completed", 1580, {"cache_hit_bps": metric(8000, 9900)}),
        make_event("EXP-006", 1, "blocked", 1540, {"open_connections": metric(50, 200)}),
        make_event("EXP-006", 2, "failed", 1590, {"error_code": "PRICING_UPSTREAM_TIMEOUT"}),
        make_event("EXP-007", 1, "blocked", 1600, {"paused_ms": 0, "reason": "manual chaos barrier"}),
        make_event("EXP-009", 1, "failed", 1620, {"error_code": "GATEWAY_RESET"}),
        make_event(
            "EXP-009",
            2,
            "recovered",
            1650,
            {"connection_pool": "rewarmed", "target_service": "gateway-service"},
            generation=1,
        ),
    ]


def build_policy_v1() -> Dict[str, Any]:
    return {
        "snapshot_id": "policy-v1-single-json",
        "format": "v1_single_json",
        "generation": 1,
        "declared_logical_clock_ms": 1000,
        "effective_relative_seconds": "0.2509",
        "rounding": "ceil",
        "policies": {
            "phase_charge_points": {
                "value": 5,
                "declared_logical_clock_ms": 1000,
                "generation": 1,
            },
            "recovery_restore_bps": {
                "value": 4000,
                "declared_logical_clock_ms": 1300,
                "generation": 2,
            },
            "legacy_summary_format": {
                "value": "canonical-v1",
                "declared_logical_clock_ms": 900,
                "generation": 1,
            },
        },
    }


def build_policy_v2(directory: Path) -> Dict[str, Any]:
    shard_00 = {
        "format": "v2_json_shard",
        "shard_id": 0,
        "policies": {
            "phase_charge_points": {
                "value": 3,
                "declared_logical_clock_ms": 1100,
                "generation": 2,
            },
            "recovery_restore_bps": {
                "value": 6000,
                "declared_logical_clock_ms": 1300,
                "generation": 2,
            },
        },
    }
    shard_01 = {
        "format": "v2_json_shard",
        "shard_id": 1,
        "policies": {
            "recovery_fee_points": {
                "value": 2,
                "declared_logical_clock_ms": 1300,
                "generation": 2,
            },
            "scheduling_stop_on_constraint": {
                "value": True,
                "declared_logical_clock_ms": 1100,
                "generation": 2,
            },
        },
    }
    write_json(directory / "policy_v2_shard_00.json", shard_00)
    write_json(directory / "policy_v2_shard_01.json", shard_01)

    metadata_path = directory / "policy_v2_metadata.sqlite3"
    try:
        metadata_path.unlink()
    except FileNotFoundError:
        pass

    conn = sqlite3.connect(metadata_path)
    try:
        conn.execute(
            """
            CREATE TABLE snapshot (
                snapshot_id TEXT PRIMARY KEY,
                format TEXT NOT NULL,
                generation INTEGER NOT NULL,
                declared_logical_clock_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE shards (
                shard_file TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                key_count INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE policy_meta (
                policy_key TEXT PRIMARY KEY,
                shard_file TEXT NOT NULL,
                declared_logical_clock_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO snapshot VALUES (?, ?, ?, ?)",
            ("policy-v2-sharded-sqlite", "v2_shard_sqlite", 2, 1100),
        )
        for shard_name, payload in (
            ("policy_v2_shard_00.json", shard_00),
            ("policy_v2_shard_01.json", shard_01),
        ):
            checksum = input_file_hash(directory / shard_name)
            conn.execute(
                "INSERT INTO shards VALUES (?, ?, ?)",
                (shard_name, checksum, len(payload["policies"])),
            )
            for key, entry in payload["policies"].items():
                conn.execute(
                    "INSERT INTO policy_meta VALUES (?, ?, ?, ?)",
                    (key, shard_name, entry["declared_logical_clock_ms"], entry["generation"]),
                )
        conn.commit()
    finally:
        conn.close()
    return {"metadata_path": metadata_path}


def generate(output_dir: str | Path = "sample_data") -> Dict[str, Any]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(SEED)

    experiments = build_experiments(rng)
    observations = build_observations(rng)
    budget_config = {
        "budget_config_id": "controlled-chaos-local-budget",
        "per_experiment_budget": 1000,
        "round_logical_time_budget_ms": 700,
        "global_concurrency_limit": 3,
        "backoff_base_ms": 100,
        "backoff_cap_ms": 2000,
        "phase_charge_points": 3,
        "admission_charge_points": 0,
        "cost_per_ms": 1,
        "lock_timeout_seconds": 10,
    }

    write_json(directory / "experiments.json", {"experiments": experiments})
    write_json(directory / "budget_config.json", {"budget_config": budget_config})
    write_json(directory / "observations.json", {"observations": observations})
    write_json(directory / "policy_v1.json", build_policy_v1())
    build_policy_v2(directory)

    manifest = {
        "seed": SEED,
        "deterministic": True,
        "uses_numpy": np.__version__,
        "injection_clock_ms": 1000,
        "sample_count": len(experiments),
        "observation_count": len(observations),
        "workflow": [
            {"command": "load", "clock_ms": 1000, "reset": True},
            {"command": "run", "clock_ms": 1000},
            {"command": "observe", "clock_ms": 1400},
            {"command": "run", "clock_ms": 1500},
            {"command": "observe", "clock_ms": 1595},
            {"command": "run", "clock_ms": 1600},
            {"command": "observe", "clock_ms": 1650},
            {"command": "resume", "experiment_id": "EXP-002", "clock_ms": 1700},
            {"command": "observe", "clock_ms": 1800},
            {"command": "run", "clock_ms": 1800},
            {"command": "verify"},
        ],
        "expected_outcome_categories": {
            "within_budget_not_started": ["EXP-011"],
            "failed_after_start": ["EXP-006"],
            "voluntarily_abandoned": ["EXP-003", "EXP-004", "EXP-008"],
        },
    }
    write_json(directory / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic controlled-chaos sample data")
    parser.add_argument("--output-dir", default="sample_data")
    args = parser.parse_args()
    manifest = generate(args.output_dir)
    print(json.dumps({"ok": True, "generated": args.output_dir, "manifest": manifest}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
