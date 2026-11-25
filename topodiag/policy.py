"""Parsing, validation, merge, and incremental overlay of policy snapshots."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError, require
from .model import (
    PolicyCandidate,
    TopologyInvalidation,
    V1Policy,
    V2PolicyPackage,
    V2Tombstone,
)
from .storage import read_merge_decision, write_merge_decision
from .util import (
    canonical_json,
    compare_semver,
    is_nonnegative_int,
    is_positive_int,
    parse_semver,
    pretty_json,
    read_json,
    sha256_bytes,
    sha256_json,
    validate_labels,
)


def _policy_error(
    code: str,
    message: str,
    *,
    template_name: str | None = None,
    version: str | int | None = None,
    shard_index: int | None = None,
    request_seq: int | None = None,
    **details: Any,
) -> BusinessError:
    return BusinessError(
        code,
        message,
        template_name=template_name,
        version=version,
        shard_index=shard_index,
        request_seq=request_seq,
        **details,
    )


def _entry_fields(
    name: str,
    spec: Any,
    *,
    version: str | int | None,
    shard_index: int | None = None,
) -> tuple[int, tuple[str, ...], tuple[str, ...], int | None]:
    if not isinstance(spec, dict):
        raise _policy_error(
            "INVALID_POLICY_ENTRY",
            f"Policy entry {name!r} must be an object",
            template_name=name,
            version=version,
            shard_index=shard_index,
        )
    entry_version = spec.get("version")
    require(
        is_positive_int(entry_version),
        "INVALID_TEMPLATE_VERSION",
        f"Policy entry {name!r} version must be a positive integer",
        template_name=name,
        version=entry_version,
        shard_index=shard_index,
    )
    dependencies_raw = spec.get("dependencies")
    if not isinstance(dependencies_raw, list):
        raise _policy_error(
            "INVALID_POLICY_ENTRY",
            f"Policy entry {name!r} dependencies must be an array",
            template_name=name,
            version=entry_version,
            shard_index=shard_index,
        )
    dependencies: list[str] = []
    for dependency in dependencies_raw:
        if not isinstance(dependency, str):
            raise _policy_error(
                "INVALID_POLICY_ENTRY",
                f"Policy entry {name!r} has a non-string dependency",
                template_name=name,
                version=entry_version,
                shard_index=shard_index,
            )
        dependencies.append(dependency)
    if len(set(dependencies)) != len(dependencies):
        raise _policy_error(
            "INVALID_POLICY_ENTRY",
            f"Policy entry {name!r} has duplicate dependencies",
            template_name=name,
            version=entry_version,
            shard_index=shard_index,
        )
    labels = validate_labels(
        spec.get("labels"),
        template_name=name,
        version=entry_version,
        shard_index=shard_index,
    )
    logical_clock = spec.get("logical_clock")
    if logical_clock is not None:
        require(
            is_nonnegative_int(logical_clock),
            "INVALID_LOGICAL_CLOCK",
            f"Policy entry {name!r} logical_clock must be non-negative",
            template_name=name,
            version=entry_version,
            shard_index=shard_index,
            logical_clock=logical_clock,
        )
    return entry_version, tuple(sorted(dependencies)), labels, logical_clock


def load_v1_policy(path: Path) -> V1Policy:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise _policy_error(
            "INVALID_V1_POLICY",
            "v1 policy snapshot must be a JSON object",
        )
    require(
        payload.get("format") == "orchestration-policy/v1",
        "INVALID_V1_POLICY",
        "Unsupported v1 policy format",
        actual_format=payload.get("format"),
    )
    policy_key = payload.get("policy_key")
    require(
        isinstance(policy_key, str) and bool(policy_key),
        "INVALID_V1_POLICY",
        "v1 policy_key must be a non-empty string",
    )
    manifest_version = payload.get("manifest_version")
    parse_semver(manifest_version, field="v1 manifest_version")
    logical_clock = payload.get("logical_clock")
    require(
        is_nonnegative_int(logical_clock),
        "INVALID_LOGICAL_CLOCK",
        "v1 policy logical_clock must be non-negative",
        version=manifest_version,
        logical_clock=logical_clock,
    )
    relative_seconds = payload.get("relative_effective_seconds")
    require(
        is_nonnegative_int(relative_seconds),
        "INVALID_V1_RELATIVE_TIME",
        "v1 relative_effective_seconds must be a non-negative integer",
        version=manifest_version,
        relative_effective_seconds=relative_seconds,
    )
    require(
        payload.get("no_topology_constraints") is True,
        "INVALID_V1_TOPOLOGY_DECLARATION",
        "v1 policy must explicitly declare no topology constraints",
        version=manifest_version,
        no_topology_constraints=payload.get("no_topology_constraints"),
    )
    generation = payload.get("policy_generation", 1)
    require(
        is_positive_int(generation),
        "INVALID_POLICY_GENERATION",
        "v1 policy_generation must be a positive integer",
        version=manifest_version,
        policy_generation=generation,
    )
    dependencies = payload.get("template_dependencies")
    versions = payload.get("template_versions")
    labels_by_name = payload.get("template_labels")
    if not isinstance(dependencies, dict) or not isinstance(versions, dict):
        raise _policy_error(
            "INVALID_V1_POLICY",
            "v1 template_dependencies and template_versions must be objects",
            version=manifest_version,
        )
    if not isinstance(labels_by_name, dict):
        raise _policy_error(
            "INVALID_V1_POLICY",
            "v1 template_labels must be an object",
            version=manifest_version,
        )
    names = set(dependencies) | set(versions) | set(labels_by_name)
    entries_raw = payload.get("template_entries", {})
    if not isinstance(entries_raw, dict):
        raise _policy_error(
            "INVALID_V1_POLICY",
            "v1 template_entries must be an object",
            version=manifest_version,
        )
    names |= set(entries_raw)
    entries: dict[str, dict[str, Any]] = {}
    for name in sorted(names):
        base = {
            "version": versions.get(name),
            "dependencies": dependencies.get(name),
            "labels": labels_by_name.get(name),
            "logical_clock": logical_clock,
            "policy_generation": generation,
        }
        override = entries_raw.get(name, {})
        if not isinstance(override, dict):
            raise _policy_error(
                "INVALID_POLICY_ENTRY",
                f"v1 template_entries[{name!r}] must be an object",
                template_name=name,
                version=manifest_version,
            )
        base.update(override)
        entry_version, entry_dependencies, entry_labels, entry_clock = _entry_fields(
            name,
            base,
            version=manifest_version,
        )
        entry_generation = base.get("policy_generation", generation)
        require(
            is_positive_int(entry_generation),
            "INVALID_POLICY_GENERATION",
            f"v1 entry {name!r} policy_generation must be positive",
            template_name=name,
            version=entry_version,
            policy_generation=entry_generation,
        )
        entries[name] = {
            "version": entry_version,
            "dependencies": entry_dependencies,
            "labels": entry_labels,
            "logical_clock": entry_clock if entry_clock is not None else logical_clock,
            "policy_generation": entry_generation,
        }
    _validate_graph(
        {name: entry["dependencies"] for name, entry in entries.items()},
        source="v1 policy",
        version=manifest_version,
        allow_missing=True,
    )
    return V1Policy(
        policy_key=policy_key,
        policy_generation=generation,
        manifest_version=manifest_version,
        logical_clock=logical_clock,
        relative_effective_seconds=relative_seconds,
        no_topology_constraints=True,
        entries=entries,
    )


def _read_metadata_db(
    metadata_path: Path,
) -> tuple[dict[str, Any], list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
    if not metadata_path.is_file():
        raise _policy_error(
            "V2_METADATA_MISSING",
            f"v2 SQLite metadata database is missing: {metadata_path}",
        )
    connection = sqlite3.connect(str(metadata_path))
    connection.row_factory = sqlite3.Row
    try:
        try:
            package_rows = connection.execute(
                "SELECT * FROM package_info"
            ).fetchall()
            shard_rows = connection.execute(
                """
                SELECT * FROM shard_metadata
                ORDER BY shard_index
                """
            ).fetchall()
            tombstones = connection.execute(
                """
                SELECT * FROM closure_tombstones
                ORDER BY template_name
                """
            ).fetchall()
            invalidations = connection.execute(
                """
                SELECT * FROM topology_invalidations
                ORDER BY invalidation_id
                """
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise _policy_error(
                "V2_METADATA_INVALID",
                f"v2 metadata database is invalid: {metadata_path}",
                parse_error=str(exc),
            ) from exc
    finally:
        connection.close()
    if len(package_rows) != 1:
        raise _policy_error(
            "V2_METADATA_INVALID",
            "v2 metadata must contain exactly one package_info row",
            row_count=len(package_rows),
        )
    return dict(package_rows[0]), shard_rows, tombstones, invalidations


def load_v2_package(path: Path) -> V2PolicyPackage:
    package_dir = path if path.is_dir() else path.parent
    manifest_path = package_dir / "manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise _policy_error(
            "INVALID_V2_PACKAGE",
            "v2 package manifest must be a JSON object",
            version=manifest.get("manifest_version") if isinstance(manifest, dict) else None,
        )
    require(
        manifest.get("format") == "orchestration-policy-package/v2",
        "INVALID_V2_PACKAGE",
        "Unsupported v2 package manifest format",
        version=manifest.get("manifest_version"),
        actual_format=manifest.get("format"),
    )
    metadata_name = manifest.get("metadata_file", "metadata.sqlite")
    if not isinstance(metadata_name, str) or "/" in metadata_name or "\\" in metadata_name:
        raise _policy_error(
            "INVALID_V2_PACKAGE",
            "v2 metadata_file must be a simple filename",
            version=manifest.get("manifest_version"),
        )
    metadata, shard_rows, tombstone_rows, invalidation_rows = _read_metadata_db(
        package_dir / metadata_name
    )
    manifest_version = metadata.get("manifest_version")
    parse_semver(manifest_version, field="v2 manifest_version")
    if manifest.get("manifest_version") != manifest_version:
        raise _policy_error(
            "V2_METADATA_MISMATCH",
            "v2 manifest and SQLite metadata disagree on manifest_version",
            version=manifest_version,
            manifest_value=manifest.get("manifest_version"),
            metadata_value=manifest_version,
        )
    generation = metadata.get("policy_generation")
    require(
        is_positive_int(generation),
        "INVALID_POLICY_GENERATION",
        "v2 policy_generation must be a positive integer",
        version=manifest_version,
        policy_generation=generation,
    )
    generation_floor = metadata.get("generation_floor")
    require(
        is_positive_int(generation_floor) and generation_floor <= generation,
        "INVALID_POLICY_GENERATION",
        "v2 generation_floor must be positive and no greater than policy_generation",
        version=manifest_version,
        policy_generation=generation,
        generation_floor=generation_floor,
    )
    logical_clock = metadata.get("logical_clock")
    require(
        is_nonnegative_int(logical_clock),
        "INVALID_LOGICAL_CLOCK",
        "v2 metadata logical_clock must be non-negative",
        version=manifest_version,
        logical_clock=logical_clock,
    )
    absolute_effective_ms = metadata.get("absolute_effective_ms")
    require(
        is_nonnegative_int(absolute_effective_ms),
        "INVALID_V2_EFFECTIVE_TIME",
        "v2 absolute_effective_ms must be non-negative",
        version=manifest_version,
        absolute_effective_ms=absolute_effective_ms,
    )
    policy_key = metadata.get("policy_key")
    require(
        isinstance(policy_key, str) and bool(policy_key),
        "INVALID_V2_PACKAGE",
        "v2 policy_key must be a non-empty string",
        version=manifest_version,
    )
    update_mode = metadata.get("update_mode")
    require(
        update_mode in {"full", "incremental"},
        "INVALID_V2_PACKAGE",
        "v2 update_mode must be 'full' or 'incremental'",
        version=manifest_version,
        update_mode=update_mode,
    )
    base_generation = metadata.get("base_generation")
    if update_mode == "incremental":
        require(
            is_positive_int(base_generation),
            "INVALID_V2_PACKAGE",
            "Incremental v2 package must declare base_generation",
            version=manifest_version,
            base_generation=base_generation,
        )
    if manifest.get("policy_generation") != generation:
        raise _policy_error(
            "V2_METADATA_MISMATCH",
            "v2 manifest and SQLite metadata disagree on policy_generation",
            version=manifest_version,
            manifest_value=manifest.get("policy_generation"),
            metadata_value=generation,
        )
    shard_count = metadata.get("shard_count")
    require(
        is_positive_int(shard_count),
        "INVALID_V2_PACKAGE",
        "v2 shard_count must be positive",
        version=manifest_version,
        policy_generation=generation,
    )
    if len(shard_rows) != shard_count:
        raise _policy_error(
            "V2_SHARD_COUNT_MISMATCH",
            "v2 shard metadata count does not match package_info",
            version=manifest_version,
            expected_shard_count=shard_count,
            actual_shard_count=len(shard_rows),
            shard_index=None,
        )
    for field in (
        "policy_key",
        "policy_generation",
        "generation_floor",
        "logical_clock",
        "absolute_effective_ms",
        "update_mode",
        "base_generation",
        "shard_count",
        "dependency_digest",
    ):
        if field in manifest and manifest.get(field) != metadata.get(field):
            raise _policy_error(
                "V2_METADATA_MISMATCH",
                f"v2 manifest and SQLite metadata disagree on {field}",
                version=manifest_version,
                field=field,
                manifest_value=manifest.get(field),
                metadata_value=metadata.get(field),
            )
    shard_indexes = [int(row["shard_index"]) for row in shard_rows]
    if shard_indexes != list(range(1, shard_count + 1)):
        raise _policy_error(
            "V2_SHARD_INDEX_MISMATCH",
            "v2 shard indexes must be contiguous from 1",
            version=manifest_version,
            shard_indexes=shard_indexes,
        )
    entries: dict[str, dict[str, Any]] = {}
    shard_hashes: list[dict[str, Any]] = []
    for shard_row in shard_rows:
        shard_index = int(shard_row["shard_index"])
        filename = shard_row["filename"]
        if (
            not isinstance(filename, str)
            or "/" in filename
            or "\\" in filename
            or filename in {"", ".", ".."}
        ):
            raise _policy_error(
                "INVALID_V2_PACKAGE",
                "v2 shard filename must be a simple filename",
                version=manifest_version,
                shard_index=shard_index,
            )
        shard_path = package_dir / filename
        try:
            raw = shard_path.read_bytes()
        except FileNotFoundError as exc:
            raise _policy_error(
                "V2_SHARD_MISSING",
                f"v2 shard file is missing: {filename}",
                version=manifest_version,
                shard_index=shard_index,
                path=str(shard_path),
            ) from exc
        actual_hash = sha256_bytes(raw)
        expected_hash = shard_row["sha256"]
        if actual_hash != expected_hash:
            raise _policy_error(
                "SHARD_HASH_MISMATCH",
                f"v2 shard {shard_index} hash does not match SQLite metadata",
                version=manifest_version,
                shard_index=shard_index,
                expected_sha256=expected_hash,
                actual_sha256=actual_hash,
                path=str(shard_path),
            )
        try:
            shard_payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _policy_error(
                "V2_SHARD_JSON_INVALID",
                f"v2 shard {shard_index} is not valid JSON",
                version=manifest_version,
                shard_index=shard_index,
                parse_error=str(exc),
            ) from exc
        if not isinstance(shard_payload, dict):
            raise _policy_error(
                "V2_SHARD_JSON_INVALID",
                f"v2 shard {shard_index} must be a JSON object",
                version=manifest_version,
                shard_index=shard_index,
            )
        for field, expected in (
            ("policy_generation", generation),
            ("logical_clock", logical_clock),
            ("absolute_effective_ms", absolute_effective_ms),
            ("shard_index", shard_index),
            ("policy_key", policy_key),
        ):
            if shard_payload.get(field) != expected:
                raise _policy_error(
                    "V2_SHARD_METADATA_MISMATCH",
                    f"v2 shard {shard_index} field {field} disagrees with metadata",
                    version=manifest_version,
                    shard_index=shard_index,
                    field=field,
                    expected=expected,
                    actual=shard_payload.get(field),
                )
        templates = shard_payload.get("templates")
        if not isinstance(templates, dict):
            raise _policy_error(
                "V2_SHARD_JSON_INVALID",
                f"v2 shard {shard_index} templates must be an object",
                version=manifest_version,
                shard_index=shard_index,
            )
        if int(shard_row["entry_count"]) != len(templates):
            raise _policy_error(
                "V2_SHARD_COUNT_MISMATCH",
                f"v2 shard {shard_index} entry count disagrees with metadata",
                version=manifest_version,
                shard_index=shard_index,
                expected=shard_row["entry_count"],
                actual=len(templates),
            )
        for name, spec in templates.items():
            if not isinstance(name, str):
                raise _policy_error(
                    "INVALID_POLICY_ENTRY",
                    f"v2 shard {shard_index} has a non-string template name",
                    version=manifest_version,
                    shard_index=shard_index,
                )
            entry_version, dependencies, labels, entry_clock = _entry_fields(
                name,
                spec,
                version=manifest_version,
                shard_index=shard_index,
            )
            if name in entries:
                raise _policy_error(
                    "POLICY_ENTRY_DUPLICATE",
                    f"Template {name!r} appears in more than one v2 shard",
                    template_name=name,
                    version=entry_version,
                    shard_index=shard_index,
                )
            entry_effective = spec.get(
                "absolute_effective_ms",
                absolute_effective_ms,
            )
            require(
                is_nonnegative_int(entry_effective),
                "INVALID_V2_EFFECTIVE_TIME",
                f"v2 entry {name!r} absolute_effective_ms must be non-negative",
                template_name=name,
                version=entry_version,
                shard_index=shard_index,
            )
            entries[name] = {
                "version": entry_version,
                "dependencies": dependencies,
                "labels": labels,
                "logical_clock": (
                    entry_clock if entry_clock is not None else logical_clock
                ),
                "policy_generation": generation,
                "absolute_effective_ms": entry_effective,
            }
        shard_hashes.append(
            {
                "shard_index": shard_index,
                "filename": filename,
                "sha256": actual_hash,
                "entry_count": len(templates),
            }
        )
    tombstones: list[V2Tombstone] = []
    seen_tombstones: set[str] = set()
    for row in tombstone_rows:
        name = row["template_name"]
        if name in seen_tombstones:
            raise _policy_error(
                "V2_TOMBSTONE_DUPLICATE",
                f"Duplicate tombstone for template {name!r}",
                template_name=name,
                version=manifest_version,
            )
        seen_tombstones.add(name)
        require(
            is_positive_int(row["version"]),
            "INVALID_TEMPLATE_VERSION",
            f"Tombstone for {name!r} has invalid version",
            template_name=name,
            version=row["version"],
        )
        require(
            is_nonnegative_int(row["logical_clock"]),
            "INVALID_LOGICAL_CLOCK",
            f"Tombstone for {name!r} has invalid logical_clock",
            template_name=name,
            version=row["version"],
            logical_clock=row["logical_clock"],
        )
        tombstones.append(
            V2Tombstone(
                template_name=name,
                version=int(row["version"]),
                logical_clock=int(row["logical_clock"]),
                reason=str(row["reason"]),
            )
        )
    invalidations: list[TopologyInvalidation] = []
    invalidation_scopes: set[str] = set()
    for row in invalidation_rows:
        require(
            is_nonnegative_int(row["logical_clock"]),
            "INVALID_LOGICAL_CLOCK",
            "Topology invalidation logical_clock must be non-negative",
            version=manifest_version,
            logical_clock=row["logical_clock"],
        )
        scope = row["scope"]
        if not isinstance(scope, str) or not scope:
            raise _policy_error(
                "INVALID_V2_PACKAGE",
                "Topology invalidation scope must be a non-empty string",
                version=manifest_version,
            )
        if scope in invalidation_scopes:
            raise _policy_error(
                "V2_TOPOLOGY_INVALIDATION_DUPLICATE",
                f"Duplicate topology invalidation scope: {scope}",
                version=manifest_version,
                scope=scope,
            )
        invalidation_scopes.add(scope)
        invalidations.append(
            TopologyInvalidation(
                scope=scope,
                logical_clock=int(row["logical_clock"]),
                reason=str(row["reason"]),
            )
        )
    package_digest = sha256_json(
        {
            "policy_key": policy_key,
            "policy_generation": generation,
            "generation_floor": generation_floor,
            "manifest_version": manifest_version,
            "logical_clock": logical_clock,
            "absolute_effective_ms": absolute_effective_ms,
            "update_mode": update_mode,
            "base_generation": base_generation,
            "dependency_digest": metadata.get("dependency_digest"),
            "shards": shard_hashes,
            "tombstones": [tomb.__dict__ for tomb in tombstones],
            "topology_invalidations": [
                invalidation.__dict__ for invalidation in invalidations
            ],
        }
    )
    return V2PolicyPackage(
        path=str(package_dir),
        policy_key=policy_key,
        policy_generation=generation,
        generation_floor=generation_floor,
        manifest_version=manifest_version,
        logical_clock=logical_clock,
        absolute_effective_ms=absolute_effective_ms,
        update_mode=update_mode,
        base_generation=base_generation,
        dependency_digest=str(metadata.get("dependency_digest")),
        shard_count=shard_count,
        entries=entries,
        tombstones=tombstones,
        topology_invalidations=invalidations,
        bundle_digest=package_digest,
        metadata={**metadata, "shards": shard_hashes},
    )


def _v1_candidate(
    name: str,
    entry: dict[str, Any],
    *,
    policy_key: str,
    manifest_version: str,
    relative_seconds: int,
    injected_clock_ms: int,
) -> PolicyCandidate:
    effective_at = injected_clock_ms + relative_seconds * 1000
    original_time = {
        "kind": "relative_seconds",
        "value": relative_seconds,
        "snapshot_logical_clock_ms": injected_clock_ms,
    }
    conversion = {
        "scale_ms_per_second": 1000,
        "rounding": "exact_integer",
        "pre_round_value": relative_seconds,
        "rounding_direction": "none",
    }
    return PolicyCandidate(
        template_name=name,
        version=entry["version"],
        dependencies=tuple(sorted(entry["dependencies"])),
        labels=tuple(entry["labels"]),
        logical_clock=entry["logical_clock"],
        policy_generation=entry["policy_generation"],
        source_format="v1",
        source_version=manifest_version,
        original_time=original_time,
        conversion=conversion,
        effective_at_ms=effective_at,
        decision_basis="source_only",
        decision_detail=f"validated v1 snapshot {policy_key}",
    )


def _v2_candidate(
    name: str,
    entry: dict[str, Any],
    *,
    manifest_version: str,
) -> PolicyCandidate:
    effective_at = entry["absolute_effective_ms"]
    return PolicyCandidate(
        template_name=name,
        version=entry["version"],
        dependencies=tuple(sorted(entry["dependencies"])),
        labels=tuple(entry["labels"]),
        logical_clock=entry["logical_clock"],
        policy_generation=entry["policy_generation"],
        source_format="v2",
        source_version=manifest_version,
        original_time={
            "kind": "absolute_ms",
            "value": effective_at,
        },
        conversion={
            "scale_ms_per_second": 1,
            "rounding": "none",
            "pre_round_value": effective_at,
            "rounding_direction": "none",
        },
        effective_at_ms=effective_at,
        decision_basis="source_only",
        decision_detail=f"validated v2 package {manifest_version}",
    )


def _adjudicate(
    left: PolicyCandidate,
    right: PolicyCandidate,
) -> PolicyCandidate:
    def rank(candidate: PolicyCandidate) -> tuple[int, int, int]:
        return (
            candidate.logical_clock,
            candidate.policy_generation,
            1 if candidate.source_format == "v2" else 0,
        )

    left_rank = rank(left)
    right_rank = rank(right)
    if left_rank > right_rank:
        winner, loser = left, right
    else:
        winner, loser = right, left
    if winner.logical_clock > loser.logical_clock:
        basis = "higher_logical_clock"
        detail = (
            f"logical_clock {winner.logical_clock} > {loser.logical_clock}; "
            f"selected {winner.source_format}"
        )
    elif winner.policy_generation > loser.policy_generation:
        basis = "higher_policy_generation"
        detail = (
            f"equal logical_clock {winner.logical_clock}; "
            f"generation {winner.policy_generation} > {loser.policy_generation}"
        )
    else:
        basis = "source_format_priority"
        detail = (
            f"equal logical_clock and generation; "
            f"source priority v2 > v1 selected {winner.source_format}"
        )
    winner.decision_basis = basis
    winner.decision_detail = detail
    return winner


def _merge_candidates(
    v1: V1Policy,
    v2: V2PolicyPackage,
    *,
    injected_clock_ms: int,
) -> dict[str, PolicyCandidate]:
    candidates: dict[str, PolicyCandidate] = {}
    for name, entry in v1.entries.items():
        candidates[name] = _v1_candidate(
            name,
            entry,
            policy_key=v1.policy_key,
            manifest_version=v1.manifest_version,
            relative_seconds=v1.relative_effective_seconds,
            injected_clock_ms=injected_clock_ms,
        )
    for name, entry in v2.entries.items():
        candidate = _v2_candidate(
            name,
            entry,
            manifest_version=v2.manifest_version,
        )
        existing = candidates.get(name)
        candidates[name] = (
            candidate if existing is None else _adjudicate(existing, candidate)
        )
    _apply_tombstones(candidates, v2.tombstones, v2)
    _validate_final_graph(candidates)
    v2_layer_digest = effective_v2_dependency_digest(candidates.values())
    metadata_digest = v2.metadata.get("dependency_digest")
    if v2_layer_digest != metadata_digest:
        raise _policy_error(
            "POLICY_DEPENDENCY_DIGEST_MISMATCH",
            "Effective v2 dependency digest does not match SQLite metadata",
            version=v2.manifest_version,
            shard_index=None,
            expected_digest=metadata_digest,
            actual_digest=v2_layer_digest,
        )
    return candidates


def merge_v1_v2(
    v1: V1Policy,
    v2: V2PolicyPackage,
    *,
    injected_clock_ms: int,
) -> dict[str, PolicyCandidate]:
    if v1.policy_key != v2.policy_key:
        raise _policy_error(
            "POLICY_KEY_MISMATCH",
            "v1 and v2 snapshots use different policy keys",
            version=v2.manifest_version,
            v1_policy_key=v1.policy_key,
            v2_policy_key=v2.policy_key,
        )
    return _merge_candidates(v1, v2, injected_clock_ms=injected_clock_ms)


def overlay_incremental(
    base: dict[str, PolicyCandidate],
    package: V2PolicyPackage,
) -> dict[str, PolicyCandidate]:
    if package.update_mode != "incremental":
        raise _policy_error(
            "POLICY_MODE_MISMATCH",
            "overlay_incremental requires an incremental package",
            version=package.manifest_version,
        )
    candidates = {name: _copy_candidate(candidate) for name, candidate in base.items()}
    for name, entry in package.entries.items():
        previous = candidates.get(name)
        candidate = _v2_candidate(
            name,
            entry,
            manifest_version=package.manifest_version,
        )
        if previous is not None:
            if candidate.version < previous.version:
                raise _policy_error(
                    "TEMPLATE_VERSION_ROLLBACK",
                    f"Incoming policy would roll template {name} back from "
                    f"{previous.version} to {candidate.version}",
                    template_name=name,
                    version=candidate.version,
                    previous_version=previous.version,
                    shard_index=None,
                )
            if candidate.logical_clock > previous.logical_clock:
                candidate.decision_basis = "higher_logical_clock"
                candidate.decision_detail = (
                    f"incremental generation {package.policy_generation}; "
                    f"logical_clock {candidate.logical_clock} > "
                    f"{previous.logical_clock}"
                )
            elif candidate.logical_clock == previous.logical_clock:
                candidate.decision_basis = "higher_policy_generation"
                candidate.decision_detail = (
                    f"incremental patch generation {package.policy_generation} "
                    f"supersedes generation {previous.policy_generation}"
                )
            else:
                raise _policy_error(
                    "POLICY_CLOCK_ROLLBACK",
                    f"Incremental patch for {name} has an older logical clock",
                    template_name=name,
                    version=candidate.version,
                    incoming_clock=candidate.logical_clock,
                    current_clock=previous.logical_clock,
                )
        candidates[name] = candidate
    _apply_tombstones(candidates, package.tombstones, package)
    _validate_final_graph(candidates)
    v2_layer_digest = effective_v2_dependency_digest(candidates.values())
    metadata_digest = package.metadata.get("dependency_digest")
    if v2_layer_digest != metadata_digest:
        raise _policy_error(
            "POLICY_DEPENDENCY_DIGEST_MISMATCH",
            "Effective v2 dependency digest after incremental overlay "
            "does not match SQLite metadata",
            version=package.manifest_version,
            expected_digest=metadata_digest,
            actual_digest=v2_layer_digest,
        )
    return candidates


def _copy_candidate(candidate: PolicyCandidate) -> PolicyCandidate:
    return PolicyCandidate(**candidate.__dict__)


def _apply_tombstones(
    candidates: dict[str, PolicyCandidate],
    tombstones: Iterable[V2Tombstone],
    package: V2PolicyPackage,
) -> None:
    for tombstone in tombstones:
        candidate = candidates.get(tombstone.template_name)
        if candidate is None:
            raise _policy_error(
                "V2_TOMBSTONE_UNKNOWN_TEMPLATE",
                f"Tombstone references unknown template {tombstone.template_name!r}",
                template_name=tombstone.template_name,
                version=tombstone.version,
            )
        if tombstone.logical_clock < candidate.logical_clock:
            continue
        candidate.tombstone = True
        candidate.tombstone_reason = tombstone.reason
        if tombstone.version > candidate.version:
            candidate.version = tombstone.version
        if tombstone.logical_clock >= candidate.logical_clock:
            candidate.logical_clock = tombstone.logical_clock
            candidate.policy_generation = package.policy_generation
            candidate.source_format = "v2"
            candidate.source_version = package.manifest_version
            candidate.effective_at_ms = package.absolute_effective_ms
            candidate.original_time = {
                "kind": "absolute_ms",
                "value": package.absolute_effective_ms,
            }
            candidate.conversion = {
                "scale_ms_per_second": 1,
                "rounding": "none",
                "pre_round_value": package.absolute_effective_ms,
                "rounding_direction": "none",
            }
            candidate.decision_basis = "tombstone"
            candidate.decision_detail = (
                f"generation {package.policy_generation} tombstone "
                f"{tombstone.reason}"
            )


def _validate_final_graph(candidates: dict[str, PolicyCandidate]) -> None:
    graph = {
        name: tuple(candidate.dependencies)
        for name, candidate in candidates.items()
    }
    _validate_graph(graph, source="merged policy", version=None, allow_missing=False)


def _validate_graph(
    graph: dict[str, tuple[str, ...]],
    *,
    source: str,
    version: str | int | None,
    allow_missing: bool,
) -> None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(name: str) -> None:
        status = state.get(name, 0)
        if status == 1:
            start = stack.index(name) if name in stack else 0
            cycle = stack[start:] + [name]
            raise _policy_error(
                "DEPENDENCY_CYCLE",
                f"{source} dependency graph contains a cycle: {' -> '.join(cycle)}",
                template_name=name,
                version=version,
                cycle=cycle,
            )
        if status == 2:
            return
        state[name] = 1
        stack.append(name)
        for dependency in graph.get(name, ()):
            if dependency not in graph:
                if allow_missing:
                    continue
                raise _policy_error(
                    "MISSING_DEPENDENCY",
                    f"{source} template {name!r} depends on missing template "
                    f"{dependency!r}",
                    template_name=name,
                    version=version,
                    missing_dependency=dependency,
                )
            visit(dependency)
        stack.pop()
        state[name] = 2

    for name in sorted(graph):
        visit(name)


def dependency_digest(candidates: Iterable[PolicyCandidate]) -> str:
    payload = {
        candidate.template_name: sorted(candidate.dependencies)
        for candidate in sorted(candidates, key=lambda item: item.template_name)
    }
    return sha256_json(payload)


def effective_v2_dependency_digest(candidates: Iterable[PolicyCandidate]) -> str:
    return dependency_digest(
        candidate
        for candidate in candidates
        if candidate.source_format == "v2"
    )


def dependency_digest_set(candidates: Iterable[PolicyCandidate]) -> dict[str, str]:
    ordered = sorted(candidates, key=lambda item: item.template_name)
    return {
        candidate.template_name: sha256_json(
            {
                "template_name": candidate.template_name,
                "dependencies": sorted(candidate.dependencies),
                "version": candidate.version,
                "source_format": candidate.source_format,
                "source_version": candidate.source_version,
            }
        )
        for candidate in ordered
    }


def persist_merge_decisions(
    connection: sqlite3.Connection,
    *,
    policy_key: str,
    injected_clock_ms: int,
    candidates: dict[str, PolicyCandidate],
) -> None:
    for name in sorted(candidates):
        candidate = candidates[name]
        payload = {
            "injected_clock_ms": injected_clock_ms,
            "candidate": candidate.node_payload(),
        }
        write_merge_decision(
            connection,
            policy_key,
            injected_clock_ms,
            name,
            payload,
        )


def candidate_json(candidates: dict[str, PolicyCandidate]) -> dict[str, Any]:
    return {
        name: candidate.node_payload()
        for name, candidate in sorted(candidates.items())
    }


def source_summary(
    v1: V1Policy,
    v2: V2PolicyPackage,
) -> dict[str, Any]:
    return {
        "v1": {
            "manifest_version": v1.manifest_version,
            "logical_clock": v1.logical_clock,
            "relative_effective_seconds": v1.relative_effective_seconds,
            "policy_generation": v1.policy_generation,
            "no_topology_constraints": v1.no_topology_constraints,
            "entry_count": len(v1.entries),
        },
        "v2": {
            "manifest_version": v2.manifest_version,
            "logical_clock": v2.logical_clock,
            "policy_generation": v2.policy_generation,
            "generation_floor": v2.generation_floor,
            "update_mode": v2.update_mode,
            "shard_count": v2.shard_count,
            "bundle_digest": v2.bundle_digest,
            "dependency_digest": v2.dependency_digest,
        },
    }


def policy_package_debug(path: Path) -> str:
    """Return a deterministic diagnostic rendering used by tests."""

    return pretty_json(load_v2_package(path).metadata)
