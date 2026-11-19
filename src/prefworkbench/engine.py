from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .analysis import (
    all_pair_ids,
    changed_pair_ids,
    diff_pair_hashes,
    sample_pair_hashes,
)
from .canonical import canonical_bytes, hash_value
from .config import node_params, validate_config
from .errors import (
    E_INTERNAL,
    E_RESOURCE_LIMIT,
    WorkbenchError,
)
from .generator import read_config, read_samples
from .graph import (
    build_key_components,
    cache_key_for,
    compute_node,
    payload_hash,
    topological_order,
    upstream_for,
)
from .nodes import NODE_ORDER, TOPOLOGY_EDGES
from .store import Stage, StageLock


def _record_error(stage: Stage, error: WorkbenchError) -> None:
    try:
        stage.record_error(error)
    except Exception:
        pass


def run_pipeline(
    *,
    samples_path: Path | str,
    config_path: Path | str,
    stage_dir: Path | str,
    mode: str = "incremental",
    expected_base_version: int | None = None,
    requested_parallel_nodes: int = 1,
) -> dict[str, Any]:
    stage = Stage(stage_dir)
    try:
        samples, source_sha256 = read_samples(samples_path)
        config, config_sha256 = read_config(config_path)
        return run_data(
            samples=samples,
            source_sha256=source_sha256,
            config=config,
            config_sha256=config_sha256,
            stage=stage,
            mode=mode,
            expected_base_version=expected_base_version,
            requested_parallel_nodes=requested_parallel_nodes,
        )
    except WorkbenchError as exc:
        _record_error(stage, exc)
        raise


def run_data(
    *,
    samples: list[dict[str, Any]],
    source_sha256: str,
    config: dict[str, Any],
    config_sha256: str | None = None,
    stage: Stage,
    mode: str = "incremental",
    expected_base_version: int | None = None,
    requested_parallel_nodes: int = 1,
) -> dict[str, Any]:
    if mode not in {"incremental", "full"}:
        raise WorkbenchError(E_INTERNAL, "mode must be incremental or full")
    normalized = validate_config(config)
    samples = sorted(copy.deepcopy(samples), key=lambda item: item["pair_id"])
    config_digest = config_sha256 or hash_value(normalized)
    pair_hashes = sample_pair_hashes(samples)

    try:
        with StageLock(stage.root):
            return _run_locked(
                samples=samples,
                source_sha256=source_sha256,
                config=normalized,
                config_sha256=config_digest,
                pair_hashes=pair_hashes,
                stage=stage,
                mode=mode,
                expected_base_version=expected_base_version,
                requested_parallel_nodes=requested_parallel_nodes,
            )
    except WorkbenchError as exc:
        _record_error(stage, exc)
        raise


def _run_locked(
    *,
    samples: list[dict[str, Any]],
    source_sha256: str,
    config: dict[str, Any],
    config_sha256: str,
    pair_hashes: dict[str, str],
    stage: Stage,
    mode: str,
    expected_base_version: int | None,
    requested_parallel_nodes: int,
) -> dict[str, Any]:
    previous = stage.current_manifest()
    current_version = int(previous["version"]) if previous is not None else 0
    if expected_base_version is not None and expected_base_version != current_version:
        raise WorkbenchError(
            "E_STALE_VERSION",
            "expected base version does not match the active snapshot",
            {
                "expected_base_version": expected_base_version,
                "current_version": current_version,
            },
            node="manifest",
            version=current_version,
        )
    version = current_version + 1
    force_recompute = mode == "full"

    resources = config["resources"]
    requested_parallel = max(1, int(requested_parallel_nodes))
    max_parallel = int(resources["max_parallel_nodes"])
    resource_degradations: list[dict[str, Any]] = []
    if requested_parallel > max_parallel:
        if resources["on_limit"] == "error":
            raise WorkbenchError(
                E_RESOURCE_LIMIT,
                "requested parallel node count exceeds configuration",
                {
                    "category": "PARALLELISM_LIMIT",
                    "requested_parallel_nodes": requested_parallel,
                    "max_parallel_nodes": max_parallel,
                },
                node="scheduler",
                version=version,
            )
        resource_degradations.append(
            {
                "category": "PARALLELISM_CAPPED",
                "requested_parallel_nodes": requested_parallel,
                "effective_parallel_nodes": max_parallel,
            }
        )
    effective_parallel = min(requested_parallel, max_parallel)

    previous_nodes = previous.get("nodes", {}) if previous else {}
    cache_version_hints: dict[tuple[str, str], int] = {}
    for node, diag in previous_nodes.items():
        if isinstance(diag, dict) and isinstance(diag.get("entry_version"), int):
            cache_version_hints[(node, diag.get("cache_key"))] = diag["entry_version"]

    payloads: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    node_diags: dict[str, dict[str, Any]] = {}
    reused_nodes: list[str] = []
    recomputed_nodes: list[str] = []

    for node in topological_order():
        params = node_params(config, node)
        if node == "quality_clean":
            input_components = [
                {
                    "name": "raw_samples",
                    "kind": "input_digest",
                    "value": {
                        "sha256": source_sha256,
                        "pair_count": len(samples),
                    },
                }
            ]
        else:
            input_components = []
        upstreams = upstream_for(node)
        upstream_hashes = {name: hashes[name] for name in sorted(upstreams)}
        components = build_key_components(
            node,
            input_components=input_components,
            upstream_hashes=upstream_hashes,
            params=params,
        )
        key = cache_key_for(components)
        hit = None if force_recompute else stage.cache.get(
            node,
            key,
            version_hint=cache_version_hints.get((node, key)),
        )
        if hit is not None:
            payload = hit["payload"]
            entry = hit
            cache_status = "hit"
            entry_version = hit["created_version"]
            reused_nodes.append(node)
        else:
            try:
                payload = compute_node(
                    node,
                    samples=samples,
                    source_sha256=source_sha256,
                    params=params,
                    upstream_payloads=payloads,
                    version=version,
                )
            except WorkbenchError as exc:
                if exc.node is None:
                    exc.node = node
                if exc.version is None:
                    exc.version = version
                    exc.details.setdefault("version", version)
                raise
            entry = stage.cache.put(
                node,
                key,
                components,
                payload,
                version=version,
            )
            cache_status = "recomputed"
            entry_version = entry["created_version"]
            recomputed_nodes.append(node)
        current_payload_hash = payload_hash(payload)
        if entry["payload_hash"] != current_payload_hash:
            raise WorkbenchError(
                "E_CACHE_CORRUPT",
                "recomputed payload differs from cached envelope",
                {
                    "category": "CACHE_PAYLOAD_HASH_MISMATCH",
                    "entry_hash": entry["payload_hash"],
                    "computed_hash": current_payload_hash,
                },
                node=node,
                version=entry_version,
            )
        payloads[node] = payload
        hashes[node] = current_payload_hash
        node_diags[node] = {
            "node": node,
            "cache_status": cache_status,
            "cache_hit": cache_status == "hit",
            "cache_key": key,
            "key_components": components,
            "payload_hash": current_payload_hash,
            "entry_version": entry_version,
            "snapshot_version": version,
            "cache_entry_checksum": entry["entry_checksum"],
            "cache_object_path": str(
                stage.cache.path_for(node, key).relative_to(stage.root)
            ),
            "input_components": input_components,
            "upstream_hashes": upstream_hashes,
            "parameters": params,
            "counts": payload.get("counts", {}),
            "degradations": payload.get("degradations", []),
        }

    raw_diff = diff_pair_hashes(
        previous.get("source", {}).get("pair_hashes") if previous else None,
        pair_hashes,
    )
    previous_params = previous.get("node_params", {}) if previous else {}
    parameter_changes = {
        node: previous_params.get(node) != node_params(config, node)
        for node in NODE_ORDER
    }

    affected_by_node: dict[str, list[str]] = {}
    changed_by_node: dict[str, list[str]] = {}
    for node in topological_order():
        upstream_affected: set[str] = set()
        for upstream in upstream_for(node):
            upstream_affected.update(affected_by_node.get(upstream, []))
        previous_diag = previous_nodes.get(node)
        old_payload = None
        if previous_diag is not None and not (force_recompute and node == NODE_ORDER[0]):
            old_payload = _load_previous_payload(stage, node, previous_diag)
        local_changed = changed_pair_ids(old_payload, payloads[node], node)
        parameter_affected: set[str] = set()
        if previous is not None and parameter_changes[node]:
            parameter_affected.update(all_pair_ids(payloads[node], node))
        if previous is None:
            affected = set(all_pair_ids(payloads[node], node))
        elif node == "quality_clean":
            affected = (
                set(raw_diff["added"])
                | set(raw_diff["removed"])
                | set(raw_diff["changed"])
                | set(local_changed)
                | parameter_affected
            )
        else:
            affected = upstream_affected | set(local_changed) | parameter_affected
        affected_by_node[node] = sorted(affected)
        changed_by_node[node] = local_changed
        node_diags[node]["changed_pair_ids"] = local_changed
        node_diags[node]["affected_pair_ids"] = sorted(affected)
        node_diags[node]["affected_pair_count"] = len(affected)
        node_diags[node]["affected_pair_ids_truncated"] = False

    invalidated_nodes = sorted(
        node
        for node in NODE_ORDER
        if previous is None or previous_nodes.get(node, {}).get("cache_key") != node_diags[node]["cache_key"]
    )
    affected_nodes = sorted(node for node, values in affected_by_node.items() if values)
    all_affected_pairs = sorted(
        {pair_id for values in affected_by_node.values() for pair_id in values}
    )

    causes: list[dict[str, Any]] = []
    raw_changed_ids = sorted(
        set(raw_diff["added"]) | set(raw_diff["removed"]) | set(raw_diff["changed"])
    )
    if raw_changed_ids:
        causes.append(
            {
                "kind": "sample_content",
                "pair_ids": raw_changed_ids,
                "added": raw_diff["added"],
                "removed": raw_diff["removed"],
                "changed": raw_diff["changed"],
            }
        )
    for node in NODE_ORDER:
        if previous is not None and parameter_changes[node]:
            causes.append(
                {
                    "kind": "node_parameters",
                    "node": node,
                    "affected_pairs": len(affected_by_node[node]),
                }
            )

    consistency_payload = payloads["pair_consistency"]
    sampling_payload = payloads["allocation_sampling"]
    conflicts = consistency_payload.get("conflicts", [])
    underfilled = sampling_payload.get("underfilled_targets", [])
    payload_degradations = [
        {"node": node, **item}
        for node, payload in payloads.items()
        for item in payload.get("degradations", [])
    ]
    degradations = resource_degradations + payload_degradations

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "version": version,
        "mode": mode,
        "source": {
            "samples_sha256": source_sha256,
            "config_sha256": config_sha256,
            "sample_count": len(samples),
            "pair_hashes": pair_hashes,
        },
        "topology": {
            "nodes": list(NODE_ORDER),
            "edges": [list(edge) for edge in TOPOLOGY_EDGES],
        },
        "node_params": {node: node_diags[node]["parameters"] for node in NODE_ORDER},
        "nodes": node_diags,
        "reused_nodes": sorted(reused_nodes),
        "recomputed_nodes": sorted(recomputed_nodes),
        "invalidated_nodes": invalidated_nodes,
        "affected_nodes": affected_nodes,
        "affected_pair_ids": all_affected_pairs,
        "raw_diff": raw_diff,
        "causes": causes,
        "conflicts": conflicts,
        "conflict_pair_ids": sorted(
            {pair_id for item in conflicts for pair_id in item.get("pair_ids", [])}
        ),
        "quota_targets": sampling_payload.get("quota_targets", []),
        "underfilled_targets": underfilled,
        "degradations": degradations,
        "resources": {
            "requested_parallel_nodes": requested_parallel,
            "effective_parallel_nodes": effective_parallel,
            "max_parallel_nodes": max_parallel,
            "max_manifest_bytes": int(config["resources"]["max_manifest_bytes"]),
            "near_duplicate_max_comparisons": int(
                config["near_duplicate"]["max_comparisons"]
            ),
            "policy": config["resources"]["on_limit"],
        },
        "sampling_seed": sampling_payload.get("sampling_seed"),
        "recent_errors": stage.recent_errors(),
        "cache_index_path": f"cache/indexes/v{version:06d}.json",
    }

    manifest = _enforce_manifest_limit(manifest, config)
    dataset_records = sampling_payload["dataset"]
    cache_entries = {
        node: {
            diag["cache_key"]: {
                "entry_version": diag["entry_version"],
                "snapshot_version": version,
                "payload_hash": diag["payload_hash"],
                "entry_checksum": diag["cache_entry_checksum"],
            }
        }
        for node, diag in node_diags.items()
    }
    stage.commit_cache_index(version=version, entries=cache_entries)
    committed = stage.commit(
        manifest,
        dataset_records,
        expected_base_version=expected_base_version,
    )
    return committed


def _load_previous_payload(
    stage: Stage,
    node: str,
    previous_diag: dict[str, Any],
) -> dict[str, Any] | None:
    key = previous_diag.get("cache_key")
    if not isinstance(key, str):
        return None
    entry = stage.cache.get(
        node,
        key,
        version_hint=previous_diag.get("entry_version"),
    )
    return entry["payload"] if entry is not None else None


def _enforce_manifest_limit(
    manifest: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    limit = int(config["resources"]["max_manifest_bytes"])
    size = len(canonical_bytes(manifest))
    if size <= limit:
        manifest["manifest_memory_estimate_bytes"] = size
        return manifest
    if config["resources"]["on_limit"] == "error":
        raise WorkbenchError(
            E_RESOURCE_LIMIT,
            "manifest memory estimate exceeds limit",
            {
                "category": "MANIFEST_MEMORY_LIMIT",
                "estimated_bytes": size,
                "max_manifest_bytes": limit,
            },
            node="manifest",
            version=manifest["version"],
        )

    for diag in manifest["nodes"].values():
        full_ids = diag.get("affected_pair_ids", [])
        diag["affected_pair_ids_truncated"] = len(full_ids) > 200
        diag["affected_pair_ids"] = full_ids[:200]
    manifest["degradations"] = list(manifest.get("degradations", [])) + [
        {
            "node": "manifest",
            "category": "MANIFEST_DETAIL_TRUNCATED",
            "original_estimated_bytes": size,
            "max_manifest_bytes": limit,
        }
    ]
    compacted_size = len(canonical_bytes(manifest))
    if compacted_size > limit:
        raise WorkbenchError(
            E_RESOURCE_LIMIT,
            "manifest cannot be compacted below the memory limit",
            {
                "category": "MANIFEST_MEMORY_LIMIT_AFTER_COMPACTION",
                "original_estimated_bytes": size,
                "compacted_estimated_bytes": compacted_size,
                "max_manifest_bytes": limit,
            },
            node="manifest",
            version=manifest["version"],
        )
    manifest["manifest_memory_estimate_bytes"] = compacted_size
    return manifest
