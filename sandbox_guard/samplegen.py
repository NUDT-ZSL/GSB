"""Deterministic fixed-seed sample policy and patch generator."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json
from .capabilities import (
    CAPABILITY_BITS,
    CAPABILITY_WHITELIST_VERSION,
    MERGE_POLICY_VERSION,
    CHECKPOINT_FORMAT_VERSION,
    METHOD_SYNC_MARKER,
)


OBSERVATION_CLOCK_MS = 1_760_000_000_000
DEFAULT_EVALUATION_CLOCK_MS = OBSERVATION_CLOCK_MS + 10_000


def cap_mask(*names: str) -> int:
    return sum(CAPABILITY_BITS[name] for name in names)


def build_sample(seed: int = 361) -> dict[str, Any]:
    rng = random.Random(seed)
    clock = OBSERVATION_CLOCK_MS

    profiles = [
        {
            "id": "p-edge",
            "executable": r"C:\guard\bin\edge-net.exe",
            "args_template": ["--profile", "edge", "--request-id={request_id}"],
            "required_capabilities": ["file_read", "network_outbound"],
            "capability_mask": cap_mask("file_read", "network_outbound"),
            "spawns": [],
        },
        {
            "id": "p-middle",
            "executable": r"C:\guard\bin\middle-worker.exe",
            "args_template": ["--shard={shard}", "--seed", str(seed)],
            "required_capabilities": ["file_read", "file_write"],
            "capability_mask": cap_mask("file_read", "file_write"),
            "spawns": ["p-edge"],
        },
        {
            "id": "p-root",
            "executable": r"C:\guard\bin\root-supervisor.exe",
            "args_template": ["--config", r"C:\guard\etc\root.json"],
            "required_capabilities": ["file_read", "process_spawn"],
            "capability_mask": cap_mask("file_read", "process_spawn"),
            "spawns": ["p-middle"],
        },
        {
            "id": "p-writer",
            "executable": r"C:\guard\bin\safe-writer.exe",
            "args_template": ["--output={output_path}"],
            "required_capabilities": ["file_read", "file_write", "symlink_follow"],
            "capability_mask": cap_mask(
                "file_read", "file_write", "symlink_follow"
            ),
            "spawns": [],
        },
        {
            "id": "p-standalone",
            "executable": r"C:\guard\bin\standalone-reader.exe",
            "args_template": ["--path={read_path}"],
            "required_capabilities": ["file_read"],
            "capability_mask": cap_mask("file_read"),
            "spawns": [],
        },
        {
            "id": "p-revive",
            "executable": r"C:\guard\bin\revivable-net.exe",
            "args_template": ["--endpoint={endpoint}"],
            "required_capabilities": ["network_outbound"],
            "capability_mask": cap_mask("network_outbound"),
            "spawns": [],
        },
        {
            "id": "p-clock",
            "executable": r"C:\guard\bin\clock-merge.exe",
            "args_template": [],
            "required_capabilities": ["file_read", "network_outbound"],
            "capability_mask": cap_mask("file_read", "network_outbound"),
            "spawns": [],
        },
        {
            "id": "p-bootstrap",
            "executable": r"C:\guard\bin\bootstrap.exe",
            "args_template": [],
            "required_capabilities": [],
            "capability_mask": 0,
            "spawns": ["p-root", "p-writer", "p-standalone", "p-revive", "p-clock"],
        },
    ]

    extra_count = 2 + (seed % 3)
    core_caps = ["file_read", "file_write", "network_outbound", "symlink_follow"]
    for index in range(extra_count):
        chosen = sorted(
            rng.sample(core_caps, k=1 + rng.randrange(2)),
            key=lambda name: list(CAPABILITY_BITS).index(name),
        )
        profiles.append(
            {
                "id": f"p-seed-{index:02d}",
                "executable": rf"C:\guard\bin\seed-{index:02d}.exe",
                "args_template": [f"--nonce-{rng.randrange(1_000_000):06d}={{value}}"],
                "required_capabilities": chosen,
                "capability_mask": cap_mask(*chosen),
                "spawns": [],
            }
        )

    grants_v1 = [
        {
            "key": "g-clock",
            "profile_id": "p-clock",
            "capabilities": ["file_read"],
            "capability_mask": cap_mask("file_read"),
            "ttl_seconds": 3600,
        },
        {
            "key": "g-writer",
            "profile_id": "p-writer",
            "capabilities": ["file_read", "file_write", "symlink_follow"],
            "capability_mask": cap_mask(
                "file_read", "file_write", "symlink_follow"
            ),
            "ttl_seconds": 7200,
        },
        {
            "key": "g-standalone",
            "profile_id": "p-standalone",
            "capabilities": ["file_read"],
            "capability_mask": cap_mask("file_read"),
            "ttl_seconds": 1800,
        },
    ]

    grants_v2 = [
        {
            "key": "g-edge",
            "profile_id": "p-edge",
            "shard": "edge/00",
            "generation": 1,
            "effective_at_ms": clock,
            "expires_at_ms": clock + 10_000_000,
            "capabilities": ["file_read", "network_outbound"],
            "capability_mask": cap_mask("file_read", "network_outbound"),
            "dependencies": ["g-middle"],
            "tombstone": False,
        },
        {
            "key": "g-middle",
            "profile_id": "p-middle",
            "shard": "middle/00",
            "generation": 1,
            "effective_at_ms": clock,
            "expires_at_ms": clock + 10_000_000,
            "capabilities": ["file_read", "file_write"],
            "capability_mask": cap_mask("file_read", "file_write"),
            "dependencies": ["g-root"],
            "tombstone": False,
        },
        {
            "key": "g-root",
            "profile_id": "p-root",
            "shard": "root/00",
            "generation": 1,
            "effective_at_ms": clock,
            "expires_at_ms": clock + 10_000_000,
            "capabilities": ["file_read", "process_spawn"],
            "capability_mask": cap_mask("file_read", "process_spawn"),
            "dependencies": [],
            "tombstone": False,
        },
        {
            "key": "g-writer",
            "profile_id": "p-writer",
            "generation": 1,
            "effective_at_ms": clock,
            "expires_at_ms": clock + 7_200_000,
            "capabilities": ["file_read", "file_write", "symlink_follow"],
            "capability_mask": cap_mask(
                "file_read", "file_write", "symlink_follow"
            ),
            "dependencies": [],
        },
        {
            "key": "g-standalone",
            "profile_id": "p-standalone",
            "generation": 1,
            "effective_at_ms": clock,
            "expires_at_ms": clock + 1_800_000,
            "capabilities": ["file_read"],
            "capability_mask": cap_mask("file_read"),
            "dependencies": [],
        },
        {
            "key": "g-revive",
            "profile_id": "p-revive",
            "generation": 1,
            "effective_at_ms": clock,
            "expires_at_ms": clock + 10_000_000,
            "capabilities": ["network_outbound"],
            "capability_mask": cap_mask("network_outbound"),
            "dependencies": [],
        },
        {
            "key": "g-clock",
            "profile_id": "p-clock",
            "generation": 1,
            "effective_at_ms": clock,
            "expires_at_ms": clock + 7_200_000,
            "capabilities": ["file_read", "network_outbound"],
            "capability_mask": cap_mask("file_read", "network_outbound"),
            "dependencies": [],
        },
    ]
    rng.shuffle(grants_v2)

    processes = [
        {"pid": 4000, "profile_id": "p-bootstrap", "parent_pid": None, "status": "running"},
        {"pid": 4101, "profile_id": "p-root", "parent_pid": 4000, "status": "running"},
        {"pid": 4102, "profile_id": "p-middle", "parent_pid": 4101, "status": "running"},
        {"pid": 4103, "profile_id": "p-edge", "parent_pid": 4102, "status": "running"},
        {"pid": 4104, "profile_id": "p-writer", "parent_pid": 4000, "status": "running"},
        {"pid": 4105, "profile_id": "p-standalone", "parent_pid": 4000, "status": "suspended"},
        {"pid": 4106, "profile_id": "p-revive", "parent_pid": 4000, "status": "running"},
        {"pid": 4107, "profile_id": "p-clock", "parent_pid": 4000, "status": "running"},
        {"pid": 4199, "profile_id": "p-edge", "parent_pid": 4102, "status": "exited"},
    ]

    policy = {
        "schema": "sandbox-guard-sample/1",
        "seed": seed,
        "path_style": "windows",
        "observation_clock_ms": OBSERVATION_CLOCK_MS,
        "default_evaluation_clock_ms": DEFAULT_EVALUATION_CLOCK_MS,
        "capability_whitelist_version": CAPABILITY_WHITELIST_VERSION,
        "merge_policy_version": MERGE_POLICY_VERSION,
        "checkpoint_format_version": CHECKPOINT_FORMAT_VERSION,
        "method_sync_marker": METHOD_SYNC_MARKER,
        "markers": {
            "observation_clock_declared": True,
            "grant_generation_declared": True,
            "method_sync_marker": METHOD_SYNC_MARKER,
            "fixed_seed": seed,
            "clock_source": "sample.observation_clock_ms",
            "authority_order": [
                "generation",
                "effective_at_ms",
                "canonical_key",
                "fact_id",
            ],
            "rng_nonneg_nonce": rng.randrange(1 << 30),
        },
        "profiles": profiles,
        "grants_v1": grants_v1,
        "grants_v2": grants_v2,
        "processes": processes,
    }

    patches = {
        "patch-100-revoke-root.json": {
            "schema": "sandbox-guard-patch/1",
            "batch_id": "patch-100-revoke-root",
            "watermark": 100,
            "operations": [
                {
                    "op_id": "root-tombstone-gen10",
                    "op": "tombstone",
                    "key": "g-root",
                    "generation": 10,
                    "effective_at_ms": clock + 1_000,
                    "reason": "fixed sample root revocation",
                }
            ],
        },
        "patch-200-reduce-and-dependency.json": {
            "schema": "sandbox-guard-patch/1",
            "batch_id": "patch-200-reduce-and-dependency",
            "watermark": 200,
            "operations": [
                {
                    "op_id": "writer-remove-symlink-gen20",
                    "op": "reduce_capabilities",
                    "key": "g-writer",
                    "generation": 20,
                    "effective_at_ms": clock + 2_000,
                    "remove_capabilities": ["symlink_follow"],
                    "remove_mask": CAPABILITY_BITS["symlink_follow"],
                },
                {
                    "op_id": "standalone-depends-middle-gen20",
                    "op": "set_dependencies",
                    "key": "g-standalone",
                    "generation": 20,
                    "effective_at_ms": clock + 2_001,
                    "dependencies": ["g-middle"],
                },
            ],
        },
        "patch-300-revive-tombstone.json": {
            "schema": "sandbox-guard-patch/1",
            "batch_id": "patch-300-revive-tombstone",
            "watermark": 300,
            "operations": [
                {
                    "op_id": "revive-tombstone-gen50",
                    "op": "tombstone",
                    "key": "g-revive",
                    "generation": 50,
                    "effective_at_ms": clock + 3_000,
                }
            ],
        },
        "patch-400-revive-restore.json": {
            "schema": "sandbox-guard-patch/1",
            "batch_id": "patch-400-revive-restore",
            "watermark": 400,
            "operations": [
                {
                    "op_id": "revive-restore-gen60",
                    "op": "add_grant",
                    "key": "g-revive",
                    "profile_id": "p-revive",
                    "generation": 60,
                    "effective_at_ms": clock + 4_000,
                    "expires_at_ms": clock + 20_000_000,
                    "capabilities": ["network_outbound"],
                    "capability_mask": CAPABILITY_BITS["network_outbound"],
                    "dependencies": [],
                }
            ],
        },
    }

    invalid_batch = {
        "schema": "sandbox-guard-patch/1",
        "batch_id": "invalid-900-bad-records",
        "watermark": "not-an-integer",
        "operations": [
            {
                "op_id": "well-formed-but-batch-still-atomic",
                "op": "tombstone",
                "key": "g-standalone",
                "generation": 90,
                "effective_at_ms": clock + 9_000,
            },
            {
                "op_id": "bad-key",
                "op": "tombstone",
                "key": "bad key!",
                "generation": 1,
                "effective_at_ms": clock,
            },
            {
                "op_id": "bad-generation",
                "op": "tombstone",
                "key": "g-root",
                "generation": -1,
                "effective_at_ms": clock,
            },
            {
                "op_id": "missing-dependency",
                "op": "set_dependencies",
                "key": "g-writer",
                "generation": 2,
                "effective_at_ms": clock,
                "dependencies": ["g-does-not-exist"],
            },
            {
                "op_id": "unknown-target",
                "op": "reduce_capabilities",
                "key": "g-ghost",
                "generation": 2,
                "effective_at_ms": clock,
                "remove_capabilities": ["file_read"],
            },
        ],
    }

    invalid_policy_records = {
        "schema": "sandbox-guard-invalid-records/1",
        "observation_clock_ms": clock,
        "profiles": [
            {
                "id": "p-valid-after-error",
                "executable": r"C:\guard\bin\valid.exe",
                "required_capabilities": ["file_read"],
                "capability_mask": 1,
            },
            {
                "id": "p-bad-path",
                "executable": "not-absolute-or-extension",
                "required_capabilities": ["file_read"],
                "capability_mask": 1,
            },
            {
                "id": "p-valid-duplicate",
                "executable": r"C:\guard\bin\dup.exe",
                "required_capabilities": [],
                "capability_mask": 0,
            },
            {
                "id": "p-valid-duplicate",
                "executable": r"C:\guard\bin\dup2.exe",
                "required_capabilities": [],
                "capability_mask": 0,
            },
            {
                "id": "p-cap-mismatch",
                "executable": r"C:\guard\bin\cap.exe",
                "required_capabilities": ["file_read"],
                "capability_mask": 4,
            },
        ],
        "grants_v1": [
            {
                "key": "g-bad-ttl",
                "profile_id": "p-valid-after-error",
                "capabilities": ["file_read"],
                "capability_mask": 1,
                "ttl_seconds": -3,
            },
            {
                "key": "g-v1-deps",
                "profile_id": "p-valid-after-error",
                "capabilities": ["file_read"],
                "capability_mask": 1,
                "ttl_seconds": 10,
                "dependencies": ["g-other"],
            },
        ],
        "grants_v2": [
            {
                "key": "g-bad-generation",
                "profile_id": "p-valid-after-error",
                "effective_at_ms": clock,
                "generation": "one",
                "capabilities": [],
                "capability_mask": 0,
                "dependencies": [],
            },
            {
                "key": "g-missing-dep",
                "profile_id": "p-valid-after-error",
                "effective_at_ms": clock,
                "generation": 2,
                "capabilities": [],
                "capability_mask": 0,
                "dependencies": ["g-unresolved"],
            },
        ],
    }

    expected = {
        "seed": seed,
        "observation_clock_ms": clock,
        "default_evaluation_clock_ms": DEFAULT_EVALUATION_CLOCK_MS,
        "method_sync_marker": METHOD_SYNC_MARKER,
        "conflict_fields": [
            ["grant:g-clock", "capabilities"],
            ["grant:g-clock", "expires_at_ms"],
        ],
        "valid_patch_closure": {
            "trigger_keys": ["grant:g-root", "grant:g-writer"],
            "closure_grant_keys": [
                "grant:g-edge",
                "grant:g-middle",
                "grant:g-root",
                "grant:g-standalone",
                "grant:g-writer",
            ],
            "affected_pids": [4101, 4102, 4103, 4104, 4105],
            "denied_pids": [4101, 4102, 4103, 4104, 4105],
            "alive_pids": [4000, 4106, 4107],
            "revived_pid": 4106,
        },
        "order_independence_compare": [
            "unified_view.grants[*].canonical_key",
            "unified_view.grants[*].effective_mask",
            "unified_view.processes[*].canonical_key",
            "unified_view.processes[*].alive",
            "closure_grant_keys",
            "affected_pids",
            "final_denied_process_keys",
            "rejections",
        ],
    }

    return {
        "policy": policy,
        "patches": patches,
        "invalid_batch": invalid_batch,
        "invalid_policy_records": invalid_policy_records,
        "expected": expected,
    }


def generate_sample(out_dir: str | Path, seed: int = 361) -> dict[str, Any]:
    bundle = build_sample(seed)
    root = Path(out_dir)
    patches_dir = root / "patches"
    invalid_dir = root / "invalid"
    patches_dir.mkdir(parents=True, exist_ok=True)
    invalid_dir.mkdir(parents=True, exist_ok=True)

    atomic_write_json(root / "policy.json", bundle["policy"])
    for filename, payload in bundle["patches"].items():
        atomic_write_json(patches_dir / filename, payload)
    atomic_write_json(invalid_dir / "bad-batch.json", bundle["invalid_batch"])
    atomic_write_json(
        invalid_dir / "policy-records.json",
        bundle["invalid_policy_records"],
    )
    atomic_write_json(root / "expected-summary.json", bundle["expected"])
    atomic_write_json(
        root / "README.json",
        {
            "generated_by": "python -m sandbox_guard generate-sample",
            "seed": seed,
            "method_sync_marker": METHOD_SYNC_MARKER,
            "verification": "python -m pytest -q",
        },
    )
    return {
        "root": str(root),
        "policy": str(root / "policy.json"),
        "patches": sorted(str(path) for path in patches_dir.glob("*.json")),
        "invalid": sorted(str(path) for path in invalid_dir.glob("*.json")),
        "expected": str(root / "expected-summary.json"),
    }
