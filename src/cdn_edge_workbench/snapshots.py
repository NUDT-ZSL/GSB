from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .index import arbitrate_snapshot_entries
from .models import CacheEntry, ValidatedSamples
from .validation import (
    enforce_cache_generation_monotonic,
    load_injections,
    load_json_file,
    load_nodes,
    load_origin_manifest,
    load_policy,
    normalize_digest,
    normalize_path,
    parse_etag,
    safe_relative_path,
    sha256_hex,
)


def _required_int(
    value: Any,
    field: str,
    *,
    code: ErrorCode = ErrorCode.SCHEMA_INVALID,
    minimum: int = 1,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        comparator = "positive" if minimum == 1 else f">= {minimum}"
        raise BusinessError(
            code,
            f"{field} must be an integer {comparator}",
            details={"field": field, "value": value},
        )
    return value


def _to_ticks(raw: Any, scale: int, rounding: str, field: str) -> tuple[int, dict[str, Any]]:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            f"{field} must be a finite relative-seconds number",
            details={"field": field, "value": raw},
        )
    try:
        decimal_raw = Decimal(str(raw))
    except Exception as exc:
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            f"{field} is not a valid decimal value",
            details={"field": field, "value": raw},
        ) from exc
    if not decimal_raw.is_finite():
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            f"{field} must be finite",
            details={"field": field, "value": raw},
        )
    quotient = decimal_raw / Decimal(scale)
    mode = {
        "ceil": ROUND_CEILING,
        "floor": ROUND_FLOOR,
        "half_up": ROUND_HALF_UP,
        "nearest": ROUND_HALF_UP,
    }.get(rounding)
    if mode is None:
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            f"unsupported rounding mode: {rounding}",
            details={"rounding": rounding},
        )
    ticks = int(quotient.to_integral_value(rounding=mode))
    return ticks, {
        "raw": raw,
        "scale_seconds_per_tick": scale,
        "rounding": rounding,
        "unrounded_ticks": str(quotient),
        "converted_ticks": ticks,
    }


def _entry_from_v1(
    item: dict[str, Any],
    *,
    node_hint: str | None,
    known_nodes: set[str],
    base_clock: int,
    scale: int,
    rounding: str,
    source_ref: str,
    default_algorithm: str,
) -> CacheEntry:
    if not isinstance(item, dict):
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "v1 entry must be an object")
    node = node_hint if node_hint is not None else item.get("node")
    if not isinstance(node, str) or node not in known_nodes:
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            f"v1 entry references unknown node: {node!r}",
            node=node if isinstance(node, str) else None,
            details={"entry": item},
        )
    path = normalize_path(item.get("path"))
    version = _required_int(
        item.get("content_version", item.get("version")),
        f"content_version for {node}:{path}",
        code=ErrorCode.VERSION_INVALID,
    )
    generation = _required_int(
        item.get("cache_generation", item.get("generation")),
        f"cache_generation for {node}:{path}",
    )
    algorithm = item.get(
        "digest_algorithm",
        item.get("etag_algorithm", default_algorithm),
    )
    if not isinstance(algorithm, str):
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "digest_algorithm must be a string")
    digest_value = item.get("content_digest", item.get("digest"))
    algorithm, digest, etag, weak = parse_etag(
        item.get("etag"),
        declared_algorithm=algorithm,
        expected_digest=digest_value,
    )
    age_raw = item.get(
        "last_verified_age_seconds",
        item.get("age_seconds", item.get("last_checked_seconds_ago")),
    )
    ttl_raw = item.get(
        "ttl_remaining_seconds",
        item.get("expires_in_seconds", item.get("remaining_seconds")),
    )
    if age_raw is None or ttl_raw is None:
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            "v1 entries require relative last-verification age and TTL seconds",
            path=path,
            node=node,
        )
    age_ticks, age_conversion = _to_ticks(age_raw, scale, rounding, "last_verified_age_seconds")
    ttl_ticks, ttl_conversion = _to_ticks(ttl_raw, scale, rounding, "ttl_remaining_seconds")
    last_verified = base_clock - age_ticks
    expires_at = base_clock + ttl_ticks
    cacheable = item.get("cacheable", True)
    if not isinstance(cacheable, bool):
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "cacheable must be boolean")
    effective_ttl_seconds = item.get("effective_ttl_seconds")
    if effective_ttl_seconds is None:
        effective_ttl_seconds = max(0, expires_at - last_verified) * scale
    effective_ttl_seconds = _required_int(
        effective_ttl_seconds,
        "effective_ttl_seconds",
        minimum=0,
    )
    raw_time = {
        "kind": "relative_seconds",
        "base_logical_clock": base_clock,
        "rounding": rounding,
        "scale_seconds_per_tick": scale,
        "last_verified_conversion": age_conversion,
        "ttl_conversion": ttl_conversion,
        "converted": {
            "last_verified_clock": last_verified,
            "expires_at_clock": expires_at,
        },
        "raw_entry_time_fields": {
            "last_verified_age_seconds": age_raw,
            "ttl_remaining_seconds": ttl_raw,
        },
    }
    return CacheEntry(
        node=node,
        path=path,
        digest_algorithm=algorithm,
        content_digest=digest,
        content_version=version,
        etag=etag,
        last_verified_clock=last_verified,
        expires_at_clock=expires_at,
        cache_generation=generation,
        source_version="v1",
        source_ref=source_ref,
        raw_time=raw_time,
        effective_ttl_seconds=effective_ttl_seconds,
        cacheable=cacheable,
        weak_etag=weak,
    )


def load_v1_snapshot(
    path: str | Path,
    *,
    known_nodes: set[str],
) -> tuple[int, int, list[CacheEntry]]:
    payload = load_json_file(path, stage="parse")
    if not isinstance(payload, dict):
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "v1 snapshot root must be an object")
    generation = _required_int(payload.get("snapshot_generation", 1), "v1 snapshot_generation")
    base_clock = _required_int(
        payload.get("logical_clock"),
        "v1 logical_clock",
        minimum=0,
    )
    time_semantics = payload.get("time_semantics", payload.get("time", {}))
    if not isinstance(time_semantics, dict):
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "v1 time_semantics must be an object")
    kind = time_semantics.get("kind", "relative_seconds")
    if kind != "relative_seconds":
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            "v1 time_semantics.kind must be relative_seconds",
            details={"kind": kind},
        )
    scale = _required_int(
        time_semantics.get("scale_seconds_per_tick", time_semantics.get("time_unit_seconds", 1)),
        "v1 scale_seconds_per_tick",
    )
    rounding = time_semantics.get("rounding", "ceil")
    if rounding not in {"ceil", "floor", "half_up", "nearest"}:
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            "v1 rounding must be ceil, floor, or half_up",
            details={"rounding": rounding},
        )
    raw_entries: list[tuple[dict[str, Any], str | None]] = []
    if "entries" in payload:
        entries_value = payload["entries"]
        if not isinstance(entries_value, list):
            raise BusinessError(ErrorCode.SCHEMA_INVALID, "v1 entries must be a list")
        raw_entries.extend((item, None) for item in entries_value)
    elif "nodes" in payload:
        nodes_value = payload["nodes"]
        if not isinstance(nodes_value, dict):
            raise BusinessError(ErrorCode.SCHEMA_INVALID, "v1 nodes must be an object")
        for node, items in nodes_value.items():
            if not isinstance(items, list):
                raise BusinessError(ErrorCode.SCHEMA_INVALID, "v1 node entries must be a list")
            raw_entries.extend((item, node) for item in items)
    else:
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "v1 snapshot must contain entries or nodes")
    source_ref = Path(path).name
    default_algorithm = payload.get("digest_algorithm", "sha256")
    entries: list[CacheEntry] = []
    seen: set[tuple[str, str]] = set()
    for item, node_hint in raw_entries:
        entry = _entry_from_v1(
            item,
            node_hint=node_hint,
            known_nodes=known_nodes,
            base_clock=base_clock,
            scale=scale,
            rounding=rounding,
            source_ref=source_ref,
            default_algorithm=default_algorithm,
        )
        if entry.key in seen:
            raise BusinessError(
                ErrorCode.DUPLICATE_KEY,
                f"duplicate v1 cache key: {entry.key_text}",
                path=entry.path,
                node=entry.node,
            )
        seen.add(entry.key)
        entries.append(entry)
    return generation, base_clock, entries


def _read_v2_metadata(path: Path) -> tuple[int, int, list[dict[str, Any]]]:
    suffix = path.suffix.lower()
    if suffix in {".sqlite", ".sqlite3", ".db"}:
        uri = f"file:{path.resolve()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM metadata")
            }
            shard_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT shard_file, sha256, entry_count, snapshot_generation, shard_index
                    FROM shards
                    ORDER BY shard_index
                    """
                )
            ]
            connection.close()
        except sqlite3.Error as exc:
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                f"cannot read v2 SQLite metadata: {exc}",
                details={"file": str(path)},
            ) from exc
        try:
            generation = int(metadata["snapshot_generation"])
            logical_clock = int(metadata["logical_clock"])
        except (KeyError, ValueError) as exc:
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                "v2 metadata is missing snapshot_generation or logical_clock",
                details={"file": str(path)},
            ) from exc
        return generation, logical_clock, shard_rows
    payload = load_json_file(path, stage="parse")
    if not isinstance(payload, dict):
        raise BusinessError(ErrorCode.SHARD_METADATA_INVALID, "v2 metadata root must be an object")
    generation = _required_int(payload.get("snapshot_generation"), "v2 snapshot_generation")
    logical_clock = _required_int(payload.get("logical_clock"), "v2 logical_clock", minimum=0)
    shards = payload.get("shards")
    if not isinstance(shards, list):
        raise BusinessError(ErrorCode.SHARD_METADATA_INVALID, "v2 metadata shards must be a list")
    return generation, logical_clock, [dict(item) for item in shards]


def _entry_from_v2(
    item: dict[str, Any],
    *,
    known_nodes: set[str],
    snapshot_generation: int,
    source_ref: str,
    default_algorithm: str,
) -> CacheEntry:
    if not isinstance(item, dict):
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "v2 entry must be an object")
    node = item.get("node")
    if not isinstance(node, str) or node not in known_nodes:
        raise BusinessError(
            ErrorCode.SCHEMA_INVALID,
            f"v2 entry references unknown node: {node!r}",
            node=node if isinstance(node, str) else None,
        )
    path = normalize_path(item.get("path"))
    version = _required_int(
        item.get("content_version", item.get("version")),
        f"content_version for {node}:{path}",
        code=ErrorCode.VERSION_INVALID,
    )
    generation = _required_int(
        item.get("cache_generation", item.get("generation")),
        f"cache_generation for {node}:{path}",
    )
    algorithm = item.get(
        "digest_algorithm",
        item.get("etag_algorithm", default_algorithm),
    )
    if not isinstance(algorithm, str):
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "digest_algorithm must be a string")
    digest_value = item.get("content_digest", item.get("digest"))
    algorithm, digest, etag, weak = parse_etag(
        item.get("etag"),
        declared_algorithm=algorithm,
        expected_digest=digest_value,
    )
    last_verified = _required_int(
        item.get("last_verified_clock"),
        f"last_verified_clock for {node}:{path}",
        minimum=0,
    )
    expires_at = _required_int(
        item.get("expires_at_clock"),
        f"expires_at_clock for {node}:{path}",
        minimum=0,
    )
    cacheable = item.get("cacheable", True)
    if not isinstance(cacheable, bool):
        raise BusinessError(ErrorCode.SCHEMA_INVALID, "cacheable must be boolean")
    effective_ttl_seconds = item.get(
        "effective_ttl_seconds",
        max(0, expires_at - last_verified),
    )
    effective_ttl_seconds = _required_int(
        effective_ttl_seconds,
        "effective_ttl_seconds",
        minimum=0,
    )
    raw_time = {
        "kind": "absolute_logical_clock",
        "snapshot_generation": snapshot_generation,
        "snapshot_logical_clock": item.get("snapshot_logical_clock"),
        "raw_entry_time_fields": {
            "last_verified_clock": last_verified,
            "expires_at_clock": expires_at,
        },
        "converted": {
            "last_verified_clock": last_verified,
            "expires_at_clock": expires_at,
        },
    }
    return CacheEntry(
        node=node,
        path=path,
        digest_algorithm=algorithm,
        content_digest=digest,
        content_version=version,
        etag=etag,
        last_verified_clock=last_verified,
        expires_at_clock=expires_at,
        cache_generation=generation,
        source_version="v2",
        source_ref=source_ref,
        raw_time=raw_time,
        effective_ttl_seconds=effective_ttl_seconds,
        cacheable=cacheable,
        weak_etag=weak,
    )


def load_v2_snapshot(
    metadata_path: str | Path,
    *,
    sample_root: str | Path,
    known_nodes: set[str],
) -> tuple[int, int, list[CacheEntry]]:
    metadata_path = Path(metadata_path)
    generation, logical_clock, shard_rows = _read_v2_metadata(metadata_path)
    if not shard_rows:
        raise BusinessError(ErrorCode.SHARD_METADATA_INVALID, "v2 metadata declares no shards")
    seen_files: set[str] = set()
    seen_indices: set[int] = set()
    entries: list[CacheEntry] = []
    seen_keys: set[tuple[str, str]] = set()
    for row in shard_rows:
        shard_file = row.get("shard_file", row.get("file", row.get("file_path")))
        if not isinstance(shard_file, str):
            raise BusinessError(ErrorCode.SHARD_METADATA_INVALID, "shard file path must be a string")
        if shard_file in seen_files:
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                f"duplicate shard file reference: {shard_file}",
            )
        seen_files.add(shard_file)
        expected_hash = row.get("sha256", row.get("hash"))
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                f"invalid shard hash for {shard_file}",
            )
        shard_index = _required_int(row.get("shard_index"), "v2 shard_index", minimum=0)
        if shard_index in seen_indices:
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                f"duplicate shard index: {shard_index}",
            )
        seen_indices.add(shard_index)
        row_generation = _required_int(
            row.get("snapshot_generation", generation),
            "v2 shard snapshot_generation",
        )
        if row_generation != generation:
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                "shard generation does not match v2 metadata generation",
                details={
                    "shard_generation": row_generation,
                    "metadata_generation": generation,
                },
            )
        shard_path = safe_relative_path(sample_root, shard_file)
        try:
            shard_bytes = shard_path.read_bytes()
        except OSError as exc:
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                f"cannot read shard {shard_file}: {exc}",
            ) from exc
        actual_hash = hashlib.sha256(shard_bytes).hexdigest()
        if not hmac.compare_digest(actual_hash, expected_hash.lower()):
            raise BusinessError(
                ErrorCode.SHARD_HASH_MISMATCH,
                f"shard hash mismatch for {shard_file}",
                details={
                    "expected": expected_hash.lower(),
                    "actual": actual_hash,
                    "shard": shard_file,
                },
            )
        try:
            shard_payload = json.loads(shard_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                ErrorCode.INVALID_JSON,
                f"invalid shard JSON: {shard_file}",
                details={"shard": shard_file},
            ) from exc
        if not isinstance(shard_payload, dict):
            raise BusinessError(ErrorCode.SCHEMA_INVALID, "v2 shard root must be an object")
        if shard_payload.get("format", "v2-shard") not in {"v2-shard", "v2"}:
            raise BusinessError(ErrorCode.SCHEMA_INVALID, "unsupported v2 shard format")
        if shard_payload.get("snapshot_generation") != generation:
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                "shard payload generation mismatch",
                details={"shard": shard_file},
            )
        if shard_payload.get("shard_index") != shard_index:
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                "shard payload index mismatch",
                details={"shard": shard_file},
            )
        raw_entries = shard_payload.get("entries")
        if not isinstance(raw_entries, list):
            raise BusinessError(ErrorCode.SCHEMA_INVALID, "v2 shard entries must be a list")
        expected_count = row.get("entry_count")
        if expected_count is not None and (
            isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or expected_count != len(raw_entries)
        ):
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                "shard entry count does not match metadata",
                details={"shard": shard_file},
            )
        for item in raw_entries:
            entry = _entry_from_v2(
                item,
                known_nodes=known_nodes,
                snapshot_generation=generation,
                source_ref=shard_file,
                default_algorithm=shard_payload.get("digest_algorithm", "sha256"),
            )
            if entry.key in seen_keys:
                raise BusinessError(
                    ErrorCode.DUPLICATE_KEY,
                    f"duplicate v2 cache key: {entry.key_text}",
                    path=entry.path,
                    node=entry.node,
                )
            seen_keys.add(entry.key)
            entries.append(entry)
    return generation, logical_clock, entries


def load_samples(
    samples_dir: str | Path,
    *,
    policy_file: str = "policy.json",
    nodes_file: str = "nodes.json",
    origin_file: str = "origin_manifest.json",
    v1_file: str = "v1_snapshot.json",
    v2_metadata_file: str | None = None,
    injections_file: str = "injections.json",
) -> ValidatedSamples:
    root = Path(samples_dir).resolve()
    if not root.is_dir():
        raise BusinessError(
            ErrorCode.INVALID_JSON,
            f"samples directory does not exist: {root}",
        )
    policy_path = safe_relative_path(root, policy_file)
    nodes_path = safe_relative_path(root, nodes_file)
    origin_path = safe_relative_path(root, origin_file)
    v1_path = safe_relative_path(root, v1_file)
    injections_path = safe_relative_path(root, injections_file)
    if v2_metadata_file is None:
        sqlite_candidate = root / "v2_meta.sqlite"
        json_candidate = root / "v2_meta.json"
        if sqlite_candidate.exists():
            v2_metadata_file = "v2_meta.sqlite"
        elif json_candidate.exists():
            v2_metadata_file = "v2_meta.json"
        else:
            raise BusinessError(
                ErrorCode.SHARD_METADATA_INVALID,
                "no v2 metadata file found in samples directory",
            )
    metadata_path = safe_relative_path(root, v2_metadata_file)
    policy = load_policy(policy_path)
    nodes = load_nodes(nodes_path)
    resources = load_origin_manifest(origin_path)
    known_nodes = set(nodes)
    v1_generation, _v1_clock, v1_entries = load_v1_snapshot(
        v1_path,
        known_nodes=known_nodes,
    )
    v2_generation, _v2_clock, v2_entries = load_v2_snapshot(
        metadata_path,
        sample_root=root,
        known_nodes=known_nodes,
    )
    if v2_generation <= v1_generation:
        raise BusinessError(
            ErrorCode.CACHE_GENERATION_ROLLBACK,
            "v2 snapshot generation must be greater than v1",
            details={"v1_generation": v1_generation, "v2_generation": v2_generation},
        )
    enforce_cache_generation_monotonic(v1_entries, v2_entries)
    candidates = v1_entries + v2_entries
    known_paths = set(resources) | {entry.path for entry in candidates}
    injections = load_injections(
        injections_path,
        known_paths=known_paths,
        known_nodes=known_nodes,
    )
    snapshot_arbitrations = arbitrate_snapshot_entries(candidates)
    source_files = {
        "policy": policy_file,
        "nodes": nodes_file,
        "origin_manifest": origin_file,
        "v1_snapshot": v1_file,
        "v2_metadata": v2_metadata_file,
        "injections": injections_file,
    }
    return ValidatedSamples(
        samples_dir=str(root),
        policy=policy,
        nodes=nodes,
        resources=resources,
        injections=injections,
        snapshot_candidates=candidates,
        snapshot_arbitrations=snapshot_arbitrations,
        snapshot_generation=v2_generation,
        v1_generation=v1_generation,
        v2_generation=v2_generation,
        source_files=source_files,
    )

