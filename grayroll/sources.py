from __future__ import annotations

import copy
import itertools
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from .errors import GrayrollError
from .model import CandidateBatch, LoadedPackage
from .util import (
    canonical_bytes,
    load_json_file,
    natural_key,
    require_dict,
    require_int,
    require_keys,
    require_list,
    require_number,
    require_str,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
    tier_digest,
)

OBSERVATION_OPS = {"<", "<=", ">", ">=", "=="}


def _validate_tiers(value: Any, field: str, code: str) -> tuple[float, ...]:
    raw = require_list(value, field, code)
    if not raw:
        raise GrayrollError(code, f"{field} must not be empty")
    ratios: list[float] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            item = item.get("ratio")
        ratio = require_number(item, f"{field}[{index}]", code)
        if ratio < 0.0 or ratio > 1.0:
            raise GrayrollError(code, f"{field}[{index}] must be within [0, 1]")
        ratios.append(ratio)
    array = np.asarray(ratios, dtype=np.float64)
    if not bool(np.all(np.diff(array) > 0.0)):
        raise GrayrollError(code, f"{field} must be strictly increasing")
    if not float(array[-1]) == 1.0:
        raise GrayrollError(code, f"{field} must end at 1.0")
    return tuple(float(item) for item in array)


def _validate_markers(
    value: Any, tiers: tuple[float, ...], field: str, code: str
) -> tuple[float, ...]:
    raw = require_list(value, field, code)
    tier_array = np.asarray(tiers, dtype=np.float64)
    markers: list[float] = []
    for index, item in enumerate(raw):
        marker = require_number(item, f"{field}[{index}]", code)
        matches = np.flatnonzero(np.isclose(tier_array, marker, rtol=0.0, atol=1e-12))
        if matches.size == 0:
            raise GrayrollError(
                code, f"{field}[{index}]={marker!r} is not present in tiers"
            )
        normalized = float(tier_array[int(matches[0])])
        if normalized in markers:
            raise GrayrollError(code, f"{field} contains duplicate marker {normalized}")
        markers.append(normalized)
    return tuple(markers)


def validate_criteria(value: Any, field: str, code: str) -> dict[str, Any]:
    criteria = require_dict(value, field, code)
    require_keys(criteria, ("min_samples", "rules"), field, code)
    min_samples = require_int(criteria["min_samples"], f"{field}.min_samples", code)
    rules_raw = require_list(criteria["rules"], f"{field}.rules", code)
    if not rules_raw:
        raise GrayrollError(code, f"{field}.rules must not be empty")
    rules: list[dict[str, Any]] = []
    for index, item in enumerate(rules_raw):
        rule = require_dict(item, f"{field}.rules[{index}]", code)
        require_keys(rule, ("metric", "op", "value"), f"{field}.rules[{index}]", code)
        metric = require_str(rule["metric"], f"{field}.rules[{index}].metric", code)
        op = require_str(rule["op"], f"{field}.rules[{index}].op", code)
        if op not in OBSERVATION_OPS:
            raise GrayrollError(
                code,
                f"{field}.rules[{index}].op must be one of {sorted(OBSERVATION_OPS)}",
            )
        threshold = require_number(rule["value"], f"{field}.rules[{index}].value", code)
        rules.append({"metric": metric, "op": op, "value": threshold})
    unknown_metric = criteria.get("unknown_metric", "CONTINUE")
    if unknown_metric not in ("CONTINUE", "FAIL"):
        raise GrayrollError(code, f"{field}.unknown_metric must be CONTINUE or FAIL")
    return {
        "min_samples": min_samples,
        "rules": rules,
        "unknown_metric": unknown_metric,
    }


def validate_defaults(value: Any) -> dict[str, Any]:
    code = "E_BAD_DEFAULTS"
    defaults = require_dict(value, "defaults", code)
    require_keys(
        defaults,
        ("clock_tolerance_ms", "rollback_step_ms", "source_priority", "v1_defaults"),
        "defaults",
        code,
    )
    priority = require_dict(defaults["source_priority"], "source_priority", code)
    priorities = {
        "v1": require_int(priority.get("v1"), "source_priority.v1", code, minimum=0),
        "v2": require_int(priority.get("v2"), "source_priority.v2", code, minimum=0),
    }
    v1_defaults = require_dict(defaults["v1_defaults"], "v1_defaults", code)
    require_keys(
        v1_defaults,
        ("criteria", "generation", "rollback_window"),
        "v1_defaults",
        code,
    )
    rollback_window = require_dict(
        v1_defaults["rollback_window"], "v1_defaults.rollback_window", code
    )
    require_keys(
        rollback_window,
        ("duration_ms", "start_offset_ms"),
        "v1_defaults.rollback_window",
        code,
    )
    normalized = {
        "clock_tolerance_ms": require_int(
            defaults["clock_tolerance_ms"], "clock_tolerance_ms", code
        ),
        "rollback_step_ms": require_int(
            defaults["rollback_step_ms"], "rollback_step_ms", code, minimum=1
        ),
        "source_priority": priorities,
        "v1_defaults": {
            "criteria": validate_criteria(
                v1_defaults["criteria"], "v1_defaults.criteria", code
            ),
            "generation": require_int(
                v1_defaults["generation"], "v1_defaults.generation", code, minimum=1
            ),
            "rollback_window": {
                "duration_ms": require_int(
                    rollback_window["duration_ms"],
                    "v1_defaults.rollback_window.duration_ms",
                    code,
                ),
                "start_offset_ms": require_int(
                    rollback_window["start_offset_ms"],
                    "v1_defaults.rollback_window.start_offset_ms",
                    code,
                ),
            },
        },
    }
    return normalized


def load_defaults(path: Path) -> dict[str, Any]:
    return validate_defaults(load_json_file(path, "E_BAD_DEFAULTS"))


def _v1_candidates(
    document: Any, defaults: dict[str, Any], source_path: str
) -> list[CandidateBatch]:
    code = "E_BAD_RHYTHM"
    doc = require_dict(document, "v1 rhythm", code)
    require_keys(
        doc, ("batches", "format", "logical_clock_ms", "service"), "v1 rhythm", code
    )
    if doc["format"] != "rhythm-v1":
        raise GrayrollError(code, "v1 format must be rhythm-v1")
    service = require_str(doc["service"], "v1 service", code)
    logical_clock = require_int(doc["logical_clock_ms"], "v1 logical_clock_ms", code)
    batches = require_dict(doc["batches"], "v1 batches", code)
    if not batches:
        raise GrayrollError(code, "v1 batches must not be empty")

    parsed: list[
        tuple[str, dict[str, Any], tuple[float, ...], tuple[float, ...], int]
    ] = []
    seen_ids: set[str] = set()
    for batch_id, raw_batch in batches.items():
        if not isinstance(batch_id, str) or not batch_id:
            raise GrayrollError(code, "v1 batch id must be a non-empty string")
        if batch_id in seen_ids:
            raise GrayrollError(code, f"duplicate v1 batch id: {batch_id}")
        seen_ids.add(batch_id)
        batch = require_dict(raw_batch, f"v1 batches.{batch_id}", code)
        require_keys(
            batch,
            ("min_interval_seconds", "observation_points", "tiers"),
            f"v1 batches.{batch_id}",
            code,
        )
        tiers = _validate_tiers(batch["tiers"], f"v1 batches.{batch_id}.tiers", code)
        markers = _validate_markers(
            batch["observation_points"],
            tiers,
            f"v1 batches.{batch_id}.observation_points",
            code,
        )
        interval = require_int(
            batch["min_interval_seconds"],
            f"v1 batches.{batch_id}.min_interval_seconds",
            code,
        )
        parsed.append((batch_id, batch, tiers, markers, interval))

    parsed.sort(key=lambda item: natural_key(item[0]))
    offset_ms = 0
    result: list[CandidateBatch] = []
    v1_defaults = defaults["v1_defaults"]
    for index, (batch_id, raw_batch, tiers, markers, interval) in enumerate(parsed):
        relative_seconds = 0 if index == 0 else interval
        delta_ms = int(relative_seconds * 1000)
        offset_ms += delta_ms
        earliest = logical_clock + offset_ms
        rollback_start = earliest + int(
            v1_defaults["rollback_window"]["start_offset_ms"]
        )
        rollback_end = rollback_start + int(
            v1_defaults["rollback_window"]["duration_ms"]
        )
        migration = {
            "compatibility_defaults": {
                "criteria": "v1_defaults.criteria",
                "dependencies": "empty_dependency_set",
                "generation": "v1_defaults.generation",
                "rollback_window": "v1_defaults.rollback_window",
            },
            "earliest_rollout": {
                "anchor_logical_clock_ms": logical_clock,
                "mode": "relative_seconds_to_absolute_ms",
                "relative_offset_ms": offset_ms,
                "relative_seconds": relative_seconds,
                "rounding_direction": "ceil",
                "scale_ms_per_second": 1000,
            },
            "rollback_window": {
                "duration_ms": int(v1_defaults["rollback_window"]["duration_ms"]),
                "end_ms": rollback_end,
                "start_ms": rollback_start,
                "start_offset_ms": int(
                    v1_defaults["rollback_window"]["start_offset_ms"]
                ),
            },
        }
        result.append(
            CandidateBatch(
                service=service,
                batch_id=batch_id,
                source_version="v1",
                source_priority=int(defaults["source_priority"]["v1"]),
                logical_clock_ms=logical_clock,
                generation=int(v1_defaults["generation"]),
                tiers=tiers,
                observation_markers=markers,
                earliest_rollout_ms=earliest,
                criteria=copy.deepcopy(v1_defaults["criteria"]),
                rollback_start_ms=rollback_start,
                rollback_end_ms=rollback_end,
                rollback_step_ms=int(defaults["rollback_step_ms"]),
                depends_on=(),
                raw={
                    "batch": copy.deepcopy(raw_batch),
                    "format": "rhythm-v1",
                    "logical_clock_ms": logical_clock,
                    "source_path": source_path,
                },
                migration=migration,
            )
        )
    return result


def _read_v2_meta(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise GrayrollError(
            "E_BAD_SHARD", f"v2 metadata database not found: {path.name}"
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        meta = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM meta")
        }
        shards = {
            row["path"]: dict(row) for row in connection.execute("SELECT * FROM shards")
        }
    except sqlite3.Error as exc:
        raise GrayrollError(
            "E_BAD_SHARD", f"cannot read v2 metadata database {path.name}: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()
    return {"meta": meta, "shards": shards}


def _extract_v2_shard(
    document: Any,
    *,
    meta_row: dict[str, Any],
    shard_path: str,
    defaults: dict[str, Any],
) -> CandidateBatch:
    code = "E_BAD_SHARD"
    doc = require_dict(document, f"v2 shard {shard_path}", code)
    required = (
        "absolute_rollout_ms",
        "batch_id",
        "criteria",
        "depends_on",
        "format",
        "generation",
        "logical_clock_ms",
        "observation_markers",
        "rollback_window",
        "service",
        "tiers",
    )
    require_keys(doc, required, f"v2 shard {shard_path}", code)
    if doc["format"] != "rhythm-v2":
        raise GrayrollError(code, f"v2 shard {shard_path} format must be rhythm-v2")
    service = require_str(doc["service"], "v2 service", code)
    batch_id = require_str(doc["batch_id"], "v2 batch_id", code)
    generation = require_int(doc["generation"], "v2 generation", code, minimum=1)
    logical_clock = require_int(doc["logical_clock_ms"], "v2 logical_clock_ms", code)
    absolute_rollout = require_int(
        doc["absolute_rollout_ms"], "v2 absolute_rollout_ms", code
    )
    tiers = _validate_tiers(doc["tiers"], f"v2 shard {shard_path}.tiers", code)
    markers = _validate_markers(
        doc["observation_markers"],
        tiers,
        f"v2 shard {shard_path}.observation_markers",
        code,
    )
    criteria = validate_criteria(
        doc["criteria"], f"v2 shard {shard_path}.criteria", code
    )
    dependency_raw = require_list(doc["depends_on"], "v2 depends_on", code)
    depends_on: list[str] = []
    for item in dependency_raw:
        dependency = require_str(item, "v2 depends_on[]", code)
        if dependency == batch_id:
            raise GrayrollError(code, f"v2 batch {batch_id} cannot depend on itself")
        if dependency not in depends_on:
            depends_on.append(dependency)
    rollback = require_dict(doc["rollback_window"], "v2 rollback_window", code)
    if "start_ms" in rollback and "end_ms" in rollback:
        rollback_start = require_int(
            rollback["start_ms"], "rollback_window.start_ms", code
        )
        rollback_end = require_int(rollback["end_ms"], "rollback_window.end_ms", code)
    else:
        rollback_start = require_int(
            rollback.get("start"), "rollback_window.start", code
        )
        rollback_end = require_int(rollback.get("end"), "rollback_window.end", code)
    if rollback_start > rollback_end:
        raise GrayrollError(
            code, f"rollback window start {rollback_start} exceeds end {rollback_end}"
        )
    step_raw = doc.get("rollback_step_ms", defaults["rollback_step_ms"])
    step_ms = require_int(step_raw, "rollback_step_ms", code, minimum=1)

    expected = {
        "service": service,
        "batch_id": batch_id,
        "generation": generation,
        "logical_clock_ms": logical_clock,
        "absolute_rollout_ms": absolute_rollout,
    }
    for field, expected_value in expected.items():
        if field in meta_row and meta_row[field] != expected_value:
            raise GrayrollError(
                code,
                f"v2 metadata mismatch for {field} in {shard_path}",
                {"expected": expected_value, "metadata": meta_row.get(field)},
            )
    metadata_consistency = {
        "criteria_json": criteria,
        "depends_json": depends_on,
        "observation_markers_json": list(markers),
        "tiers_json": list(tiers),
    }
    for field, expected_value in metadata_consistency.items():
        if field not in meta_row:
            continue
        actual_value = json.loads(meta_row[field])
        if field == "criteria_json":
            actual_value = validate_criteria(actual_value, field, code)
        if actual_value != expected_value:
            raise GrayrollError(
                code,
                f"v2 metadata mismatch for {field} in {shard_path}",
                {"expected": expected_value, "metadata": actual_value},
            )
    for field, expected_value in (
        ("rollback_start_ms", rollback_start),
        ("rollback_end_ms", rollback_end),
    ):
        if field in meta_row and int(meta_row[field]) != expected_value:
            raise GrayrollError(
                code,
                f"v2 metadata mismatch for {field} in {shard_path}",
                {"expected": expected_value, "metadata": meta_row[field]},
            )
    return CandidateBatch(
        service=service,
        batch_id=batch_id,
        source_version="v2",
        source_priority=int(defaults["source_priority"]["v2"]),
        logical_clock_ms=logical_clock,
        generation=generation,
        tiers=tiers,
        observation_markers=markers,
        earliest_rollout_ms=absolute_rollout,
        criteria=criteria,
        rollback_start_ms=rollback_start,
        rollback_end_ms=rollback_end,
        rollback_step_ms=step_ms,
        depends_on=tuple(depends_on),
        raw={
            "format": "rhythm-v2",
            "shard": copy.deepcopy(doc),
            "shard_path": shard_path,
        },
        migration={
            "earliest_rollout": {
                "mode": "absolute_ms",
                "rounding_direction": "none",
                "source_value_ms": absolute_rollout,
            }
        },
    )


def _check_dependency_cycles(batches: list[CandidateBatch], code: str) -> None:
    by_service: dict[str, dict[str, CandidateBatch]] = {}
    for batch in batches:
        if batch.source_version != "v2":
            continue
        by_service.setdefault(batch.service, {})[batch.batch_id] = batch
    for service, group in by_service.items():
        for batch in group.values():
            for dependency in batch.depends_on:
                if dependency not in group:
                    raise GrayrollError(
                        code,
                        f"v2 batch {batch.batch_id} depends on undeclared batch {dependency}",
                    )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(
            target_id: str,
            *,
            visiting_set: set[str],
            visited_set: set[str],
            group_map: dict[str, CandidateBatch],
            service_name: str,
        ) -> None:
            if target_id in visiting_set:
                raise GrayrollError(
                    code, f"dependency cycle detected for service {service_name}"
                )
            if target_id in visited_set:
                return
            visiting_set.add(target_id)
            for dependency in group_map[target_id].depends_on:
                visit(
                    dependency,
                    visiting_set=visiting_set,
                    visited_set=visited_set,
                    group_map=group_map,
                    service_name=service_name,
                )
            visiting_set.remove(target_id)
            visited_set.add(target_id)

        for batch_id in sorted(group, key=natural_key):
            visit(
                batch_id,
                visiting_set=visiting,
                visited_set=visited,
                group_map=group,
                service_name=service,
            )


def load_package(manifest_path: Path, defaults_path: Path) -> LoadedPackage:
    defaults = load_defaults(defaults_path)
    manifest = require_dict(
        load_json_file(manifest_path, "E_BAD_MANIFEST"), "manifest", "E_BAD_MANIFEST"
    )
    code = "E_BAD_MANIFEST"
    require_keys(
        manifest,
        (
            "generation_floor",
            "package_version",
            "schema",
            "service",
            "shards",
            "tier_digest",
            "v1",
        ),
        "manifest",
        code,
    )
    if manifest["schema"] != "rhythm-package-manifest-v1":
        raise GrayrollError(code, "unsupported manifest schema")
    version = require_int(
        manifest["package_version"], "package_version", code, minimum=1
    )
    generation_floor = require_int(
        manifest["generation_floor"], "generation_floor", code
    )
    service = require_str(manifest["service"], "manifest service", code)
    manifest_tier_digest = require_str(
        manifest["tier_digest"], "manifest tier_digest", code
    )
    base = manifest_path.parent

    v1_entry = require_dict(manifest["v1"], "manifest.v1", code)
    require_keys(v1_entry, ("path", "sha256"), "manifest.v1", code)
    v1_path = safe_relative_path(base, v1_entry["path"], code)
    if not v1_path.is_file():
        raise GrayrollError(code, f"v1 rhythm file not found: {v1_entry['path']}")
    v1_hash = sha256_file(v1_path)
    if v1_hash != v1_entry["sha256"]:
        raise GrayrollError(
            "E_BAD_SHARD",
            "v1 rhythm file hash mismatch",
            {"expected": v1_entry["sha256"], "actual": v1_hash},
        )
    v1_document = load_json_file(v1_path, "E_BAD_RHYTHM")
    if v1_document.get("service") != service:
        raise GrayrollError(code, "v1 service does not match manifest service")
    v1_candidates = _v1_candidates(v1_document, defaults, v1_entry["path"])

    shard_entries = require_list(manifest["shards"], "manifest.shards", code)
    if not shard_entries:
        raise GrayrollError(code, "manifest.shards must not be empty")
    normalized_shards: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for index, raw_entry in enumerate(shard_entries):
        entry = require_dict(raw_entry, f"manifest.shards[{index}]", code)
        require_keys(entry, ("path", "sha256"), f"manifest.shards[{index}]", code)
        relative = require_str(entry["path"], f"manifest.shards[{index}].path", code)
        if relative in seen_paths:
            raise GrayrollError(code, f"duplicate shard path: {relative}")
        seen_paths.add(relative)
        normalized_shards.append(
            {
                "path": relative,
                "sha256": require_str(entry["sha256"], "shard sha256", code),
            }
        )

    meta_entry = manifest.get("v2_meta")
    if not isinstance(meta_entry, dict):
        meta_entry = manifest.get("v2", {})
    meta_relative = meta_entry.get("meta_path", "v2/meta.db")
    meta_path = safe_relative_path(base, meta_relative, code)
    meta_hash = sha256_file(meta_path) if meta_path.is_file() else ""
    expected_meta_hash = meta_entry.get("meta_sha256")
    if expected_meta_hash and meta_hash != expected_meta_hash:
        raise GrayrollError(
            "E_BAD_SHARD",
            "v2 metadata database hash mismatch",
            {"expected": expected_meta_hash, "actual": meta_hash},
        )
    v2_meta = _read_v2_meta(meta_path)
    meta_values = v2_meta["meta"]
    expected_meta = {
        "package_version": str(version),
        "generation_floor": str(generation_floor),
        "service": service,
        "tier_digest": manifest_tier_digest,
    }
    for key, expected in expected_meta.items():
        if meta_values.get(key) != expected:
            raise GrayrollError(
                "E_BAD_SHARD",
                f"v2 metadata value mismatch for {key}",
                {"expected": expected, "actual": meta_values.get(key)},
            )

    manifest_paths = {entry["path"] for entry in normalized_shards}
    if set(v2_meta["shards"]) != manifest_paths:
        raise GrayrollError(
            "E_BAD_SHARD",
            "v2 metadata shard rows do not match manifest shards",
            {
                "manifest_only": sorted(manifest_paths - set(v2_meta["shards"])),
                "metadata_only": sorted(set(v2_meta["shards"]) - manifest_paths),
            },
        )

    v2_candidates: list[CandidateBatch] = []
    seen_batch_keys: set[tuple[str, str]] = set()
    for entry in normalized_shards:
        path = safe_relative_path(base, entry["path"], "E_BAD_SHARD")
        if not path.is_file():
            raise GrayrollError("E_BAD_SHARD", f"shard not found: {entry['path']}")
        actual_hash = sha256_file(path)
        if actual_hash != entry["sha256"]:
            raise GrayrollError(
                "E_BAD_SHARD",
                f"shard hash mismatch: {entry['path']}",
                {"expected": entry["sha256"], "actual": actual_hash},
            )
        metadata_row = v2_meta["shards"][entry["path"]]
        if metadata_row["sha256"] != actual_hash:
            raise GrayrollError(
                "E_BAD_SHARD",
                f"v2 metadata hash mismatch: {entry['path']}",
            )
        document = load_json_file(path, "E_BAD_SHARD")
        candidate = _extract_v2_shard(
            document,
            meta_row=metadata_row,
            shard_path=entry["path"],
            defaults=defaults,
        )
        key = (candidate.service, candidate.batch_id)
        if key in seen_batch_keys:
            raise GrayrollError(
                "E_BAD_SHARD",
                f"duplicate v2 batch key: {candidate.service}/{candidate.batch_id}",
            )
        seen_batch_keys.add(key)
        v2_candidates.append(candidate)

    actual_tier_digest = tier_digest(
        (batch.service, batch.batch_id, batch.tiers) for batch in v2_candidates
    )
    if actual_tier_digest != manifest_tier_digest:
        raise GrayrollError(
            "E_BAD_SHARD",
            "v2 tier digest mismatch",
            {"expected": manifest_tier_digest, "actual": actual_tier_digest},
        )
    _check_dependency_cycles(v2_candidates, "E_BAD_SHARD")

    by_service: dict[str, list[CandidateBatch]] = {}
    for batch in v2_candidates:
        by_service.setdefault(batch.service, []).append(batch)
    for service_name, batches in by_service.items():
        ordered = sorted(batches, key=lambda item: natural_key(item.batch_id))
        generations = [batch.generation for batch in ordered]
        if any(later < earlier for earlier, later in itertools.pairwise(generations)):
            raise GrayrollError(
                "E_BAD_SHARD",
                f"v2 batch generation is not monotonic for service {service_name}",
                {
                    "batch_ids": [batch.batch_id for batch in ordered],
                    "generations": generations,
                },
            )
    for candidate in v2_candidates:
        if candidate.service != service:
            raise GrayrollError(
                "E_BAD_SHARD",
                "v2 shard service does not match manifest service",
                {"manifest_service": service, "shard_service": candidate.service},
            )

    all_candidates = tuple(v1_candidates + v2_candidates)
    identity = {
        "generation_floor": generation_floor,
        "manifest_version": version,
        "service": service,
        "shards": normalized_shards,
        "tier_digest": manifest_tier_digest,
        "v1_sha256": v1_hash,
    }
    package_id = f"pkg-v{version}-{sha256_bytes(canonical_bytes(identity))[:20]}"
    return LoadedPackage(
        manifest=manifest,
        manifest_hash=sha256_bytes(canonical_bytes(manifest)),
        defaults=defaults,
        candidates=all_candidates,
        package_id=package_id,
    )
