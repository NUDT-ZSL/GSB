"""Deterministic local samples for the dependency diagnostics component.

The generator deliberately creates two policy formats, one incremental v2
package, branch-local label changes, a tombstone, a namespace invalidation,
topology breaks, concurrency pressure, resource pressure, a started failure,
an explicitly abandoned request, and a round-budget exhaustion case.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .util import pretty_json, sha256_bytes, write_json_atomic

DEFAULT_SEED = 20261126
DEFAULT_INJECTED_CLOCK_MS = 12_000


def _resources(cpu: int, memory: int) -> dict[str, int]:
    return {"cpu_millis": cpu, "memory_mb": memory}


def _template(
    name: str,
    version: int,
    dependencies: list[str],
    labels: list[str],
    logical_clock: int,
    cpu: int,
    memory: int,
) -> dict[str, Any]:
    return {
        "name": name,
        "version": version,
        "dependencies": dependencies,
        "labels": labels,
        "logical_clock": logical_clock,
        "resource_requirements": _resources(cpu, memory),
    }


def sample_templates() -> list[dict[str, Any]]:
    return [
        _template("base", 2, [], ["core", "tier:base"], 12_000, 50, 64),
        _template(
            "catalog",
            2,
            ["base"],
            ["data", "tier:data"],
            12_000,
            100,
            128,
        ),
        _template(
            "orders",
            4,
            ["base", "catalog"],
            ["commerce", "tier:service"],
            12_000,
            200,
            256,
        ),
        _template(
            "frontend",
            7,
            ["orders", "catalog"],
            ["edge", "tier:web"],
            12_000,
            250,
            256,
        ),
        _template(
            "analytics",
            2,
            ["catalog"],
            ["analytics", "channel:batch", "tier:data"],
            12_000,
            150,
            192,
        ),
        _template(
            "report",
            2,
            ["analytics", "base"],
            ["reporting", "tier:data"],
            12_000,
            120,
            160,
        ),
        _template(
            "notifications",
            3,
            ["orders"],
            ["async", "commerce"],
            12_000,
            90,
            96,
        ),
        _template(
            "legacy",
            5,
            ["base"],
            ["legacy"],
            12_000,
            80,
            80,
        ),
        _template(
            "retired",
            1,
            ["base"],
            ["legacy", "retired"],
            12_000,
            60,
            64,
        ),
        _template(
            "archive",
            1,
            ["base"],
            ["archive", "tier:data"],
            12_000,
            40,
            48,
        ),
    ]


def _v1_entry(
    name: str,
    version: int,
    dependencies: list[str],
    labels: list[str],
    *,
    logical_clock: int = 10_000,
) -> dict[str, Any]:
    return {
        "version": version,
        "dependencies": dependencies,
        "labels": labels,
        "logical_clock": logical_clock,
    }


def sample_v1_policy() -> dict[str, Any]:
    return {
        "format": "orchestration-policy/v1",
        "policy_key": "main",
        "policy_generation": 2,
        "manifest_version": "1.0.0",
        "logical_clock": 10_000,
        "relative_effective_seconds": 5,
        "no_topology_constraints": True,
        "template_dependencies": {
            "base": [],
            "catalog": ["base"],
            "orders": ["catalog"],
            "frontend": ["orders", "catalog"],
            "analytics": ["catalog"],
            "report": ["analytics"],
            "notifications": ["orders"],
            "legacy": ["base"],
            "retired": ["base"],
            "archive": ["base"],
        },
        "template_versions": {
            "base": 1,
            "catalog": 1,
            "orders": 3,
            "frontend": 6,
            "analytics": 1,
            "report": 1,
            "notifications": 2,
            "legacy": 5,
            "retired": 1,
            "archive": 1,
        },
        "template_labels": {
            "base": ["core", "tier:base"],
            "catalog": ["data", "tier:data"],
            "orders": ["commerce", "tier:service"],
            "frontend": ["edge", "tier:web"],
            "analytics": ["analytics", "tier:data"],
            "report": ["reporting", "tier:data"],
            "notifications": ["async", "commerce"],
            "legacy": ["legacy"],
            "retired": ["legacy", "retired"],
            "archive": ["archive", "tier:data"],
        },
        "template_entries": {
            "catalog": _v1_entry(
                "catalog",
                1,
                ["base"],
                ["data", "tier:data"],
                logical_clock=12_000,
            ),
            "notifications": _v1_entry(
                "notifications",
                2,
                ["orders"],
                ["async", "commerce"],
                logical_clock=12_000,
            ),
        },
    }


def _v2_template(
    version: int,
    dependencies: list[str],
    labels: list[str],
) -> dict[str, Any]:
    return {
        "version": version,
        "dependencies": dependencies,
        "labels": labels,
    }


def sample_v2_templates(generation: int) -> dict[str, dict[str, Any]]:
    if generation == 2:
        return {
            "base": _v2_template(2, [], ["core", "tier:base"]),
            "catalog": _v2_template(2, ["base"], ["data", "tier:data"]),
            "orders": _v2_template(
                4,
                ["base", "catalog"],
                ["commerce", "tier:service"],
            ),
            "frontend": _v2_template(
                7,
                ["orders", "catalog"],
                ["edge", "tier:web"],
            ),
            "analytics": _v2_template(
                2,
                ["catalog"],
                ["analytics", "channel:batch", "tier:data"],
            ),
            "report": _v2_template(
                2,
                ["analytics", "base"],
                ["reporting", "tier:data"],
            ),
            "notifications": _v2_template(
                3,
                ["orders"],
                ["async", "commerce"],
            ),
            "legacy": _v2_template(5, ["base"], ["legacy"]),
            "retired": _v2_template(1, ["base"], ["legacy", "retired"]),
        }
    if generation == 3:
        return {
            "analytics": _v2_template(
                3,
                ["catalog"],
                ["analytics", "channel:realtime", "tier:data"],
            ),
        }
    raise BusinessError(
        "SAMPLE_GENERATION_UNSUPPORTED",
        f"Unsupported sample policy generation: {generation}",
        version=generation,
    )


def _dependency_digest(templates: dict[str, dict[str, Any]]) -> str:
    payload = {
        name: sorted(entry["dependencies"])
        for name, entry in sorted(templates.items())
    }
    return sha256_bytes(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _write_metadata_db(
    path: Path,
    *,
    policy_key: str,
    generation: int,
    generation_floor: int,
    manifest_version: str,
    logical_clock: int,
    absolute_effective_ms: int,
    update_mode: str,
    base_generation: int | None,
    dependency_digest: str,
    shards: list[dict[str, Any]],
    tombstones: list[dict[str, Any]],
    topology_invalidations: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd_path = path
    if fd_path.exists():
        fd_path.unlink()
    connection = sqlite3.connect(str(fd_path))
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE package_info (
                policy_key TEXT NOT NULL,
                policy_generation INTEGER NOT NULL,
                generation_floor INTEGER NOT NULL,
                manifest_version TEXT NOT NULL,
                logical_clock INTEGER NOT NULL,
                absolute_effective_ms INTEGER NOT NULL,
                update_mode TEXT NOT NULL,
                base_generation INTEGER,
                dependency_digest TEXT NOT NULL,
                shard_count INTEGER NOT NULL
            );
            CREATE TABLE shard_metadata (
                shard_index INTEGER PRIMARY KEY,
                filename TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                entry_count INTEGER NOT NULL
            );
            CREATE TABLE closure_tombstones (
                template_name TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                logical_clock INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE topology_invalidations (
                invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                logical_clock INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO package_info(
                policy_key, policy_generation, generation_floor,
                manifest_version, logical_clock, absolute_effective_ms,
                update_mode, base_generation, dependency_digest, shard_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_key,
                generation,
                generation_floor,
                manifest_version,
                logical_clock,
                absolute_effective_ms,
                update_mode,
                base_generation,
                dependency_digest,
                len(shards),
            ),
        )
        for shard in shards:
            connection.execute(
                """
                INSERT INTO shard_metadata(
                    shard_index, filename, sha256, entry_count
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    shard["shard_index"],
                    shard["filename"],
                    shard["sha256"],
                    shard["entry_count"],
                ),
            )
        for tombstone in tombstones:
            connection.execute(
                """
                INSERT INTO closure_tombstones(
                    template_name, version, logical_clock, reason
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    tombstone["template_name"],
                    tombstone["version"],
                    tombstone["logical_clock"],
                    tombstone["reason"],
                ),
            )
        for invalidation in topology_invalidations:
            connection.execute(
                """
                INSERT INTO topology_invalidations(
                    scope, logical_clock, reason
                ) VALUES (?, ?, ?)
                """,
                (
                    invalidation["scope"],
                    invalidation["logical_clock"],
                    invalidation["reason"],
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _write_v2_package(
    package_dir: Path,
    *,
    generation: int,
    generation_floor: int,
    manifest_version: str,
    logical_clock: int,
    absolute_effective_ms: int,
    update_mode: str,
    base_generation: int | None,
    templates: dict[str, dict[str, Any]],
    tombstones: list[dict[str, Any]],
    topology_invalidations: list[dict[str, Any]],
    effective_templates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)
    names = sorted(templates)
    split = max(1, (len(names) + 1) // 2)
    chunks = [names[:split], names[split:]] if len(names) > 1 else [names]
    shards: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        shard_payload = {
            "format": "orchestration-policy/v2-shard",
            "policy_key": "main",
            "policy_generation": generation,
            "logical_clock": logical_clock,
            "absolute_effective_ms": absolute_effective_ms,
            "shard_index": index,
            "templates": {name: templates[name] for name in chunk},
        }
        filename = f"shard-{index:03d}.json"
        path = package_dir / filename
        data = (pretty_json(shard_payload) + "\n").encode("utf-8")
        path.write_bytes(data)
        shards.append(
            {
                "shard_index": index,
                "filename": filename,
                "sha256": sha256_bytes(data),
                "entry_count": len(chunk),
            }
        )
    dependency_digest = _dependency_digest(effective_templates)
    metadata_path = package_dir / "metadata.sqlite"
    _write_metadata_db(
        metadata_path,
        policy_key="main",
        generation=generation,
        generation_floor=generation_floor,
        manifest_version=manifest_version,
        logical_clock=logical_clock,
        absolute_effective_ms=absolute_effective_ms,
        update_mode=update_mode,
        base_generation=base_generation,
        dependency_digest=dependency_digest,
        shards=shards,
        tombstones=tombstones,
        topology_invalidations=topology_invalidations,
    )
    manifest = {
        "format": "orchestration-policy-package/v2",
        "policy_key": "main",
        "policy_generation": generation,
        "generation_floor": generation_floor,
        "manifest_version": manifest_version,
        "logical_clock": logical_clock,
        "absolute_effective_ms": absolute_effective_ms,
        "update_mode": update_mode,
        "base_generation": base_generation,
        "dependency_digest": dependency_digest,
        "metadata_file": "metadata.sqlite",
        "shard_count": len(shards),
        "tombstones": tombstones,
        "topology_invalidations": topology_invalidations,
    }
    write_json_atomic(package_dir / "manifest.json", manifest)
    return manifest


def _sample_requests() -> list[dict[str, Any]]:
    return [
        {
            "request_seq": 1,
            "template_name": "frontend",
            "target_topology_namespace": "ns-blue",
            "instance_id": "inst-0001",
            "desired_state": "active",
            "logical_clock": 12_100,
            "round": 1,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 2,
            "template_name": "frontend",
            "target_topology_namespace": "ns-blue",
            "instance_id": "inst-0002",
            "desired_state": "active",
            "logical_clock": 12_110,
            "round": 1,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 3,
            "template_name": "frontend",
            "target_topology_namespace": "ns-blue",
            "instance_id": "inst-0003",
            "desired_state": "active",
            "logical_clock": 12_200,
            "round": 2,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 4,
            "template_name": "analytics",
            "target_topology_namespace": "ns-green",
            "instance_id": "inst-0004",
            "desired_state": "active",
            "logical_clock": 12_300,
            "round": 3,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 5,
            "template_name": "report",
            "target_topology_namespace": "ns-green",
            "instance_id": "inst-0005",
            "desired_state": "active",
            "logical_clock": 12_310,
            "round": 3,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 6,
            "template_name": "orders",
            "target_topology_namespace": "ns-blue",
            "instance_id": "inst-0006",
            "desired_state": "active",
            "logical_clock": 12_400,
            "round": 4,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 7,
            "template_name": "notifications",
            "target_topology_namespace": "ns-blue",
            "instance_id": "inst-0007",
            "desired_state": "active",
            "logical_clock": 12_410,
            "round": 4,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 8,
            "template_name": "analytics",
            "target_topology_namespace": "ns-green",
            "instance_id": "inst-0008",
            "desired_state": "active",
            "logical_clock": 12_500,
            "round": 5,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 9,
            "template_name": "legacy",
            "target_topology_namespace": "ns-green",
            "instance_id": "inst-0009",
            "desired_state": "active",
            "logical_clock": 12_600,
            "round": 5,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 10,
            "template_name": "retired",
            "target_topology_namespace": "ns-red",
            "instance_id": "inst-0010",
            "desired_state": "active",
            "logical_clock": 12_700,
            "round": 6,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 11,
            "template_name": "frontend",
            "target_topology_namespace": "ns-red",
            "instance_id": "inst-0011",
            "desired_state": "active",
            "logical_clock": 12_710,
            "round": 6,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 12,
            "template_name": "orders",
            "target_topology_namespace": "ns-amber",
            "instance_id": "inst-0012",
            "desired_state": "active",
            "logical_clock": 12_800,
            "round": 7,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 13,
            "template_name": "notifications",
            "target_topology_namespace": "ns-amber",
            "instance_id": "inst-0013",
            "desired_state": "active",
            "logical_clock": 12_810,
            "round": 7,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 14,
            "template_name": "report",
            "target_topology_namespace": "ns-green",
            "instance_id": "inst-0014",
            "desired_state": "active",
            "logical_clock": 12_900,
            "round": 8,
            "logical_cost_ms": 100,
            "inject_start_failure": True,
        },
        {
            "request_seq": 15,
            "template_name": "catalog",
            "target_topology_namespace": "ns-green",
            "instance_id": "inst-0015",
            "desired_state": "abandoned",
            "logical_clock": 12_910,
            "round": 8,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 16,
            "template_name": "frontend",
            "target_topology_namespace": "ns-amber",
            "instance_id": "inst-0016",
            "desired_state": "active",
            "logical_clock": 12_920,
            "round": 9,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 17,
            "template_name": "frontend",
            "target_topology_namespace": "ns-green",
            "instance_id": "inst-0017",
            "desired_state": "active",
            "logical_clock": 12_930,
            "round": 9,
            "logical_cost_ms": 100,
        },
        {
            "request_seq": 18,
            "template_name": "orders",
            "target_topology_namespace": "ns-green",
            "instance_id": "inst-0018",
            "desired_state": "active",
            "logical_clock": 12_940,
            "round": 9,
            "logical_cost_ms": 100,
        },
    ]


def _sample_topology() -> dict[str, Any]:
    return {
        "format": "topology-capacity/v1",
        "max_depth": 16,
        "nodes": {
            "node-a": {
                "resources": {
                    "cpu_millis": 6_000,
                    "memory_mb": 8_192,
                },
                "template_concurrency": {
                    "frontend": 2,
                    "orders": 3,
                    "notifications": 3,
                    "analytics": 2,
                    "report": 2,
                },
            },
            "node-b": {
                "resources": {
                    "cpu_millis": 1_200,
                    "memory_mb": 2_048,
                },
                "template_concurrency": {
                    "retired": 2,
                    "frontend": 2,
                },
            },
            "node-c": {
                "resources": {
                    "cpu_millis": 1_000,
                    "memory_mb": 1_024,
                },
                "template_concurrency": {
                    "orders": 1,
                    "notifications": 2,
                },
            },
        },
        "namespaces": {
            "ns-blue": {
                "node_id": "node-a",
                "topology_constraints": {"max_depth": 8},
            },
            "ns-green": {
                "node_id": "node-a",
                "topology_constraints": {"max_depth": 8},
            },
            "ns-red": {
                "node_id": "node-b",
                "topology_constraints": {"max_depth": 8},
            },
            "ns-amber": {
                "node_id": "node-c",
                "topology_constraints": {"max_depth": 8},
            },
        },
    }


def generate_samples(workdir: Path, *, seed: int = DEFAULT_SEED, force: bool = False) -> dict[str, Any]:
    generated_paths = [
        workdir / "samples",
        workdir / "runtime.sqlite3",
        workdir / "runtime.sqlite3-wal",
        workdir / "runtime.sqlite3-shm",
    ]
    if any(path.exists() for path in generated_paths) and not force:
        raise BusinessError(
            "WORKDIR_NOT_EMPTY",
            "Work directory already contains generated state; use --force to "
            "regenerate samples",
            workdir=str(workdir),
        )
    workdir.mkdir(parents=True, exist_ok=True)
    samples_dir = workdir / "samples"
    if samples_dir.exists():
        shutil.rmtree(samples_dir)
    samples_dir.mkdir(parents=True)
    policies_dir = samples_dir / "policies"
    policies_dir.mkdir()

    templates = sample_templates()
    templates_payload = {
        "format": "service-template-manifest/v1",
        "manifest_version": "1.0.0",
        "seed": seed,
        "injected_logical_clock_ms": DEFAULT_INJECTED_CLOCK_MS,
        "templates": templates,
    }
    requests_payload = {
        "format": "instantiation-request-stream/v1",
        "requests": _sample_requests(),
    }
    topology_payload = _sample_topology()

    write_json_atomic(samples_dir / "templates.json", templates_payload)
    write_json_atomic(samples_dir / "requests.json", requests_payload)
    write_json_atomic(samples_dir / "topology.json", topology_payload)
    write_json_atomic(policies_dir / "v1.json", sample_v1_policy())

    v2_dir = policies_dir / "v2"
    v2_dir.mkdir()
    v2_templates = sample_v2_templates(2)
    v2_manifest = _write_v2_package(
        v2_dir,
        generation=2,
        generation_floor=2,
        manifest_version="2.0.0",
        logical_clock=12_000,
        absolute_effective_ms=12_000,
        update_mode="full",
        base_generation=None,
        templates=v2_templates,
        tombstones=[
            {
                "template_name": "legacy",
                "version": 5,
                "logical_clock": 12_000,
                "reason": "legacy-dependency-closure-retired",
            }
        ],
        topology_invalidations=[],
        effective_templates=v2_templates,
    )

    update_dir = policies_dir / "v2-update"
    v2_effective_after_update = dict(v2_templates)
    v2_effective_after_update.update(sample_v2_templates(3))
    update_manifest = _write_v2_package(
        update_dir,
        generation=3,
        generation_floor=2,
        manifest_version="3.0.0",
        logical_clock=13_000,
        absolute_effective_ms=13_000,
        update_mode="incremental",
        base_generation=2,
        templates=sample_v2_templates(3),
        tombstones=[
            {
                "template_name": "retired",
                "version": 1,
                "logical_clock": 13_000,
                "reason": "retired-by-generation-3",
            }
        ],
        topology_invalidations=[
            {
                "scope": "namespace:ns-red",
                "logical_clock": 13_000,
                "reason": "namespace-topology-invalidated",
            }
        ],
        effective_templates=v2_effective_after_update,
    )

    input_manifest = {
        "format": "topodiag-sample-manifest/v1",
        "seed": seed,
        "injected_logical_clock_ms": DEFAULT_INJECTED_CLOCK_MS,
        "files": {
            "templates": "templates.json",
            "requests": "requests.json",
            "topology": "topology.json",
            "v1_policy": "policies/v1.json",
            "v2_policy": "policies/v2",
            "v2_update_policy": "policies/v2-update",
        },
        "v2_generation": v2_manifest["policy_generation"],
        "v2_dependency_digest": v2_manifest["dependency_digest"],
        "v2_update_generation": update_manifest["policy_generation"],
        "v2_update_dependency_digest": update_manifest["dependency_digest"],
    }
    write_json_atomic(samples_dir / "input_manifest.json", input_manifest)
    write_json_atomic(
        samples_dir / "expected_scenarios.json",
        {
            "namespace_groups": ["ns-amber", "ns-blue", "ns-green", "ns-red"],
            "policy_merge": {
                "v1_relative_seconds": 5,
                "injected_clock_ms": DEFAULT_INJECTED_CLOCK_MS,
                "expected_v1_effective_ms": 17_000,
                "same_clock_tie": "v2_wins_source_priority",
            },
            "invalidation": {
                "trigger_template": "analytics",
                "expected_downstream": ["report"],
                "unaffected_branch_examples": ["orders", "frontend"],
            },
            "safety": {
                "legacy_tombstone_rejects_topology": True,
                "round_budget_exhaustion_expected": True,
                "explicit_abandon_expected": True,
                "started_failure_expected": True,
            },
        },
    )
    return {
        "ok": True,
        "command": "load",
        "workdir": str(workdir),
        "seed": seed,
        "samples_dir": str(samples_dir),
        "input_manifest": input_manifest,
    }


def default_round_budget_ms() -> int:
    """Round 4 and round 5 intentionally exceed this budget."""

    return 250
