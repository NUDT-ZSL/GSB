from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checksums import is_int, load_json, sha256_file
from .errors import (
    E_MISSING_FILE,
    E_PACKAGE_ADJACENCY_DIGEST,
    E_PACKAGE_NODE_BOUNDS,
    E_PACKAGE_SCHEMA,
    E_SHARD_HASH_MISMATCH,
    E_SOURCE_HASH_MISMATCH,
    GraphStoreError,
    require,
)
from .merge import merge_snapshots
from .model import ClockConfig, GraphIndex, V1Snapshot, V2Snapshot
from .validation import validate_v1_snapshot, validate_v2_snapshot


@dataclass(frozen=True)
class LoadedPackage:
    package_id: str
    package_version: int
    logical_clock_ms: int
    change_kind: str
    manifest_path: str
    manifest_sha256: str
    manifest: dict[str, Any]
    v1: V1Snapshot
    v2: V2Snapshot
    index: GraphIndex
    node_count_bounds: dict[str, int]

    @property
    def shard_hashes(self) -> dict[str, str]:
        return {shard.shard_id: shard.file_sha256 for shard in self.v2.shards}


def _resolve_file(base: Path, value: Any, field: str) -> Path:
    require(
        isinstance(value, str) and bool(value),
        E_PACKAGE_SCHEMA,
        f"package.{field} must be a non-empty relative path",
        field=field,
    )
    path = (base / value).resolve()
    require(
        path.is_file(),
        E_MISSING_FILE,
        f"package.{field} file is missing",
        field=field,
        path=str(path),
    )
    return path


def load_package(manifest_path: Path, clock: ClockConfig) -> LoadedPackage:
    manifest_path = manifest_path.resolve()
    manifest = load_json(manifest_path, label="package manifest")
    require(
        isinstance(manifest, dict),
        E_PACKAGE_SCHEMA,
        "package manifest root must be an object",
        path=str(manifest_path),
    )
    require(
        manifest.get("schema") == "graph-package/v1",
        E_PACKAGE_SCHEMA,
        "package schema must be graph-package/v1",
        path=str(manifest_path),
    )
    package_id = manifest.get("package_id")
    require(
        isinstance(package_id, str) and bool(package_id),
        E_PACKAGE_SCHEMA,
        "package_id must be a non-empty string",
    )
    package_version = manifest.get("package_version")
    require(
        is_int(package_version) and package_version >= 0,
        E_PACKAGE_SCHEMA,
        "package_version must be a non-negative integer",
    )
    logical_clock_ms = manifest.get("logical_clock_ms")
    require(
        is_int(logical_clock_ms) and logical_clock_ms >= 0,
        E_PACKAGE_SCHEMA,
        "logical_clock_ms must be a non-negative integer",
    )
    change_kind = manifest.get("change_kind", "full")
    require(
        change_kind in {"full", "append"},
        E_PACKAGE_SCHEMA,
        "change_kind must be full or append",
        value=change_kind,
    )

    sources = manifest.get("sources")
    require(
        isinstance(sources, dict),
        E_PACKAGE_SCHEMA,
        "package.sources must be an object",
    )
    v1_entry = sources.get("v1")
    v2_entry = sources.get("v2")
    require(
        isinstance(v1_entry, dict) and isinstance(v2_entry, dict),
        E_PACKAGE_SCHEMA,
        "package.sources must declare v1 and v2",
    )
    base = manifest_path.parent
    v1_path = _resolve_file(base, v1_entry.get("path"), "sources.v1.path")
    v2_manifest_path = _resolve_file(
        base, v2_entry.get("manifest"), "sources.v2.manifest"
    )
    v2_metadata_path = _resolve_file(
        base, v2_entry.get("metadata_db"), "sources.v2.metadata_db"
    )

    require(
        v1_path.is_file(),
        E_MISSING_FILE,
        "v1 snapshot file is missing",
        path=str(v1_path),
    )
    expected_v1_hash = v1_entry.get("sha256")
    observed_v1_hash = sha256_file(v1_path)
    require(
        expected_v1_hash == observed_v1_hash,
        E_SOURCE_HASH_MISMATCH,
        "v1 source file hash mismatch",
        path=str(v1_path),
        expected=expected_v1_hash,
        observed=observed_v1_hash,
    )
    expected_v2_manifest_hash = v2_entry.get("sha256")
    observed_v2_manifest_hash = sha256_file(v2_manifest_path)
    require(
        expected_v2_manifest_hash == observed_v2_manifest_hash,
        E_SOURCE_HASH_MISMATCH,
        "v2 manifest file hash mismatch",
        path=str(v2_manifest_path),
        expected=expected_v2_manifest_hash,
        observed=observed_v2_manifest_hash,
    )
    expected_v2_metadata_hash = v2_entry.get("metadata_sha256")
    observed_v2_metadata_hash = sha256_file(v2_metadata_path)
    require(
        expected_v2_metadata_hash == observed_v2_metadata_hash,
        E_SOURCE_HASH_MISMATCH,
        "v2 metadata file hash mismatch",
        path=str(v2_metadata_path),
        expected=expected_v2_metadata_hash,
        observed=observed_v2_metadata_hash,
    )

    v1 = validate_v1_snapshot(v1_path)
    v2 = validate_v2_snapshot(v2_manifest_path)
    require(
        Path(v2.metadata_path).resolve() == v2_metadata_path.resolve(),
        E_PACKAGE_SCHEMA,
        "package v2 metadata path does not match the v2 manifest",
        package_path=str(v2_metadata_path),
        manifest_path=v2.metadata_path,
    )

    package_shards = manifest.get("shards")
    require(
        isinstance(package_shards, list) and bool(package_shards),
        E_PACKAGE_SCHEMA,
        "package.shards must be a non-empty array",
    )
    require(
        len(package_shards) == len(v2.shards),
        E_PACKAGE_SCHEMA,
        "package shard count does not match v2 manifest",
        package=len(package_shards),
        v2=len(v2.shards),
    )
    v2_shards = {shard.shard_id: shard for shard in v2.shards}
    seen_shards: set[str] = set()
    for entry in package_shards:
        require(
            isinstance(entry, dict),
            E_PACKAGE_SCHEMA,
            "package shard entry must be an object",
        )
        shard_id = entry.get("shard_id")
        require(
            isinstance(shard_id, str) and shard_id in v2_shards,
            E_PACKAGE_SCHEMA,
            "package references an unknown shard",
            shard_id=shard_id,
        )
        require(
            shard_id not in seen_shards,
            E_PACKAGE_SCHEMA,
            "package shard IDs must be unique",
            shard_id=shard_id,
        )
        seen_shards.add(shard_id)
        shard_path = _resolve_file(base, entry.get("path"), f"shards.{shard_id}.path")
        observed_hash = sha256_file(shard_path)
        expected_hash = entry.get("sha256")
        require(
            expected_hash == observed_hash == v2_shards[shard_id].file_sha256,
            E_SHARD_HASH_MISMATCH,
            "package shard file hash mismatch",
            shard_id=shard_id,
            expected=expected_hash,
            observed=observed_hash,
            v2_observed=v2_shards[shard_id].file_sha256,
        )

    require(
        v1.logical_clock_ms <= logical_clock_ms,
        E_PACKAGE_SCHEMA,
        "package logical clock predates v1 source",
        package_clock=logical_clock_ms,
        v1_clock=v1.logical_clock_ms,
    )
    require(
        v2.logical_clock_ms <= logical_clock_ms,
        E_PACKAGE_SCHEMA,
        "package logical clock predates v2 source",
        package_clock=logical_clock_ms,
        v2_clock=v2.logical_clock_ms,
    )

    merged = merge_snapshots(v1, v2, clock)
    bounds = manifest.get("node_count_bounds")
    require(
        isinstance(bounds, dict)
        and is_int(bounds.get("min"))
        and is_int(bounds.get("max"))
        and 0 <= bounds["min"] <= bounds["max"],
        E_PACKAGE_NODE_BOUNDS,
        "node_count_bounds must contain ordered non-negative integers",
        value=bounds,
    )
    require(
        bounds["min"] <= merged.node_count <= bounds["max"],
        E_PACKAGE_NODE_BOUNDS,
        "merged node count is outside declared bounds",
        node_count=merged.node_count,
        bounds=bounds,
    )
    declared_adjacency = manifest.get("adjacency_digest")
    observed_adjacency = merged.adjacency_digest()
    require(
        isinstance(declared_adjacency, str)
        and declared_adjacency == observed_adjacency,
        E_PACKAGE_ADJACENCY_DIGEST,
        "merged adjacency digest does not match package manifest",
        declared=declared_adjacency,
        observed=observed_adjacency,
    )
    manifest_sha256 = sha256_file(manifest_path)
    return LoadedPackage(
        package_id=package_id,
        package_version=package_version,
        logical_clock_ms=logical_clock_ms,
        change_kind=change_kind,
        manifest_path=str(manifest_path),
        manifest_sha256=manifest_sha256,
        manifest=manifest,
        v1=v1,
        v2=v2,
        index=merged,
        node_count_bounds={"min": bounds["min"], "max": bounds["max"]},
    )

