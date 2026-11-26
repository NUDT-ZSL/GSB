from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import BizError, ErrorCode
from .hashing import canonical_digest, sha256_file
from .tensors import is_non_negative_int


def read_json(
    path: Path,
    malformed_code: str = ErrorCode.CONFIG_V2_INVALID,
    missing_code: str = ErrorCode.CONFIG_PACKAGE_NOT_FOUND,
) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BizError(
            missing_code,
            "required JSON file does not exist",
            {"path": str(path)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise BizError(
            malformed_code,
            "JSON file is malformed",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc


def validate_v1_config(raw: Any, source_path: Optional[Path] = None) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise BizError(ErrorCode.CONFIG_V1_INVALID, "v1 config must be an object")
    effective = raw.get("effective_after_seconds")
    if not is_non_negative_int(effective):
        raise BizError(
            ErrorCode.V1_EFFECTIVE_SECONDS_INVALID,
            "v1 effective_after_seconds must be a non-negative integer",
            {"effective_after_seconds": effective},
        )
    policy = raw.get("cache_policy", {})
    if not isinstance(policy, dict):
        raise BizError(
            ErrorCode.CONFIG_V1_INVALID, "v1 cache_policy must be a flat object"
        )
    for key, value in policy.items():
        if not isinstance(key, str) or "." not in key:
            raise BizError(
                ErrorCode.CONFIG_V1_INVALID,
                "v1 cache_policy keys must be dotted flat strings",
                {"key": key},
            )
        if isinstance(value, (dict, list)):
            raise BizError(
                ErrorCode.CONFIG_V1_INVALID,
                "v1 cache_policy must be flat scalar values",
                {"key": key, "value_type": type(value).__name__},
            )
    version = raw.get("manifest_version", 1)
    if not is_non_negative_int(version):
        raise BizError(
            ErrorCode.CONFIG_V1_INVALID,
            "v1 manifest_version must be a non-negative integer",
            {"manifest_version": version},
        )
    return {
        "format_generation": "v1",
        "manifest_version": int(version),
        "effective_after_seconds": int(effective),
        "cache_policy": dict(sorted(policy.items())),
        "source_path": str(source_path) if source_path else None,
    }


def _metadata_rows(metadata_path: Path) -> Dict[str, Tuple[str, int]]:
    if not metadata_path.exists():
        raise BizError(
            ErrorCode.CONFIG_V2_INVALID,
            "v2 SQLite metadata database does not exist",
            {"path": str(metadata_path)},
        )
    try:
        conn = sqlite3.connect(f"file:{metadata_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT path, sha256, generation FROM config_shard"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BizError(
            ErrorCode.CONFIG_METADATA_MISMATCH,
            "cannot read v2 config SQLite metadata",
            {"path": str(metadata_path), "error": str(exc)},
        ) from exc
    return {path: (digest, int(generation)) for path, digest, generation in rows}


def read_v2_package(package_dir: Path | str) -> Dict[str, Any]:
    package_dir = Path(package_dir)
    manifest_path = package_dir / "manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise BizError(ErrorCode.CONFIG_V2_INVALID, "v2 manifest must be an object")

    version = manifest.get("manifest_version")
    floor = manifest.get("policy_generation_floor")
    effective_at = manifest.get("effective_at_ms")
    shard_refs = manifest.get("shards")
    if not is_non_negative_int(version) or version <= 0:
        raise BizError(
            ErrorCode.CONFIG_V2_INVALID,
            "v2 manifest_version must be a positive integer",
            {"manifest_version": version},
        )
    if not is_non_negative_int(floor):
        raise BizError(
            ErrorCode.CONFIG_V2_INVALID,
            "policy_generation_floor must be a non-negative integer",
            {"policy_generation_floor": floor},
        )
    if not is_non_negative_int(effective_at):
        raise BizError(
            ErrorCode.CONFIG_V2_INVALID,
            "effective_at_ms must be a non-negative integer",
            {"effective_at_ms": effective_at},
        )
    if not isinstance(shard_refs, list) or not shard_refs:
        raise BizError(
            ErrorCode.CONFIG_V2_INVALID, "v2 manifest requires a non-empty shards list"
        )

    metadata_name = manifest.get("metadata_db", "metadata.sqlite")
    metadata_path = package_dir / str(metadata_name)
    metadata = _metadata_rows(metadata_path)

    shards: List[Dict[str, Any]] = []
    aggregate_policy: Dict[str, Any] = {}
    tombstones: List[str] = []
    previous_generation = int(floor) - 1
    for index, ref in enumerate(shard_refs):
        if not isinstance(ref, dict):
            raise BizError(ErrorCode.CONFIG_V2_INVALID, "shard reference must be object")
        rel_path = ref.get("path")
        expected_hash = ref.get("sha256")
        generation = ref.get("generation")
        if not isinstance(rel_path, str) or rel_path.startswith("/") or ".." in Path(rel_path).parts:
            raise BizError(
                ErrorCode.CONFIG_V2_INVALID,
                "shard path must be a safe relative path",
                {"path": rel_path},
            )
        if not is_non_negative_int(generation) or generation < floor:
            raise BizError(
                ErrorCode.CONFIG_GENERATION_NOT_MONOTONIC,
                "shard generation is below policy_generation_floor",
                {"path": rel_path, "generation": generation, "floor": floor},
            )
        if int(generation) < previous_generation:
            raise BizError(
                ErrorCode.CONFIG_GENERATION_NOT_MONOTONIC,
                "shard policy generations must be non-decreasing",
                {
                    "path": rel_path,
                    "generation": generation,
                    "previous_generation": previous_generation,
                    "index": index,
                },
            )
        previous_generation = int(generation)

        shard_path = package_dir / rel_path
        actual_hash = sha256_file(shard_path)
        if expected_hash != actual_hash:
            raise BizError(
                ErrorCode.CONFIG_SHARD_HASH_MISMATCH,
                "v2 shard hash does not match manifest",
                {
                    "path": rel_path,
                    "expected_sha256": expected_hash,
                    "actual_sha256": actual_hash,
                },
            )
        if rel_path not in metadata:
            raise BizError(
                ErrorCode.CONFIG_METADATA_MISMATCH,
                "shard is missing from SQLite metadata registry",
                {"path": rel_path},
            )
        meta_hash, meta_generation = metadata[rel_path]
        if meta_hash != actual_hash or meta_generation != generation:
            raise BizError(
                ErrorCode.CONFIG_METADATA_MISMATCH,
                "SQLite metadata does not match shard manifest",
                {
                    "path": rel_path,
                    "metadata_hash": meta_hash,
                    "actual_hash": actual_hash,
                    "metadata_generation": meta_generation,
                    "manifest_generation": generation,
                },
            )

        shard = read_json(shard_path)
        if not isinstance(shard, dict):
            raise BizError(ErrorCode.CONFIG_V2_INVALID, "shard must be an object")
        policies = shard.get("policies", {})
        shard_tombstones = shard.get("tombstones", [])
        if not isinstance(policies, dict) or not isinstance(shard_tombstones, list):
            raise BizError(
                ErrorCode.CONFIG_V2_INVALID,
                "shard policies/tombstones have invalid types",
                {"path": rel_path},
            )
        for key, value in policies.items():
            aggregate_policy[key] = value
        for key in shard_tombstones:
            if not isinstance(key, str):
                raise BizError(ErrorCode.CONFIG_V2_INVALID, "tombstone keys must be strings")
            if key not in tombstones:
                tombstones.append(key)
        shards.append(
            {
                "path": rel_path,
                "sha256": actual_hash,
                "generation": int(generation),
                "shard_id": shard.get("shard_id", Path(rel_path).stem),
            }
        )

    for key in tombstones:
        aggregate_policy.pop(key, None)
    key_set = sorted(aggregate_policy)
    expected_key_digest = manifest.get("cache_key_set_digest")
    actual_key_digest = canonical_digest(
        {"keys": key_set, "tombstones": sorted(tombstones)}
    )
    if expected_key_digest != actual_key_digest:
        raise BizError(
            ErrorCode.CONFIG_KEY_DIGEST_MISMATCH,
            "v2 cache key set digest does not match shard contents",
            {
                "expected": expected_key_digest,
                "actual": actual_key_digest,
                "keys": key_set,
                "tombstones": sorted(tombstones),
            },
        )

    package_hash = canonical_digest(
        {
            "manifest_version": version,
            "policy_generation_floor": floor,
            "effective_at_ms": effective_at,
            "shards": shards,
            "cache_key_set_digest": actual_key_digest,
        }
    )
    return {
        "format_generation": "v2",
        "package_dir": str(package_dir),
        "package_id": manifest.get("package_id", f"config-{version}"),
        "manifest_version": int(version),
        "policy_generation_floor": int(floor),
        "effective_at_ms": int(effective_at),
        "shards": shards,
        "policies": dict(sorted(aggregate_policy.items())),
        "tombstones": sorted(tombstones),
        "key_set": key_set,
        "cache_key_set_digest": actual_key_digest,
        "package_hash": package_hash,
        "manifest": manifest,
    }


def merge_config(v1: Dict[str, Any], v2: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(v1.get("cache_policy", {}))
    merged.update(v2.get("policies", {}))
    for key in v2.get("tombstones", []):
        merged.pop(key, None)
    normalized = dict(sorted(merged.items()))
    return {
        "format_generation": "v1+v2",
        "v1_manifest_version": v1.get("manifest_version"),
        "config_version": v2["manifest_version"],
        "policy_generation": max(
            int(v2["policy_generation_floor"]),
            max((int(s["generation"]) for s in v2["shards"]), default=0),
        ),
        "policy_generation_floor": v2["policy_generation_floor"],
        "effective_after_seconds": v1.get("effective_after_seconds"),
        "effective_at_ms": v2.get("effective_at_ms"),
        "policies": normalized,
        "tombstones": v2.get("tombstones", []),
        "merged_digest": canonical_digest(normalized),
    }


def classify_config_change(
    active: Optional[Dict[str, Any]], new_package: Dict[str, Any]
) -> str:
    if active is None:
        return "full_replace"
    active_version = int(active["config_version"])
    new_version = int(new_package["manifest_version"])
    if new_version < active_version:
        raise BizError(
            ErrorCode.CONFIG_VERSION_ROLLBACK,
            "config manifest version rollback is rejected",
            {"active_version": active_version, "new_version": new_version},
        )
    if int(new_package["policy_generation_floor"]) < int(
        active["policy_generation_floor"]
    ):
        raise BizError(
            ErrorCode.CONFIG_GENERATION_NOT_MONOTONIC,
            "policy generation floor cannot move backwards",
            {
                "active_floor": active["policy_generation_floor"],
                "new_floor": new_package["policy_generation_floor"],
            },
        )
    if new_version == active_version:
        if new_package["package_hash"] == active["package_hash"]:
            return "noop"
        raise BizError(
            ErrorCode.CONFIG_VERSION_CONFLICT,
            "same config manifest version has different shard hashes",
            {"version": new_version},
        )

    active_manifest = active.get("manifest_json")
    if isinstance(active_manifest, str):
        active_manifest = json.loads(active_manifest)
    active_shards = {
        item["path"]: item for item in (active_manifest or {}).get("shards", [])
    }
    new_shards = {item["path"]: item for item in new_package["shards"]}
    old_unchanged = all(
        path in new_shards
        and new_shards[path]["sha256"] == old["sha256"]
        and new_shards[path]["generation"] == old["generation"]
        for path, old in active_shards.items()
    )
    active_keys = set(active_manifest.get("key_set") or [])
    if not active_keys and active.get("merged_json"):
        merged = active["merged_json"]
        if isinstance(merged, str):
            merged = json.loads(merged)
        active_keys = set((merged or {}).get("policies", {}).keys())
    new_keys = set(new_package["key_set"])
    if old_unchanged and active_keys.issubset(new_keys):
        return "incremental_append"
    return "full_replace"
