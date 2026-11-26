from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

from .errors import EngineError, ErrorCode
from .hashing import is_sha256_digest, sha256_hex, sha256_object


CONTENT_RESULTS = {"hit", "miss", "stale", "revalidated", "digest_mismatch"}
EMPTY_BODY_RESULTS = {"connection_refused", "timeout", "error"}
KNOWN_RESULTS = CONTENT_RESULTS | EMPTY_BODY_RESULTS | {"pending"}


def read_json(path: str | Path) -> Any:
    path = Path(path)
    if not path.exists():
        raise EngineError(
            ErrorCode.INPUT_NOT_FOUND,
            f"input file does not exist: {path}",
            details={"path": str(path)},
        )
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise EngineError(
            ErrorCode.INVALID_JSON,
            f"invalid JSON in {path}: {exc.msg}",
            details={"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _rule_name(rule: Dict[str, Any], *, version: str) -> str:
    for key in ("name", "rule_name", "canonical_name", "spec_name"):
        value = rule.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise EngineError(
        ErrorCode.SCHEMA_VIOLATION,
        f"{version} rule is missing a non-empty name",
        details={"rule": rule},
    )


def _dependencies(rule: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    for key in ("dependencies", "depends_on", "upstream_rules", "upstreams"):
        for item in _as_list(rule.get(key)):
            if not isinstance(item, str) or not item.strip():
                raise EngineError(
                    ErrorCode.SCHEMA_VIOLATION,
                    "dependency names must be non-empty strings",
                    details={"field": key, "value": item},
                )
            values.append(item.strip())
    return sorted(set(values))


def parse_v1_rules(path: str | Path, default_clock: Optional[int] = None) -> Dict[str, Any]:
    payload = read_json(path)
    if isinstance(payload, list):
        document: Dict[str, Any] = {"rules": payload}
    elif isinstance(payload, dict):
        document = payload
    else:
        raise EngineError(ErrorCode.SCHEMA_VIOLATION, "v1 rules document must be an object or array")

    raw_rules = document.get("rules", document.get("rule_list", []))
    if not isinstance(raw_rules, list):
        raise EngineError(ErrorCode.SCHEMA_VIOLATION, "v1 rules must be a list")

    seen: Set[str] = set()
    normalized: List[Dict[str, Any]] = []
    previous_generation: Optional[int] = None

    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise EngineError(
                ErrorCode.SCHEMA_VIOLATION,
                "each v1 rule must be an object",
                details={"index": index},
            )
        name = _rule_name(raw, version="v1")
        if name in seen:
            raise EngineError(
                ErrorCode.DUPLICATE_RULE_NAME,
                f"duplicate v1 rule name: {name}",
                details={"name": name, "index": index},
            )
        seen.add(name)

        generation = raw.get("generation", raw.get("rule_generation", raw.get("algebra")))
        if not _is_int(generation) or generation < 1:
            raise EngineError(
                ErrorCode.SCHEMA_VIOLATION,
                "v1 generation must be a positive integer",
                details={"name": name, "generation": generation},
            )
        if previous_generation is not None and generation < previous_generation:
            raise EngineError(
                ErrorCode.GENERATION_NOT_MONOTONIC,
                "v1 rule generations must be non-decreasing in declared order",
                details={
                    "name": name,
                    "generation": generation,
                    "previous_generation": previous_generation,
                },
            )
        previous_generation = generation

        relative_seconds = raw.get(
            "relative_effective_seconds",
            raw.get("effective_after_seconds", raw.get("relative_effective_sec")),
        )
        if not _is_int(relative_seconds) or relative_seconds < 0:
            raise EngineError(
                ErrorCode.INVALID_EFFECTIVE_SECONDS,
                "v1 relative_effective_seconds must be a non-negative integer",
                details={"name": name, "value": relative_seconds},
            )

        declared_clock = raw.get(
            "logical_clock_ms",
            raw.get("declared_clock_ms", document.get("logical_clock_ms", default_clock)),
        )
        if not _is_int(declared_clock) or declared_clock < 0:
            raise EngineError(
                ErrorCode.SCHEMA_VIOLATION,
                "v1 declared logical clock must be a non-negative integer",
                details={"name": name, "value": declared_clock},
            )

        verify_type = raw.get(
            "origin_verify_type",
            raw.get("verify_type", raw.get("revalidation_type", "status")),
        )
        if not isinstance(verify_type, str) or not verify_type.strip():
            raise EngineError(ErrorCode.SCHEMA_VIOLATION, "verify_type must be a string", details={"name": name})

        dependencies = _dependencies(raw)
        normalized.append(
            {
                "name": name,
                "version": "v1",
                "source_format": "v1_single_json",
                "source_priority": 10,
                "source_file": str(path),
                "generation": generation,
                "declared_clock_ms": declared_clock,
                "relative_effective_seconds": relative_seconds,
                "original_time": {
                    "type": "relative_effective_seconds",
                    "value": relative_seconds,
                },
                "verify_type": verify_type.strip(),
                "dependencies": dependencies,
                "forward_dependency": raw.get("forward_dependency"),
                "content_digest": raw.get("content_digest"),
                "raw": raw,
            }
        )

    flat_edges: List[Tuple[str, str]] = []
    mapping = document.get("forward_dependencies", document.get("flat_dependency_map", {}))
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, dict):
        raise EngineError(ErrorCode.SCHEMA_VIOLATION, "v1 forward_dependencies must be an object")
    for source, target_value in mapping.items():
        targets = _as_list(target_value)
        for target in targets:
            if not isinstance(target, str) or not target.strip():
                raise EngineError(
                    ErrorCode.SCHEMA_VIOLATION,
                    "flat dependency targets must be rule-name strings",
                    details={"source": source, "target": target},
                )
            flat_edges.append((source.strip(), target.strip()))

    names = {rule["name"] for rule in normalized}
    for source, target in flat_edges:
        if source not in names:
            raise EngineError(
                ErrorCode.DANGLING_DEPENDENCY,
                "v1 flat dependency source is not a declared rule",
                details={"source": source, "target": target},
            )
        if target not in names:
            raise EngineError(
                ErrorCode.DANGLING_DEPENDENCY,
                "v1 flat dependency target is not a declared rule",
                details={"source": source, "target": target},
            )
        for rule in normalized:
            if rule["name"] == source and target not in rule["dependencies"]:
                rule["dependencies"].append(target)
                rule["dependencies"].sort()

    return {
        "format": "v1_single_json",
        "path": str(path),
        "rules": normalized,
        "forward_edges": [{"source": s, "target": t} for s, t in flat_edges],
        "document_clock_ms": document.get("logical_clock_ms", default_clock),
    }


def _sqlite_metadata(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise EngineError(
            ErrorCode.INPUT_NOT_FOUND,
            f"v2 SQLite metadata does not exist: {path}",
            details={"path": str(path)},
        )
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        result: Dict[str, Any] = {"tables": sorted(tables)}
        if "package_manifest" in tables:
            result["manifest"] = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM package_manifest")
            }
        else:
            result["manifest"] = {}
        if "shard_hashes" in tables:
            result["shard_hashes"] = {
                row["shard_path"]: row["sha256"]
                for row in connection.execute("SELECT shard_path, sha256 FROM shard_hashes")
            }
        else:
            result["shard_hashes"] = {}
        if "tombstones" in tables:
            result["tombstones"] = [
                {key: row[key] for key in row.keys()}
                for row in connection.execute(
                    "SELECT rule_name, reason, logical_clock_ms FROM tombstones ORDER BY rule_name"
                )
            ]
        else:
            result["tombstones"] = []
        connection.close()
        return result
    except sqlite3.DatabaseError as exc:
        raise EngineError(
            ErrorCode.INVALID_SQLITE_METADATA,
            f"cannot read v2 SQLite metadata: {exc}",
            details={"path": str(path)},
        ) from exc


def parse_v2_package(
    package_dir: str | Path,
    *,
    metadata_path: Optional[str | Path] = None,
    default_clock: Optional[int] = None,
) -> Dict[str, Any]:
    package_dir = Path(package_dir)
    shard_dir = package_dir / "shards"
    if not shard_dir.exists():
        shard_dir = package_dir / "v2" / "shards"
    if not shard_dir.exists():
        raise EngineError(
            ErrorCode.INPUT_NOT_FOUND,
            f"v2 shard directory does not exist: {shard_dir}",
            details={"package_dir": str(package_dir)},
        )
    shard_paths = sorted(path for path in shard_dir.glob("*.json") if path.is_file())
    if not shard_paths:
        raise EngineError(ErrorCode.INPUT_NOT_FOUND, "v2 shard directory contains no JSON files")

    meta_path = Path(metadata_path) if metadata_path else package_dir / "metadata.sqlite"
    if not meta_path.exists():
        candidate = package_dir / "v2" / "metadata.sqlite"
        if candidate.exists():
            meta_path = candidate
    metadata = _sqlite_metadata(meta_path)
    db_hashes: Dict[str, str] = metadata["shard_hashes"]

    normalized: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    previous_generation: Optional[int] = None
    shard_records: List[Dict[str, Any]] = []

    for shard_path in shard_paths:
        relative_name = shard_path.name
        actual_hash = sha256_hex(shard_path.read_bytes())
        expected_hash = db_hashes.get(relative_name) or db_hashes.get(str(shard_path.relative_to(package_dir)))
        if expected_hash is None:
            raise EngineError(
                ErrorCode.SHARD_HASH_MISMATCH,
                "shard is missing from SQLite shard_hashes",
                details={"shard": relative_name, "actual_sha256": actual_hash},
            )
        if expected_hash != actual_hash:
            raise EngineError(
                ErrorCode.SHARD_HASH_MISMATCH,
                "v2 shard hash does not match SQLite metadata",
                details={
                    "shard": relative_name,
                    "expected_sha256": expected_hash,
                    "actual_sha256": actual_hash,
                },
            )

        payload = read_json(shard_path)
        if isinstance(payload, dict):
            raw_rules = payload.get("rules", payload.get("canonical_rules", []))
            shard_name = payload.get("shard", relative_name)
        elif isinstance(payload, list):
            raw_rules = payload
            shard_name = relative_name
        else:
            raise EngineError(ErrorCode.SCHEMA_VIOLATION, "v2 shard must contain a list or object")
        if not isinstance(raw_rules, list):
            raise EngineError(ErrorCode.SCHEMA_VIOLATION, "v2 shard rules must be a list")

        shard_records.append({"shard": relative_name, "sha256": actual_hash, "rule_count": len(raw_rules)})
        for index, raw in enumerate(raw_rules):
            if not isinstance(raw, dict):
                raise EngineError(ErrorCode.SCHEMA_VIOLATION, "each v2 rule must be an object")
            name = _rule_name(raw, version="v2")
            if name in seen:
                raise EngineError(
                    ErrorCode.DUPLICATE_RULE_NAME,
                    f"duplicate v2 canonical rule name: {name}",
                    details={"name": name},
                )
            seen.add(name)

            generation = raw.get(
                "cache_generation",
                raw.get("generation", raw.get("rule_generation")),
            )
            if not _is_int(generation) or generation < 1:
                raise EngineError(
                    ErrorCode.SCHEMA_VIOLATION,
                    "v2 cache_generation must be a positive integer",
                    details={"name": name, "generation": generation},
                )
            if previous_generation is not None and generation < previous_generation:
                raise EngineError(
                    ErrorCode.GENERATION_NOT_MONOTONIC,
                    "v2 rule generations must be non-decreasing in shard order",
                    details={"name": name, "generation": generation},
                )
            previous_generation = generation

            effective_at_ms = raw.get("effective_at_ms", raw.get("absolute_effective_ms"))
            if not _is_int(effective_at_ms) or effective_at_ms < 0:
                raise EngineError(
                    ErrorCode.INVALID_EFFECTIVE_MS,
                    "v2 effective_at_ms must be a non-negative integer",
                    details={"name": name, "value": effective_at_ms},
                )

            declared_clock = raw.get(
                "logical_clock_ms",
                raw.get("declared_clock_ms", metadata["manifest"].get("declared_clock_ms", default_clock)),
            )
            if isinstance(declared_clock, str) and declared_clock.isdigit():
                declared_clock = int(declared_clock)
            if not _is_int(declared_clock) or declared_clock < 0:
                raise EngineError(
                    ErrorCode.SCHEMA_VIOLATION,
                    "v2 declared logical clock must be a non-negative integer",
                    details={"name": name, "value": declared_clock},
                )

            tombstone = bool(raw.get("tombstone", raw.get("is_tombstone", False)))
            dependencies = _dependencies(raw)
            normalized.append(
                {
                    "name": name,
                    "version": "v2",
                    "source_format": "v2_sharded_sqlite",
                    "source_priority": 20,
                    "source_file": relative_name,
                    "generation": generation,
                    "declared_clock_ms": declared_clock,
                    "effective_at_ms": effective_at_ms,
                    "original_time": {"type": "absolute_effective_at_ms", "value": effective_at_ms},
                    "verify_type": raw.get("verify_type", raw.get("origin_verify_type", "status")),
                    "dependencies": dependencies,
                    "tombstone": tombstone,
                    "content_digest": raw.get("content_digest"),
                    "raw": raw,
                }
            )

    for tombstone_row in metadata["tombstones"]:
        name = tombstone_row.get("rule_name")
        if name not in seen:
            raise EngineError(
                ErrorCode.INVALID_TOMBSTONE,
                "tombstone references a rule absent from v2 shards",
                details=tombstone_row,
            )

    db_name_set = set(db_hashes.keys())
    file_name_set = {path.name for path in shard_paths}
    if db_name_set != file_name_set:
        raise EngineError(
            ErrorCode.SHARD_HASH_MISMATCH,
            "SQLite shard hash set does not match shard files",
            details={
                "database_only": sorted(db_name_set - file_name_set),
                "file_only": sorted(file_name_set - db_name_set),
            },
        )

    ruleset_digest = sha256_object(
        [
            {
                "name": rule["name"],
                "generation": rule["generation"],
                "effective_at_ms": rule["effective_at_ms"],
                "dependencies": rule["dependencies"],
                "tombstone": rule.get("tombstone", False),
                "content_digest": rule.get("content_digest"),
            }
            for rule in normalized
        ]
    )
    manifest = metadata["manifest"]
    expected_ruleset_digest = manifest.get("ruleset_digest")
    if expected_ruleset_digest and expected_ruleset_digest != ruleset_digest:
        raise EngineError(
            ErrorCode.SHARD_HASH_MISMATCH,
            "v2 rule-set digest does not match SQLite package_manifest",
            details={
                "expected": expected_ruleset_digest,
                "actual": ruleset_digest,
            },
        )

    return {
        "format": "v2_sharded_sqlite",
        "package_dir": str(package_dir),
        "metadata_path": str(meta_path),
        "manifest": manifest,
        "rules": normalized,
        "shards": shard_records,
        "tombstones": metadata["tombstones"],
        "ruleset_digest": ruleset_digest,
    }


def parse_nodes(path: str | Path) -> Dict[str, Any]:
    payload = read_json(path)
    raw_nodes = payload.get("nodes", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise EngineError(ErrorCode.SCHEMA_VIOLATION, "edge node manifest must contain a non-empty nodes list")
    seen: Set[str] = set()
    nodes: List[Dict[str, Any]] = []
    for raw in raw_nodes:
        name = raw.get("name") or raw.get("node_name")
        if not isinstance(name, str) or not name.strip():
            raise EngineError(ErrorCode.SCHEMA_VIOLATION, "node name is required", details={"node": raw})
        name = name.strip()
        if name in seen:
            raise EngineError(ErrorCode.DUPLICATE_NODE, f"duplicate node name: {name}")
        seen.add(name)
        base_url = raw.get("origin_base_url", raw.get("origin_url", raw.get("base_url")))
        parsed = urlparse(base_url if isinstance(base_url, str) else "")
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise EngineError(
                ErrorCode.INVALID_NODE_ADDRESS,
                "origin_base_url must be a local loopback http(s) address",
                details={"name": name, "origin_base_url": base_url},
            )
        window_size = raw.get("window_size_ms", 60_000)
        if not _is_int(window_size) or window_size <= 0:
            raise EngineError(ErrorCode.SCHEMA_VIOLATION, "window_size_ms must be a positive integer")
        offset = raw.get("window_offset_ms", 0)
        if not _is_int(offset) or offset < 0 or offset >= window_size:
            raise EngineError(ErrorCode.SCHEMA_VIOLATION, "window_offset_ms must satisfy 0 <= offset < size")
        timeout = raw.get("probe_timeout_ms", 1_000)
        if not _is_int(timeout) or timeout <= 0:
            raise EngineError(ErrorCode.SCHEMA_VIOLATION, "probe_timeout_ms must be positive")
        nodes.append(
            {
                "name": name,
                "origin_base_url": base_url.rstrip("/"),
                "window_size_ms": window_size,
                "window_offset_ms": offset,
                "probe_timeout_ms": timeout,
            }
        )
    defaults = payload.get("defaults", {}) if isinstance(payload, dict) else {}
    return {"nodes": nodes, "defaults": defaults}


def _normalize_observation(raw: Dict[str, Any], index: int) -> Dict[str, Any]:
    seq = raw.get("seq", raw.get("observation_seq", raw.get("sequence")))
    if not _is_int(seq) or seq <= 0:
        raise EngineError(ErrorCode.SCHEMA_VIOLATION, "observation seq must be a positive integer", details={"index": index})
    node = raw.get("node", raw.get("edge_node", raw.get("node_name")))
    rule_name = raw.get("rule", raw.get("rule_name"))
    result = raw.get("result", raw.get("probe_result", raw.get("verdict")))
    if isinstance(result, bool):
        result = "match" if result else "mismatch"
    if not isinstance(result, str) or not result.strip():
        raise EngineError(ErrorCode.SCHEMA_VIOLATION, "observation result is required", details={"seq": seq})
    result = result.strip().lower().replace("-", "_")
    aliases = {
        "consistent": "hit",
        "cache_hit": "hit",
        "inconsistent": "digest_mismatch",
        "mismatch": "digest_mismatch",
        "refused": "connection_refused",
        "conn_refused": "connection_refused",
    }
    result = aliases.get(result, result)
    digest = raw.get("content_digest", raw.get("digest"))
    if digest is not None and not is_sha256_digest(digest):
        raise EngineError(
            ErrorCode.OBS_DIGEST_RESULT_MISMATCH,
            "content_digest must be a sha256 digest or null",
            details={"seq": seq, "digest": digest},
        )
    if result in EMPTY_BODY_RESULTS and digest is not None:
        raise EngineError(
            ErrorCode.OBS_DIGEST_RESULT_MISMATCH,
            "empty-body probe results must not carry a content digest",
            details={"seq": seq, "result": result, "digest": digest},
        )
    if result in CONTENT_RESULTS and digest is None:
        raise EngineError(
            ErrorCode.OBS_DIGEST_RESULT_MISMATCH,
            "content-bearing probe results must carry a sha256 content digest",
            details={"seq": seq, "result": result},
        )
    if result not in KNOWN_RESULTS:
        raise EngineError(
            ErrorCode.SCHEMA_VIOLATION,
            "unknown probe result",
            details={"seq": seq, "result": result, "allowed": sorted(KNOWN_RESULTS)},
        )
    clock_ms = raw.get("clock_ms", raw.get("logical_clock_ms", raw.get("observed_at_ms")))
    if clock_ms is not None and (not _is_int(clock_ms) or clock_ms < 0):
        raise EngineError(ErrorCode.SCHEMA_VIOLATION, "observation clock_ms must be a non-negative integer")
    obs_id = raw.get("observation_id", raw.get("id", f"obs-{seq:06d}"))
    return {
        "seq": seq,
        "observation_id": str(obs_id),
        "node": node,
        "rule_name": rule_name,
        "result": result,
        "content_digest": digest,
        "clock_ms": clock_ms,
        "payload": raw,
        "payload_digest": sha256_object(raw),
    }


def parse_observations(
    path: str | Path,
    *,
    node_names: Set[str],
    rule_names: Set[str],
) -> List[Dict[str, Any]]:
    payload = read_json(path)
    raw_observations = payload.get("observations", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_observations, list):
        raise EngineError(ErrorCode.SCHEMA_VIOLATION, "observation stream must be a list or contain observations list")
    observations = [_normalize_observation(raw, index) for index, raw in enumerate(raw_observations)]
    previous_seq = 0
    seen_seq: Set[int] = set()
    for obs in observations:
        if obs["seq"] in seen_seq:
            raise EngineError(
                ErrorCode.OBS_SEQUENCE_NOT_MONOTONIC,
                "observation seq must be unique and strictly increasing",
                details={"seq": obs["seq"]},
            )
        if obs["seq"] <= previous_seq:
            raise EngineError(
                ErrorCode.OBS_SEQUENCE_NOT_MONOTONIC,
                "observation seq must be strictly increasing in stream order",
                details={"seq": obs["seq"], "previous_seq": previous_seq},
            )
        seen_seq.add(obs["seq"])
        previous_seq = obs["seq"]
        if obs["node"] not in node_names:
            raise EngineError(
                ErrorCode.OBS_REFERENCE_UNKNOWN,
                "observation references unknown edge node",
                details={"seq": obs["seq"], "node": obs["node"]},
            )
        if obs["rule_name"] not in rule_names:
            raise EngineError(
                ErrorCode.OBS_REFERENCE_UNKNOWN,
                "observation references unknown rule",
                details={"seq": obs["seq"], "rule": obs["rule_name"]},
            )
    return observations


def parse_sample(root: str | Path) -> Dict[str, Any]:
    root = Path(root)
    manifest_path = root / "sample-manifest.json"
    if not manifest_path.exists():
        candidates = list(root.glob("*manifest*.json"))
        manifest_path = candidates[0] if candidates else manifest_path
    manifest = read_json(manifest_path)
    default_clock = manifest.get("default_clock_ms")

    v1_path = root / "v1" / "rules.json"
    v1 = parse_v1_rules(v1_path, default_clock=default_clock) if v1_path.exists() else {"rules": [], "forward_edges": []}

    v2_dir = root / "v2"
    v2 = parse_v2_package(v2_dir, default_clock=default_clock) if v2_dir.exists() else {"rules": [], "shards": [], "tombstones": [], "manifest": {}, "ruleset_digest": None}

    node_candidates = [root / "nodes" / "edge-nodes.json", root / "nodes.json", root / "edge-nodes.json"]
    nodes_path = next((path for path in node_candidates if path.exists()), node_candidates[0])
    nodes = parse_nodes(nodes_path)

    rule_names = {rule["name"] for rule in v1["rules"]} | {rule["name"] for rule in v2["rules"]}
    # Cross-generation dependency references are valid in the unified view.
    for version in (v1, v2):
        for rule in version["rules"]:
            for dependency in rule["dependencies"]:
                if dependency not in rule_names:
                    raise EngineError(
                        ErrorCode.DANGLING_DEPENDENCY,
                        "rule dependency is not declared in either generation",
                        details={"rule": rule["name"], "dependency": dependency, "source": rule["version"]},
                    )

    obs_candidates = [
        root / "observations" / "probe-stream.json",
        root / "observations.json",
        root / "probe-stream.json",
    ]
    obs_path = next((path for path in obs_candidates if path.exists()), obs_candidates[0])
    observations = parse_observations(
        obs_path,
        node_names={node["name"] for node in nodes["nodes"]},
        rule_names=rule_names,
    )

    origin_content_path = root / "origin-content.json"
    origin_content = read_json(origin_content_path) if origin_content_path.exists() else {}

    return {
        "root": str(root),
        "manifest": manifest,
        "v1": v1,
        "v2": v2,
        "nodes": nodes,
        "observations": observations,
        "origin_content": origin_content,
    }


def parse_observation_batch(path: str | Path) -> List[Dict[str, Any]]:
    payload = read_json(path)
    raw = payload.get("observations", payload if isinstance(payload, list) else [])
    return [_normalize_observation(item, index) for index, item in enumerate(raw)]


def content_digest_for_result_check(obs: Dict[str, Any], expected_digest: Optional[str]) -> bool:
    if obs["result"] in EMPTY_BODY_RESULTS:
        return obs["content_digest"] is None
    if obs["result"] in CONTENT_RESULTS:
        return obs["content_digest"] is not None
    return True
