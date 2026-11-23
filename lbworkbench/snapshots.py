from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .errors import WorkbenchError, bad_snapshot, hash_mismatch
from .util import (
    canonical_instance_id,
    canonical_json,
    load_json_file,
    require_bool,
    require_int,
    require_str,
    safe_resolve,
    sha256_bytes,
    sha256_file,
    sha256_json,
)


V1_STATUSES = {"healthy", "suspect", "unhealthy"}
V2_STATUSES = {"healthy", "suspect", "unhealthy"}


def _require_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise bad_snapshot(f"{field_name} must be an object", field=field_name)
    return value


def _require_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise bad_snapshot(f"{field_name} must be an array", field=field_name)
    return value


def _require_enum(value: Any, field_name: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise bad_snapshot(
            f"{field_name} must be one of {sorted(allowed)}",
            field=field_name,
            value=value,
        )
    return value


def _check_declared_hash(
    path: Path,
    declared: Any,
    *,
    field_name: str,
) -> str:
    actual = sha256_file(path)
    if declared is not None:
        if not isinstance(declared, str):
            raise bad_snapshot(f"{field_name} must be a hex digest")
        if declared.lower() != actual:
            raise hash_mismatch(
                f"{field_name} does not match file content",
                field=field_name,
                expected=declared.lower(),
                actual=actual,
                path=str(path.name),
            )
    return actual


def _parse_v1(snapshot_path: Path) -> dict[str, Any]:
    obj = _require_object(load_json_file(snapshot_path), "v1.snapshot")
    if obj.get("schema") != "lb-v1-snapshot/v1":
        raise bad_snapshot(
            "v1 schema must be lb-v1-snapshot/v1",
            field="schema",
            value=obj.get("schema"),
        )
    snapshot_id = require_str(obj.get("snapshot_id"), "v1.snapshot_id")
    clock = require_int(obj.get("logical_clock_ms"), "v1.logical_clock_ms", minimum=0)
    generation = require_int(obj.get("state_generation"), "v1.state_generation", minimum=0)
    threshold = require_int(obj.get("failure_threshold"), "v1.failure_threshold", minimum=1)
    default_weight = require_int(
        obj.get("default_effective_weight", 100),
        "v1.default_effective_weight",
        minimum=0,
    )
    priority = require_int(obj.get("source_priority", 1), "v1.source_priority", minimum=0)
    rows = _require_list(obj.get("instances"), "v1.instances")
    if not rows:
        raise bad_snapshot("v1.instances must not be empty", field="v1.instances")

    instances: dict[str, dict[str, Any]] = {}
    exact_ids: set[str] = set()
    for index, raw in enumerate(rows):
        item = _require_object(raw, f"v1.instances[{index}]")
        original_id = require_str(item.get("instance_id"), f"v1.instances[{index}].instance_id")
        if original_id in exact_ids:
            raise bad_snapshot(
                "v1 contains duplicate instance_id",
                field="instances[].instance_id",
                value=original_id,
            )
        exact_ids.add(original_id)
        canonical = canonical_instance_id(original_id, "v1.instance_id")
        if canonical in instances:
            raise bad_snapshot(
                "v1 instance IDs collide after canonicalization",
                field="instances[].instance_id",
                value=original_id,
                canonical_id=canonical,
            )
        relative_seconds = require_int(
            item.get("relative_failure_seconds"),
            f"v1.instances[{index}].relative_failure_seconds",
            minimum=0,
        )
        failures = require_int(
            item.get("consecutive_failures"),
            f"v1.instances[{index}].consecutive_failures",
            minimum=0,
        )
        if failures > threshold:
            raise bad_snapshot(
                "v1 consecutive_failures exceeds declared threshold",
                field="instances[].consecutive_failures",
                instance_id=original_id,
                value=failures,
                threshold=threshold,
            )
        health = _require_enum(
            item.get("status"),
            f"v1.instances[{index}].status",
            V1_STATUSES,
        )
        instances[canonical] = {
            "canonical_instance_id": canonical,
            "original_instance_id": original_id,
            "relative_failure_seconds": relative_seconds,
            "consecutive_failures": failures,
            "status": health,
        }

    return {
        "format": "v1",
        "snapshot_id": snapshot_id,
        "logical_clock_ms": clock,
        "state_generation": generation,
        "source_priority": priority,
        "failure_threshold": threshold,
        "default_effective_weight": default_weight,
        "instances": instances,
        "file_sha256": sha256_file(snapshot_path),
    }


def _open_meta_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise bad_snapshot(
            "v2 metadata SQLite file does not exist",
            path=str(path),
        )
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        raise bad_snapshot(
            "cannot open v2 metadata SQLite database",
            path=str(path),
            reason=str(exc),
        ) from exc


def _parse_v2_meta(
    meta_path: Path,
    *,
    expected_snapshot_id: str,
    expected_version: str,
    expected_clock: int,
    expected_floor: int,
    expected_shards: list[dict[str, Any]],
    expected_instance_digest: str,
) -> dict[str, Any]:
    conn = _open_meta_readonly(meta_path)
    try:
        required_tables = {"snapshot_meta", "shard_meta"}
        found = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = sorted(required_tables - found)
        if missing:
            raise bad_snapshot(
                "v2 metadata SQLite database is missing tables",
                missing_tables=missing,
            )
        snapshot_rows = list(conn.execute("SELECT * FROM snapshot_meta"))
        if len(snapshot_rows) != 1:
            raise bad_snapshot(
                "snapshot_meta must contain exactly one row",
                row_count=len(snapshot_rows),
            )
        meta = dict(snapshot_rows[0])
        expected_meta = {
            "snapshot_id": expected_snapshot_id,
            "package_version": expected_version,
            "logical_clock_ms": expected_clock,
            "state_generation_floor": expected_floor,
            "shard_count": len(expected_shards),
            "instance_set_sha256": expected_instance_digest,
        }
        for field_name, expected in expected_meta.items():
            if meta.get(field_name) != expected:
                raise bad_snapshot(
                    "v2 SQLite metadata does not match the manifest",
                    field=f"snapshot_meta.{field_name}",
                    expected=expected,
                    actual=meta.get(field_name),
                )

        shard_rows = [dict(row) for row in conn.execute("SELECT * FROM shard_meta")]
        if len(shard_rows) != len(expected_shards):
            raise bad_snapshot(
                "shard_meta row count does not match manifest",
                expected=len(expected_shards),
                actual=len(shard_rows),
            )
        expected_by_id = {shard["shard_id"]: shard for shard in expected_shards}
        if {row["shard_id"] for row in shard_rows} != set(expected_by_id):
            raise bad_snapshot(
                "shard_meta shard IDs do not match manifest",
                expected=sorted(expected_by_id),
                actual=sorted(row["shard_id"] for row in shard_rows),
            )
        for row in shard_rows:
            expected = expected_by_id[row["shard_id"]]
            checks = {
                "path": expected["path"],
                "sha256": expected["sha256"],
                "generation_min": expected["generation_min"],
                "generation_max": expected["generation_max"],
                "record_count": expected["record_count"],
            }
            for field_name, value in checks.items():
                if row.get(field_name) != value:
                    raise bad_snapshot(
                        "shard_meta does not match shard JSON",
                        field=f"shard_meta[{row['shard_id']}].{field_name}",
                        expected=value,
                        actual=row.get(field_name),
                    )
        return {
            "snapshot_meta": meta,
            "shard_meta": sorted(shard_rows, key=lambda row: row["shard_id"]),
        }
    except sqlite3.Error as exc:
        raise bad_snapshot(
            "cannot validate v2 metadata SQLite database",
            path=str(meta_path),
            reason=str(exc),
        ) from exc
    finally:
        conn.close()


def _parse_v2(
    manifest_path: Path,
    meta_path: Path,
    *,
    package_version: str,
) -> dict[str, Any]:
    manifest = _require_object(load_json_file(manifest_path), "v2.manifest")
    if manifest.get("schema") != "lb-v2-snapshot/v1":
        raise bad_snapshot(
            "v2 schema must be lb-v2-snapshot/v1",
            field="schema",
            value=manifest.get("schema"),
        )
    snapshot_id = require_str(manifest.get("snapshot_id"), "v2.snapshot_id")
    clock = require_int(manifest.get("logical_clock_ms"), "v2.logical_clock_ms", minimum=0)
    floor = require_int(
        manifest.get("state_generation_floor"),
        "v2.state_generation_floor",
        minimum=0,
    )
    threshold = require_int(manifest.get("failure_threshold"), "v2.failure_threshold", minimum=1)
    success_threshold = require_int(
        manifest.get("success_threshold", 1),
        "v2.success_threshold",
        minimum=1,
    )
    priority = require_int(manifest.get("source_priority", 2), "v2.source_priority", minimum=0)
    shard_specs = _require_list(manifest.get("shards"), "v2.shards")
    if not shard_specs:
        raise bad_snapshot("v2.shards must not be empty", field="v2.shards")

    instances: dict[str, dict[str, Any]] = {}
    shards: list[dict[str, Any]] = []
    previous_generation = floor - 1
    previous_shard_max = floor - 1
    for index, raw_spec in enumerate(shard_specs):
        spec = _require_object(raw_spec, f"v2.shards[{index}]")
        relative_path = require_str(spec.get("path"), f"v2.shards[{index}].path")
        shard_path = safe_resolve(manifest_path.parent, relative_path)
        actual_hash = _check_declared_hash(
            shard_path,
            spec.get("sha256"),
            field_name=f"v2.shards[{index}].sha256",
        )
        shard = _require_object(load_json_file(shard_path), f"v2.shards[{index}]")
        if shard.get("schema") != "lb-v2-shard/v1":
            raise bad_snapshot(
                "v2 shard schema must be lb-v2-shard/v1",
                path=relative_path,
            )
        shard_id = require_str(shard.get("shard_id"), "v2.shard_id")
        generation_min = require_int(
            shard.get("generation_min"), "v2.shard.generation_min", minimum=0
        )
        generation_max = require_int(
            shard.get("generation_max"), "v2.shard.generation_max", minimum=0
        )
        if generation_min > generation_max:
            raise bad_snapshot(
                "shard generation_min exceeds generation_max",
                shard_id=shard_id,
            )
        if generation_min < floor:
            raise bad_snapshot(
                "shard generation_min is below manifest floor",
                shard_id=shard_id,
                generation_min=generation_min,
                generation_floor=floor,
            )
        if generation_min < previous_shard_max:
            raise bad_snapshot(
                "v2 shard generation ranges are not monotonic",
                shard_id=shard_id,
                previous_generation_max=previous_shard_max,
                generation_min=generation_min,
            )
        rows = _require_list(shard.get("instances"), "v2.shard.instances")
        local_generations: list[int] = []
        for row_index, raw_item in enumerate(rows):
            item = _require_object(raw_item, f"v2.shard.instances[{row_index}]")
            canonical_raw = require_str(
                item.get("canonical_instance_id"),
                f"v2.shard.instances[{row_index}].canonical_instance_id",
            )
            canonical = canonical_instance_id(
                canonical_raw, "v2.canonical_instance_id"
            )
            if canonical != canonical_raw:
                raise bad_snapshot(
                    "v2 canonical_instance_id is not canonical",
                    field="canonical_instance_id",
                    value=canonical_raw,
                    expected=canonical,
                )
            if canonical in instances:
                raise bad_snapshot(
                    "v2 contains duplicate canonical instance ID",
                    field="canonical_instance_id",
                    value=canonical,
                )
            generation = require_int(
                item.get("state_generation"),
                f"v2.shard.instances[{row_index}].state_generation",
                minimum=0,
            )
            if generation < floor:
                raise bad_snapshot(
                    "v2 state_generation is below manifest floor",
                    instance_id=canonical,
                    state_generation=generation,
                    generation_floor=floor,
                )
            if generation < previous_generation:
                raise bad_snapshot(
                    "v2 state generations are not monotonic",
                    instance_id=canonical,
                    previous_generation=previous_generation,
                    state_generation=generation,
                )
            if generation < generation_min or generation > generation_max:
                raise bad_snapshot(
                    "v2 state_generation is outside shard generation range",
                    instance_id=canonical,
                    state_generation=generation,
                    generation_min=generation_min,
                    generation_max=generation_max,
                )
            previous_generation = generation
            local_generations.append(generation)
            failures = require_int(
                item.get("consecutive_failures"),
                f"v2.shard.instances[{row_index}].consecutive_failures",
                minimum=0,
            )
            successes = require_int(
                item.get("consecutive_successes"),
                f"v2.shard.instances[{row_index}].consecutive_successes",
                minimum=0,
            )
            absolute_failure_ms = require_int(
                item.get("absolute_failure_ms"),
                f"v2.shard.instances[{row_index}].absolute_failure_ms",
            )
            weight = require_int(
                item.get("effective_weight"),
                f"v2.shard.instances[{row_index}].effective_weight",
                minimum=0,
            )
            tombstone = require_bool(
                item.get("tombstone"),
                f"v2.shard.instances[{row_index}].tombstone",
            )
            health = _require_enum(
                item.get("status"),
                f"v2.shard.instances[{row_index}].status",
                V2_STATUSES,
            )
            if tombstone and absolute_failure_ms > clock:
                raise bad_snapshot(
                    "tombstone absolute_failure_ms is later than logical clock",
                    instance_id=canonical,
                    absolute_failure_ms=absolute_failure_ms,
                    logical_clock_ms=clock,
                )
            if tombstone and weight != 0:
                raise bad_snapshot(
                    "tombstone effective_weight must be zero",
                    instance_id=canonical,
                    effective_weight=weight,
                )
            if tombstone and health != "unhealthy":
                raise bad_snapshot(
                    "tombstone status must be unhealthy",
                    instance_id=canonical,
                    status=health,
                )
            original_id = item.get("original_instance_id", canonical_raw)
            original_id = require_str(
                original_id,
                f"v2.shard.instances[{row_index}].original_instance_id",
            )
            instances[canonical] = {
                "canonical_instance_id": canonical,
                "original_instance_id": original_id,
                "absolute_failure_ms": absolute_failure_ms,
                "state_generation": generation,
                "consecutive_failures": failures,
                "consecutive_successes": successes,
                "effective_weight": weight,
                "tombstone": tombstone,
                "status": health,
                "shard_id": shard_id,
            }
        if len(rows) == 0:
            raise bad_snapshot("v2 shard must contain at least one instance", shard_id=shard_id)
        if min(local_generations) != generation_min or max(local_generations) != generation_max:
            raise bad_snapshot(
                "shard generation range does not match its records",
                shard_id=shard_id,
                declared=[generation_min, generation_max],
                actual=[min(local_generations), max(local_generations)],
            )
        previous_shard_max = generation_max
        shards.append(
            {
                "shard_id": shard_id,
                "path": relative_path,
                "sha256": actual_hash,
                "generation_min": generation_min,
                "generation_max": generation_max,
                "record_count": len(rows),
            }
        )

    instance_digest = sha256_json(sorted(instances))
    meta_doc = _parse_v2_meta(
        meta_path,
        expected_snapshot_id=snapshot_id,
        expected_version=package_version,
        expected_clock=clock,
        expected_floor=floor,
        expected_shards=shards,
        expected_instance_digest=instance_digest,
    )
    return {
        "format": "v2",
        "snapshot_id": snapshot_id,
        "logical_clock_ms": clock,
        "state_generation_floor": floor,
        "source_priority": priority,
        "failure_threshold": threshold,
        "success_threshold": success_threshold,
        "instances": instances,
        "shards": shards,
        "instance_set_sha256": instance_digest,
        "manifest_sha256": sha256_file(manifest_path),
        "meta": meta_doc,
    }


def parse_package(package_path: str | Path) -> dict[str, Any]:
    """Parse and fully validate a combined v1/v2 snapshot package in memory."""

    path = Path(package_path).expanduser().resolve()
    if not path.is_file():
        raise bad_snapshot("package file does not exist", path=str(path))
    package = _require_object(load_json_file(path), "package")
    if package.get("schema") != "lb-workbench-package/v1":
        raise bad_snapshot(
            "package schema must be lb-workbench-package/v1",
            field="schema",
            value=package.get("schema"),
        )
    package_version = require_str(package.get("package_version"), "package.package_version")
    package_clock = require_int(
        package.get("logical_clock_ms"), "package.logical_clock_ms", minimum=0
    )
    package_floor = require_int(
        package.get("state_generation_floor"),
        "package.state_generation_floor",
        minimum=0,
    )
    root = path.parent

    v1_spec = _require_object(package.get("v1"), "package.v1")
    v1_relative = require_str(v1_spec.get("path"), "package.v1.path")
    v1_path = safe_resolve(root, v1_relative)
    v1_file_hash = _check_declared_hash(
        v1_path, v1_spec.get("sha256"), field_name="package.v1.sha256"
    )
    v1 = _parse_v1(v1_path)

    v2_spec = _require_object(package.get("v2"), "package.v2")
    v2_manifest_relative = require_str(v2_spec.get("manifest"), "package.v2.manifest")
    v2_manifest_path = safe_resolve(root, v2_manifest_relative)
    v2_manifest_hash = _check_declared_hash(
        v2_manifest_path,
        v2_spec.get("manifest_sha256"),
        field_name="package.v2.manifest_sha256",
    )
    meta_relative = require_str(v2_spec.get("meta_db"), "package.v2.meta_db")
    meta_path = safe_resolve(root, meta_relative)
    meta_file_hash = _check_declared_hash(
        meta_path,
        v2_spec.get("meta_sha256"),
        field_name="package.v2.meta_sha256",
    )
    v2 = _parse_v2(
        v2_manifest_path,
        meta_path,
        package_version=package_version,
    )
    if v2["state_generation_floor"] != package_floor:
        raise bad_snapshot(
            "package state_generation_floor does not match v2 manifest",
            field="state_generation_floor",
            package_value=package_floor,
            v2_value=v2["state_generation_floor"],
        )
    if package_clock < max(v1["logical_clock_ms"], v2["logical_clock_ms"]):
        raise bad_snapshot(
            "package logical_clock_ms is older than a source snapshot",
            field="logical_clock_ms",
            package_value=package_clock,
            v1_value=v1["logical_clock_ms"],
            v2_value=v2["logical_clock_ms"],
        )

    all_ids = sorted(set(v1["instances"]) | set(v2["instances"]))
    instance_set_sha256 = sha256_json(all_ids)
    declared_instance_digest = package.get("instance_set_sha256")
    if declared_instance_digest is not None and declared_instance_digest != instance_set_sha256:
        raise hash_mismatch(
            "package instance_set_sha256 does not match merged instance set",
            field="package.instance_set_sha256",
            expected=declared_instance_digest,
            actual=instance_set_sha256,
        )

    source_hashes = {
        v1_relative: v1_file_hash,
        v2_manifest_relative: v2_manifest_hash,
        meta_relative: meta_file_hash,
    }
    for shard in v2["shards"]:
        source_hashes[f"{v2_manifest_relative.rsplit('/', 1)[0]}/{shard['path']}"] = shard[
            "sha256"
        ]
    package_digest = sha256_json(
        {
            "package": {
                "package_version": package_version,
                "logical_clock_ms": package_clock,
                "state_generation_floor": package_floor,
            },
            "v1": v1,
            "v2": v2,
            "instance_set_sha256": instance_set_sha256,
            "source_hashes": source_hashes,
        }
    )
    return {
        "package_version": package_version,
        "package_digest": package_digest,
        "logical_clock_ms": package_clock,
        "state_generation_floor": package_floor,
        "instance_set_sha256": instance_set_sha256,
        "instance_ids": all_ids,
        "source_hashes": source_hashes,
        "source_hashes_sha256": sha256_json(source_hashes),
        "package_sha256": sha256_file(path),
        "v1": v1,
        "v2": v2,
        "package_path": str(path),
        "package_file": canonical_json(package),
    }
