from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import Code, KvError
from .util import KEY_RE, canonical_hash, read_json, require_clock_ms, sha256_file


FORMAT_PRIORITY = {"v1": 10, "v2": 20}


@dataclass
class LoadedPolicyPackage:
    entries: List[Dict[str, Any]]
    decisions: List[Dict[str, Any]]
    time_conversions: List[Dict[str, Any]]
    manifest_version: int
    snapshot_type: str
    generation_floor: int
    declared_clock_ms: int
    content_hash: str
    sources: List[str]
    tombstones: List[Dict[str, Any]]


def _valid_key(key: Any) -> bool:
    return isinstance(key, str) and bool(KEY_RE.fullmatch(key))


def _positive_finite_number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a threshold")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
        number = Decimal(str(value))
    elif isinstance(value, str):
        number = Decimal(value)
    else:
        raise ValueError("unsupported threshold type")
    if not number.is_finite() or number <= 0:
        raise ValueError("threshold must be finite and positive")
    return number


def load_v1_policy(path: Path, clock_ms: int) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    payload = read_json(path, parse_decimal=True)
    if not isinstance(payload, dict) or payload.get("format") != "v1":
        raise KvError(Code.V1_SCHEMA_INVALID, f"{path}: missing format=v1 object")

    effective = payload.get("effective_after_seconds")
    if not isinstance(effective, int) or isinstance(effective, bool) or effective < 0:
        raise KvError(Code.V1_EFFECTIVE_SECONDS_INVALID, "v1 effective_after_seconds must be a nonnegative integer")

    overrides = payload.get("effective_seconds", {})
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise KvError(Code.V1_SCHEMA_INVALID, "v1 effective_seconds must be an object")
    for key, value in overrides.items():
        if not _valid_key(key):
            raise KvError(Code.V1_POLICY_KEY_INVALID, f"invalid v1 policy key: {key}")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise KvError(Code.V1_EFFECTIVE_SECONDS_INVALID, f"invalid effective seconds for {key}")

    thresholds = payload.get("thresholds")
    if not isinstance(thresholds, dict) or not thresholds:
        raise KvError(Code.V1_SCHEMA_INVALID, "v1 thresholds must be a nonempty object")

    generation = payload.get("generation", 1)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise KvError(Code.V1_SCHEMA_INVALID, "v1 generation must be a positive integer")

    cold_order = payload.get("cold_order", {})
    if cold_order is None:
        cold_order = {}
    if not isinstance(cold_order, dict):
        raise KvError(Code.V1_SCHEMA_INVALID, "v1 cold_order must be an object")

    candidates: List[Dict[str, Any]] = []
    conversions: List[Dict[str, Any]] = []
    for key, raw_threshold in thresholds.items():
        if not _valid_key(key):
            raise KvError(Code.V1_POLICY_KEY_INVALID, f"invalid v1 policy key: {key}")
        try:
            threshold = _positive_finite_number(raw_threshold)
        except Exception as exc:
            raise KvError(Code.V1_THRESHOLD_INVALID, f"v1 threshold for {key} must be finite and positive") from exc

        seconds = int(overrides.get(key, effective))
        absolute_ms = clock_ms + seconds * 1000
        conversion = {
            "policy_key": key,
            "source_format": "v1",
            "from_representation": "relative_effective_seconds",
            "original_value": seconds,
            "injected_clock_ms": clock_ms,
            "to_representation": "absolute_logical_milliseconds",
            "absolute_clock_ms": absolute_ms,
            "scale_ms_per_second": 1000,
            "rounding_direction": "exact",
            "discarded_remainder": 0,
        }
        conversions.append(conversion)
        candidates.append(
            {
                "policy_key": key,
                "source_format": "v1",
                "source_path": str(path),
                "source_version": payload.get("policy_set_id", "v1"),
                "generation": generation,
                "declared_clock_ms": absolute_ms,
                "original_time_repr": {"effective_after_seconds": seconds},
                "threshold_text": format(threshold, "f"),
                "cold_order": int(cold_order.get(key, 1000)),
                "format_priority": FORMAT_PRIORITY["v1"],
                "content_digest": canonical_hash({"key": key, "threshold": format(threshold, "f"), "generation": generation}),
            }
        )
    return candidates, conversions


def _validate_metadata_db(
    metadata_path: Path,
    manifest: Dict[str, Any],
    shard_records: List[Dict[str, Any]],
    tombstone_records: List[Dict[str, Any]],
) -> None:
    if not metadata_path.is_file():
        raise KvError(Code.V2_METADATA_MISSING, f"v2 metadata SQLite is missing: {metadata_path}")
    try:
        conn = sqlite3.connect(f"file:{metadata_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise KvError(Code.V2_METADATA_MISSING, f"cannot open v2 metadata: {exc}") from exc
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if {"shard_metadata", "policy_metadata"} - tables:
            raise KvError(Code.V2_METADATA_MISSING, "v2 metadata lacks shard_metadata or policy_metadata")

        for shard in manifest.get("shards", []):
            rel_path = shard["path"]
            expected_hash = shard["sha256"]
            row = conn.execute(
                "SELECT sha256, generation FROM shard_metadata WHERE path=?",
                (rel_path,),
            ).fetchone()
            if row is None:
                raise KvError(Code.V2_METADATA_HASH_MISMATCH, f"shard {rel_path} absent from SQLite metadata")
            if row[0] != expected_hash:
                raise KvError(
                    Code.V2_METADATA_HASH_MISMATCH,
                    f"SQLite hash differs from manifest for {rel_path}",
                    details={"manifest_hash": expected_hash, "sqlite_hash": row[0]},
                )

        for policy in shard_records:
            key = policy["policy_key"]
            generation = int(policy["generation"])
            row = conn.execute(
                "SELECT digest, absolute_clock_ms FROM policy_metadata WHERE policy_key=? AND generation=?",
                (key, generation),
            ).fetchone()
            if row is None:
                raise KvError(Code.V2_GENERATION_NONMONOTONIC, f"metadata missing current generation for {key} gen {generation}")
            if row[0] != manifest["key_digests"][key]:
                raise KvError(Code.V2_KEY_DIGEST_MISMATCH, f"policy key digest mismatch in SQLite for {key}")

            history = conn.execute(
                "SELECT generation FROM policy_metadata WHERE policy_key=? ORDER BY generation ASC",
                (key,),
            ).fetchall()
            gens = [int(item[0]) for item in history]
            if any(gens[index] >= gens[index + 1] for index in range(len(gens) - 1)):
                raise KvError(Code.V2_GENERATION_NONMONOTONIC, f"policy generations are not strictly increasing for {key}")
            if not gens or gens[-1] != generation:
                raise KvError(Code.V2_GENERATION_NONMONOTONIC, f"current shard generation is not metadata maximum for {key}")

        for tombstone in tombstone_records:
            key = tombstone["policy_key"]
            generation = int(tombstone["generation"])
            row = conn.execute(
                "SELECT digest, tombstone FROM policy_metadata WHERE policy_key=? AND generation=?",
                (key, generation),
            ).fetchone()
            if row is None or int(row[1]) != 1:
                raise KvError(Code.V2_SCHEMA_INVALID, f"tombstone missing in SQLite metadata: {key}")
            if row[0] != manifest["key_digests"][key]:
                raise KvError(Code.V2_KEY_DIGEST_MISMATCH, f"tombstone digest mismatch for {key}")
    except sqlite3.Error as exc:
        raise KvError(Code.V2_METADATA_MISSING, f"invalid v2 metadata SQLite: {exc}") from exc
    finally:
        conn.close()


def load_v2_package(
    package_dir: Path,
    clock_ms: int,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], str, List[Dict[str, Any]]]:
    manifest_path = package_dir / "manifest.json"
    manifest = read_json(manifest_path, parse_decimal=True)
    if not isinstance(manifest, dict) or manifest.get("format") != "v2-package":
        raise KvError(Code.V2_MANIFEST_INVALID, f"{manifest_path}: format must be v2-package")

    manifest_version = manifest.get("manifest_version")
    if not isinstance(manifest_version, int) or isinstance(manifest_version, bool) or manifest_version <= 0:
        raise KvError(Code.V2_MANIFEST_INVALID, "manifest_version must be a positive integer")
    snapshot_type = manifest.get("snapshot_type")
    if snapshot_type not in {"full", "incremental"}:
        raise KvError(Code.V2_MANIFEST_INVALID, "snapshot_type must be full or incremental")
    generation_floor = manifest.get("generation_floor")
    if not isinstance(generation_floor, int) or isinstance(generation_floor, bool) or generation_floor < 0:
        raise KvError(Code.V2_MANIFEST_INVALID, "generation_floor must be a nonnegative integer")
    declared_clock = manifest.get("declared_clock_ms")
    if not isinstance(declared_clock, int) or isinstance(declared_clock, bool) or declared_clock < 0:
        raise KvError(Code.V2_MANIFEST_INVALID, "declared_clock_ms must be nonnegative integer")

    shards = manifest.get("shards")
    key_digests = manifest.get("key_digests")
    if not isinstance(shards, list) or not shards or not isinstance(key_digests, dict):
        raise KvError(Code.V2_MANIFEST_INVALID, "shards must be nonempty and key_digests an object")

    base = package_dir.resolve()
    all_policies: List[Dict[str, Any]] = []
    tombstones: List[Dict[str, Any]] = []
    hash_inputs: List[Dict[str, Any]] = []
    for shard in shards:
        if not isinstance(shard, dict) or not isinstance(shard.get("path"), str) or not isinstance(shard.get("sha256"), str):
            raise KvError(Code.V2_MANIFEST_INVALID, "each shard needs path and sha256")
        rel_path = Path(shard["path"])
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise KvError(Code.V2_MANIFEST_INVALID, f"unsafe shard path: {shard['path']}")
        shard_path = (base / rel_path).resolve()
        try:
            shard_path.relative_to(base)
        except ValueError as exc:
            raise KvError(Code.V2_MANIFEST_INVALID, f"shard path escapes package: {rel_path}") from exc
        if not shard_path.is_file():
            raise KvError(Code.FILE_NOT_FOUND, f"v2 shard not found: {shard_path}")
        actual_hash = sha256_file(shard_path)
        if actual_hash != shard["sha256"]:
            raise KvError(
                Code.V2_SHARD_HASH_MISMATCH,
                f"shard hash mismatch for {rel_path}",
                details={"expected": shard["sha256"], "actual": actual_hash},
            )
        shard_payload = read_json(shard_path, parse_decimal=True)
        if not isinstance(shard_payload, dict) or not isinstance(shard_payload.get("policies"), list):
            raise KvError(Code.V2_SCHEMA_INVALID, f"invalid shard payload: {shard_path}")
        policies = shard_payload["policies"]
        raw_tombstones = shard_payload.get("tombstones", [])
        if not isinstance(raw_tombstones, list):
            raise KvError(Code.V2_SCHEMA_INVALID, "v2 tombstones must be a list")
        for raw in policies:
            if not isinstance(raw, dict):
                raise KvError(Code.V2_SCHEMA_INVALID, "v2 policy must be an object")
            key = raw.get("policy_key")
            if not _valid_key(key):
                raise KvError(Code.V2_SCHEMA_INVALID, f"invalid v2 policy_key: {key}")
            generation = raw.get("generation")
            abs_clock = raw.get("absolute_clock_ms")
            if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
                raise KvError(Code.V2_GENERATION_NONMONOTONIC, f"{key}: generation must be positive integer")
            if not isinstance(abs_clock, int) or isinstance(abs_clock, bool) or abs_clock < 0:
                raise KvError(Code.V2_SCHEMA_INVALID, f"{key}: absolute_clock_ms invalid")
            if generation < generation_floor:
                raise KvError(
                    Code.POLICY_GENERATION_FLOOR,
                    f"{key}: generation {generation} below manifest floor {generation_floor}",
                )
            try:
                threshold = _positive_finite_number(raw.get("threshold"))
            except Exception as exc:
                raise KvError(Code.V1_THRESHOLD_INVALID, f"v2 threshold for {key} invalid") from exc
            cold_order = raw.get("cold_order", 1000)
            if not isinstance(cold_order, int) or isinstance(cold_order, bool):
                raise KvError(Code.V2_SCHEMA_INVALID, f"{key}: cold_order must be integer")
            digest_payload = {
                "policy_key": key,
                "threshold": format(threshold, "f"),
                "cold_order": cold_order,
                "generation": generation,
            }
            digest = canonical_hash(digest_payload)
            if key_digests.get(key) != digest:
                raise KvError(Code.V2_KEY_DIGEST_MISMATCH, f"manifest key digest mismatch for {key}")
            all_policies.append(
                {
                    "policy_key": key,
                    "source_format": "v2",
                    "source_path": str(shard_path),
                    "source_version": f"v2-manifest-{manifest_version}",
                    "generation": generation,
                    "declared_clock_ms": abs_clock,
                    "original_time_repr": {"absolute_logical_ms": abs_clock},
                    "threshold_text": format(threshold, "f"),
                    "cold_order": int(cold_order),
                    "format_priority": FORMAT_PRIORITY["v2"],
                    "content_digest": digest,
                }
            )
        hash_inputs.append({"path": shard["path"], "sha256": actual_hash})
        for raw_tombstone in raw_tombstones:
            if not isinstance(raw_tombstone, dict):
                raise KvError(Code.V2_SCHEMA_INVALID, "v2 tombstone must be an object")
            key = raw_tombstone.get("policy_key")
            generation = raw_tombstone.get("generation")
            abs_clock = raw_tombstone.get("absolute_clock_ms")
            if not _valid_key(key):
                raise KvError(Code.V2_SCHEMA_INVALID, f"invalid tombstone key: {key}")
            if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
                raise KvError(Code.V2_GENERATION_NONMONOTONIC, f"tombstone {key} has invalid generation")
            if not isinstance(abs_clock, int) or isinstance(abs_clock, bool) or abs_clock < 0:
                raise KvError(Code.V2_SCHEMA_INVALID, f"tombstone {key} has invalid absolute_clock_ms")
            digest = canonical_hash(
                {"policy_key": key, "generation": generation, "tombstone": True}
            )
            if key_digests.get(key) != digest:
                raise KvError(Code.V2_KEY_DIGEST_MISMATCH, f"manifest tombstone digest mismatch for {key}")
            tombstones.append(
                {
                    "policy_key": key,
                    "generation": generation,
                    "absolute_clock_ms": abs_clock,
                    "digest": digest,
                }
            )

    metadata_path = package_dir / "metadata.sqlite"
    _validate_metadata_db(metadata_path, manifest, all_policies, tombstones)
    content_hash = canonical_hash(
        {
            "manifest_version": manifest_version,
            "generation_floor": generation_floor,
            "shards": hash_inputs,
            "key_digests": key_digests,
        }
    )
    return all_policies, manifest, content_hash, tombstones


def load_and_merge_package(package: str | Path, clock_ms: int | None = None) -> LoadedPolicyPackage:
    injected_clock = require_clock_ms(clock_ms)
    root = Path(package)
    if not root.exists():
        raise KvError(Code.FILE_NOT_FOUND, f"policy package does not exist: {root}")

    candidates: List[Dict[str, Any]] = []
    conversions: List[Dict[str, Any]] = []
    sources: List[str] = []
    tombstones: List[Dict[str, Any]] = []
    manifest_version: Optional[int] = None
    snapshot_type = "full"
    generation_floor = 0
    declared_clock = injected_clock
    content_parts: List[Any] = []

    v1_path = root / "v1" / "policy_v1.json"
    if not v1_path.is_file() and (root / "policy_v1.json").is_file():
        v1_path = root / "policy_v1.json"
    if v1_path.is_file():
        v1_candidates, v1_conversions = load_v1_policy(v1_path, injected_clock)
        candidates.extend(v1_candidates)
        conversions.extend(v1_conversions)
        sources.append("v1")
        content_parts.append({"v1_hash": sha256_file(v1_path)})

    v2_dir: Optional[Path] = None
    if (root / "v2" / "manifest.json").is_file():
        v2_dir = root / "v2"
    elif (root / "manifest.json").is_file():
        v2_dir = root
    if v2_dir is not None:
        v2_candidates, manifest, v2_hash, v2_tombstones = load_v2_package(v2_dir, injected_clock)
        candidates.extend(v2_candidates)
        manifest_version = int(manifest["manifest_version"])
        snapshot_type = str(manifest["snapshot_type"])
        generation_floor = int(manifest["generation_floor"])
        declared_clock = int(manifest["declared_clock_ms"])
        sources.append("v2")
        content_parts.append({"v2_hash": v2_hash})
        tombstones.extend(v2_tombstones)

    if not candidates:
        raise KvError(Code.POLICY_PACKAGE_INVALID, f"no v1 or v2 policies found under {root}")
    if manifest_version is None:
        manifest_version = 0
        snapshot_type = "v1-only"

    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in candidates:
        by_key.setdefault(candidate["policy_key"], []).append(candidate)

    entries: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    for key in sorted(by_key):
        versions = by_key[key]
        ordered = sorted(
            versions,
            key=lambda item: (
                int(item["declared_clock_ms"]),
                int(item["format_priority"]),
                int(item["generation"]),
            ),
            reverse=True,
        )
        winner = ordered[0]
        losers = ordered[1:]
        decision = {
            "policy_key": key,
            "adjudication_rule": "absolute_clock_ms DESC, format_priority DESC, generation DESC",
            "winner_source": winner["source_format"],
            "winner_clock_ms": winner["declared_clock_ms"],
            "winner_generation": winner["generation"],
            "rejected_sources": [
                {
                    "source_format": loser["source_format"],
                    "declared_clock_ms": loser["declared_clock_ms"],
                    "generation": loser["generation"],
                    "reason": (
                        "equal_clock_lower_format_priority"
                        if loser["declared_clock_ms"] == winner["declared_clock_ms"]
                        else "older_declared_logical_clock"
                    ),
                }
                for loser in losers
            ],
        }
        decisions.append(decision)
        entry = dict(winner)
        entry["layer_id"] = key.split(":", 1)[1] if key.startswith("layer:") else key
        entry["rejected_sources"] = decision["rejected_sources"]
        entries.append(entry)

    content_hash = canonical_hash(content_parts)
    return LoadedPolicyPackage(
        entries=entries,
        decisions=decisions,
        time_conversions=conversions,
        manifest_version=manifest_version,
        snapshot_type=snapshot_type,
        generation_floor=generation_floor,
        declared_clock_ms=declared_clock,
        content_hash=content_hash,
        sources=sorted(set(sources)),
        tombstones=tombstones,
    )
