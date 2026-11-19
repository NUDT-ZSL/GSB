from __future__ import annotations

from pathlib import Path
from typing import Any

from .cache import cache_key_from_components
from .canonical import hash_value, sha256_file
from .errors import E_INCONSISTENT, WorkbenchError
from .graph import topology_is_acyclic
from .nodes import NODE_ORDER
from .store import Stage


def run_selfcheck(
    stage_dir: Path | str,
    *,
    version: int | None = None,
    raise_on_error: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    stage = Stage(stage_dir)
    manifest = _load_manifest(stage, version)
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def check(category: str, ok: bool, message: str, **details: Any) -> None:
        row = {
            "category": category,
            "ok": bool(ok),
            "message": message,
            "details": details,
        }
        checks.append(row)
        if not ok:
            errors.append(
                {
                    "category": category,
                    "message": message,
                    "details": details,
                }
            )

    manifest_ok = True
    try:
        Stage.validate_manifest(manifest)
    except WorkbenchError:
        manifest_ok = False
        raise
    check("MANIFEST_CHECKSUM", manifest_ok, "manifest checksum is valid")

    topology = manifest.get("topology", {})
    nodes = topology.get("nodes", [])
    edges = [tuple(edge) for edge in topology.get("edges", [])]
    acyclic, topology_errors = topology_is_acyclic(nodes, edges)
    check(
        "TOPOLOGY_CYCLE",
        acyclic,
        "graph topology is acyclic" if acyclic else "graph topology contains a cycle",
        errors=topology_errors,
    )
    check(
        "NODE_SET",
        set(nodes) == set(NODE_ORDER),
        "topology contains exactly the four business nodes",
        expected=sorted(NODE_ORDER),
        actual=sorted(nodes),
    )

    dataset = stage.dataset(manifest)
    check(
        "DATASET_VERSION",
        dataset.get("version") == manifest.get("version"),
        "dataset snapshot version matches manifest version",
        dataset_version=dataset.get("version"),
        manifest_version=manifest.get("version"),
    )
    actual_dataset_checksum = hash_value(dataset.get("records", []))
    check(
        "DATASET_CHECKSUM",
        actual_dataset_checksum == manifest.get("dataset_checksum")
        == dataset.get("dataset_checksum"),
        "dataset records, dataset snapshot and manifest checksums agree",
        manifest_checksum=manifest.get("dataset_checksum"),
        dataset_checksum=dataset.get("dataset_checksum"),
        computed_checksum=actual_dataset_checksum,
    )
    content_path = stage.root / manifest.get("dataset_records_path", "")
    content_ok = bool(manifest.get("dataset_records_path")) and content_path.exists()
    content_checksum = sha256_file(content_path) if content_ok else None
    check(
        "DATASET_RECORDS_FILE",
        content_ok and content_checksum == manifest.get("dataset_records_checksum"),
        "content-addressed dataset records file matches its checksum",
        path=manifest.get("dataset_records_path"),
        recorded_checksum=manifest.get("dataset_records_checksum"),
        computed_checksum=content_checksum,
    )

    reused = set(manifest.get("reused_nodes", []))
    recomputed = set(manifest.get("recomputed_nodes", []))
    affected = set(manifest.get("affected_nodes", []))
    check(
        "NODE_SET_OVERLAP",
        not (reused & recomputed),
        "reused and recomputed node sets are disjoint",
        intersection=sorted(reused & recomputed),
    )
    check(
        "NODE_SET_COVERAGE",
        reused | recomputed == set(NODE_ORDER),
        "reused and recomputed sets cover every graph node",
        missing=sorted(set(NODE_ORDER) - (reused | recomputed)),
        extra=sorted((reused | recomputed) - set(NODE_ORDER)),
    )
    check(
        "INVALIDATION_COVERAGE",
        affected <= (reused | recomputed),
        "reused and recomputed sets cover all affected nodes",
        uncovered=sorted(affected - (reused | recomputed)),
    )

    payloads: dict[str, dict[str, Any]] = {}
    for node in NODE_ORDER:
        diag = manifest.get("nodes", {}).get(node, {})
        key = diag.get("cache_key")
        components = diag.get("key_components")
        reconstructed = cache_key_from_components(components) if isinstance(components, list) else None
        check(
            "CACHE_KEY_MISMATCH",
            reconstructed == key,
            f"{node} cache key is reconstructable from its components",
            node=node,
            recorded_key=key,
            reconstructed_key=reconstructed,
            category_detail=f"{node}:CACHE_KEY_MISMATCH",
        )
        if not isinstance(key, str):
            continue
        try:
            entry = stage.cache.get(node, key, version_hint=diag.get("entry_version"))
        except WorkbenchError:
            raise
        if entry is None:
            check(
                "CACHE_ENTRY_MISSING",
                False,
                f"{node} cache entry is missing",
                node=node,
                cache_key=key,
            )
            continue
        payloads[node] = entry["payload"]
        component_ok = entry.get("key_components") == components
        check(
            "CACHE_KEY_COMPONENT_MISMATCH",
            component_ok,
            f"{node} cache key components match the cache entry",
            node=node,
            category_detail=f"{node}:CACHE_KEY_COMPONENT_MISMATCH",
        )
        check(
            "CACHE_PAYLOAD_HASH_MISMATCH",
            hash_value(entry["payload"]) == diag.get("payload_hash"),
            f"{node} payload hash matches the manifest",
            node=node,
            category_detail=f"{node}:CACHE_PAYLOAD_HASH_MISMATCH",
        )
        check(
            "CACHE_ENTRY_CHECKSUM_MISMATCH",
            entry.get("entry_checksum") == diag.get("cache_entry_checksum"),
            f"{node} cache entry checksum matches the manifest",
            node=node,
            category_detail=f"{node}:CACHE_ENTRY_CHECKSUM_MISMATCH",
        )

    if set(payloads) == set(NODE_ORDER):
        _check_sets_and_traceability(payloads, dataset, check)
        _check_cache_index(stage, manifest, check)

    report = {
        "ok": not errors,
        "version": manifest.get("version"),
        "checks": checks,
        "errors": errors,
    }
    if persist:
        stage.write_auxiliary_json(f"selfcheck-v{manifest['version']:06d}.json", report)
    if errors and raise_on_error:
        error = WorkbenchError(
            E_INCONSISTENT,
            "cross-module consistency self-check failed",
            {
                "categories": sorted({item["category"] for item in errors}),
                "errors": errors,
            },
            node="selfcheck",
            version=manifest.get("version"),
        )
        stage.record_error(error)
        raise error
    return report


def _load_manifest(stage: Stage, version: int | None) -> dict[str, Any]:
    if version is not None:
        return stage.load_manifest(version)
    manifest = stage.current_manifest()
    if manifest is None:
        raise WorkbenchError(
            E_INCONSISTENT,
            "no committed manifest is available for self-check",
            {"category": "NO_MANIFEST"},
            node="selfcheck",
        )
    return manifest


def _check_sets_and_traceability(
    payloads: dict[str, dict[str, Any]],
    dataset: dict[str, Any],
    check: Any,
) -> None:
    quality = payloads["quality_clean"]
    near = payloads["near_duplicate"]
    consistency = payloads["pair_consistency"]
    sampling = payloads["allocation_sampling"]

    quality_kept = set(quality.get("kept_pair_ids", []))
    quality_dropped = set(quality.get("dropped_pair_ids", []))
    check(
        "QUALITY_RETAINED_DROPPED_OVERLAP",
        not (quality_kept & quality_dropped),
        "quality retained and dropped sets are disjoint",
        intersection=sorted(quality_kept & quality_dropped),
    )
    selected = set(sampling.get("selected_pair_ids", []))
    unselected = set(sampling.get("unselected_pair_ids", []))
    check(
        "SAMPLING_SELECTED_UNSELECTED_OVERLAP",
        not (selected & unselected),
        "sampling selected and unselected sets are disjoint",
        intersection=sorted(selected & unselected),
    )

    quality_by_id = {item["pair_id"]: item for item in quality.get("pair_metrics", [])}
    near_by_id = {item["pair_id"]: item for item in near.get("pairs", [])}
    consistency_by_id = {item["pair_id"]: item for item in consistency.get("pairs", [])}
    records = dataset.get("records", [])
    dataset_ids = {item.get("pair_id") for item in records}
    trace_errors: list[dict[str, Any]] = []
    for record in records:
        pair_id = record.get("pair_id")
        if pair_id not in quality_by_id:
            trace_errors.append({"pair_id": pair_id, "category": "MISSING_QUALITY_TRACE"})
            continue
        if pair_id not in near_by_id:
            trace_errors.append({"pair_id": pair_id, "category": "MISSING_SIMILARITY_TRACE"})
            continue
        if pair_id not in consistency_by_id:
            trace_errors.append({"pair_id": pair_id, "category": "MISSING_CONSISTENCY_TRACE"})
            continue
        quality_row = quality_by_id[pair_id]
        near_row = near_by_id[pair_id]
        consistency_row = consistency_by_id[pair_id]
        if record.get("quality_score") != quality_row.get("quality_score"):
            trace_errors.append({"pair_id": pair_id, "category": "QUALITY_SCORE_MISMATCH"})
        if record.get("quality_tier") != quality_row.get("quality_tier"):
            trace_errors.append({"pair_id": pair_id, "category": "QUALITY_TIER_MISMATCH"})
        if record.get("signature_group_id") != near_row.get("signature_group_id"):
            trace_errors.append({"pair_id": pair_id, "category": "SIGNATURE_GROUP_MISMATCH"})
        if record.get("max_signature_similarity") != near_row.get("max_signature_similarity"):
            trace_errors.append({"pair_id": pair_id, "category": "SIMILARITY_MISMATCH"})
        if record.get("prompt_group_id") != consistency_row.get("prompt_group_id"):
            trace_errors.append({"pair_id": pair_id, "category": "PROMPT_GROUP_MISMATCH"})
        if not consistency_row.get("eligible"):
            trace_errors.append({"pair_id": pair_id, "category": "INELIGIBLE_SAMPLE_IN_MANIFEST"})
    check(
        "SAMPLE_TRACEABILITY",
        not trace_errors,
        "every dataset sample traces to quality, similarity and consistency artifacts",
        errors=trace_errors,
    )
    check(
        "SELECTED_DATASET_MEMBERSHIP",
        dataset_ids == selected,
        "dataset membership matches the sampling selected set",
        missing_from_dataset=sorted(selected - dataset_ids),
        extra_in_dataset=sorted(dataset_ids - selected),
    )


def _check_cache_index(stage: Stage, manifest: dict[str, Any], check: Any) -> None:
    try:
        index = stage.load_cache_index(int(manifest["version"]))
    except WorkbenchError as exc:
        check(
            "CACHE_INDEX_MISMATCH",
            False,
            "cache index snapshot is unreadable",
            error=exc.to_dict(),
        )
        return
    issues: list[dict[str, Any]] = []
    for node, diag in manifest.get("nodes", {}).items():
        key = diag.get("cache_key")
        indexed = index.get("entries", {}).get(node, {}).get(key)
        if indexed is None:
            issues.append({"node": node, "category": "MISSING_INDEX_ENTRY", "cache_key": key})
            continue
        manifest_fields = {
            "entry_version": "entry_version",
            "payload_hash": "payload_hash",
            "entry_checksum": "cache_entry_checksum",
        }
        for field, manifest_field in manifest_fields.items():
            if indexed.get(field) != diag.get(manifest_field):
                issues.append(
                    {
                        "node": node,
                        "category": f"INDEX_{field.upper()}_MISMATCH",
                    }
                )
    check(
        "CACHE_INDEX_MISMATCH",
        not issues,
        "versioned cache index agrees with the manifest",
        issues=issues,
    )
