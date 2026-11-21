from __future__ import annotations

from typing import Any

import numpy as np

from .cache import CacheStore, key_from_components
from .canonical import array_digest, pretty_json_bytes, sha256_bytes, sha256_json
from .config import (
    config_digest,
    samples_digest,
    validate_config,
)
from .encoders import cosine_scores
from .errors import PipelineError
from .graph import GraphSpec
from .state import StateStore


def _fail(
    category: str,
    location: str,
    message: str,
    **details: Any,
) -> None:
    raise PipelineError(
        "E_VERIFY_FAIL",
        message,
        stage="verify",
        details={
            "category": category,
            "location": location,
            **details,
        },
    )


def _load_manifest(
    cache: CacheStore,
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    entry = snapshot["retrieval_manifest"]
    try:
        _, meta, cache_meta = cache.get(entry["key"])
    except PipelineError as exc:
        _fail(
            "cache_key_unreadable",
            f"retrieval_manifest:{entry['key']}",
            str(exc),
            original_code=exc.code,
        )
    if cache_meta["product_hash"] != entry["product_hash"]:
        _fail(
            "manifest_product_hash",
            f"retrieval_manifest:{entry['key']}",
            "Retrieval manifest product hash differs from snapshot",
        )
    result_bytes = pretty_json_bytes(meta)
    if sha256_bytes(result_bytes) != entry.get("results_sha256"):
        _fail(
            "results_digest",
            "retrieval_manifest",
            "Result bytes hash differs from snapshot",
        )
    if meta.get("schema") != "cm-retrieval-results-v1":
        _fail(
            "result_schema",
            "retrieval_manifest",
            "Unexpected retrieval result schema",
        )
    return meta, result_bytes


def _load_index(
    cache: CacheStore,
    snapshot: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    loaded: dict[int, dict[str, Any]] = {}
    for key in sorted(snapshot["index_blocks"], key=lambda item: int(item)):
        block_id = int(key)
        entry = snapshot["index_blocks"][key]
        try:
            arrays, meta, cache_meta = cache.get(entry["key"])
        except PipelineError as exc:
            _fail(
                "cache_key_unreadable",
                f"index_build:block-{block_id}",
                str(exc),
                original_code=exc.code,
            )
        if cache_meta["product_hash"] != entry["product_hash"]:
            _fail(
                "index_product_hash",
                f"index_build:block-{block_id}",
                "Index product hash differs from snapshot",
            )
        required = {"codes", "residuals", "centroids"}
        if set(arrays) != required:
            _fail(
                "index_arrays",
                f"index_build:block-{block_id}",
                "Index payload has unexpected arrays",
            )
        pair_ids = list(meta.get("pair_ids", []))
        if pair_ids != sorted(pair_ids):
            _fail(
                "index_pair_order",
                f"index_build:block-{block_id}",
                "Index pair ids are not sorted",
            )
        if pair_ids != list(entry.get("pair_ids", [])):
            _fail(
                "index_pair_manifest",
                f"index_build:block-{block_id}",
                "Index pair ids differ from snapshot manifest",
            )
        if int(meta.get("block_id", -1)) != block_id:
            _fail(
                "index_block_id",
                f"index_build:block-{block_id}",
                "Index block id differs from payload metadata",
            )
        codes = arrays["codes"]
        residuals = arrays["residuals"]
        centroids = arrays["centroids"]
        if codes.ndim != 1 or len(codes) != len(pair_ids):
            _fail(
                "index_code_shape",
                f"index_build:block-{block_id}",
                "Index code count does not match pair count",
            )
        if residuals.shape[0] != len(pair_ids) or centroids.shape[0] == 0:
            _fail(
                "index_shape",
                f"index_build:block-{block_id}",
                "Index residual or centroid shape is invalid",
            )
        if np.any(codes < 0) or np.any(codes >= centroids.shape[0]):
            _fail(
                "index_code_range",
                f"index_build:block-{block_id}",
                "Index code is outside the codebook range",
            )
        reconstructed = centroids[codes] + residuals
        if not np.isfinite(reconstructed).all():
            _fail(
                "index_nonfinite",
                f"index_build:block-{block_id}",
                "Index reconstruction is non-finite",
            )
        loaded[block_id] = {
            "pair_ids": pair_ids,
            "codes": codes,
            "residuals": residuals,
            "centroids": centroids,
            "reconstructed": reconstructed,
            "meta": meta,
            "entry": entry,
        }
    return loaded


def _pair_block_map(index: dict[int, dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for block_id in sorted(index):
        for pair_id in index[block_id]["pair_ids"]:
            if pair_id in result:
                _fail(
                    "index_pair_duplicate",
                    f"pair:{pair_id}",
                    "Pair appears in more than one index block",
                )
            result[pair_id] = block_id
    return result


def _load_node_arrays(
    cache: CacheStore,
    snapshot: dict[str, Any],
    instance: str,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    node = snapshot["nodes"].get(instance)
    if node is None:
        _fail(
            "missing_node",
            instance,
            f"Snapshot is missing node {instance}",
        )
    try:
        arrays, meta, cache_meta = cache.get(node["key"])
    except PipelineError as exc:
        _fail(
            "cache_key_unreadable",
            instance,
            str(exc),
            original_code=exc.code,
        )
    if cache_meta["product_hash"] != node["product_hash"]:
        _fail(
            "node_product_hash",
            instance,
            "Node product hash differs from snapshot",
        )
    return arrays, meta


def _recompute_query(
    cache: CacheStore,
    snapshot: dict[str, Any],
    index: dict[int, dict[str, Any]],
    pair_block: dict[str, int],
    query_pair: str,
    query_mask: list[str],
) -> list[dict[str, Any]]:
    if "image" not in query_mask:
        return []
    arrays, _ = _load_node_arrays(
        cache,
        snapshot,
        f"align:{query_pair}",
    )
    query_vector = arrays["image_aligned"]
    candidates: list[str] = []
    vectors: list[np.ndarray] = []
    samples = {sample["pair_id"]: sample for sample in snapshot["samples"]}
    for pair_id in sorted(samples):
        if "text" not in samples[pair_id]["modality_mask"]:
            continue
        block_id = pair_block[pair_id]
        block = index[block_id]
        row = block["pair_ids"].index(pair_id)
        candidates.append(pair_id)
        vectors.append(block["reconstructed"][row])
    if not candidates:
        return []
    scores = cosine_scores(query_vector, np.stack(vectors))
    ranked = sorted(
        zip(candidates, (float(score) for score in scores)),
        key=lambda item: (-item[1], item[0]),
    )
    top_k = min(
        int(snapshot["config"]["retrieval"]["top_k"]),
        len(ranked),
    )
    result: list[dict[str, Any]] = []
    for rank, (target, score) in enumerate(ranked[:top_k], start=1):
        block_id = pair_block[target]
        block = index[block_id]
        row = block["pair_ids"].index(target)
        code = int(block["codes"][row])
        residual = block["residuals"][row]
        reconstructed = block["centroids"][code] + residual
        result.append(
            {
                "rank": rank,
                "target_pair_id": target,
                "score": score,
                "block_id": block_id,
                "code": code,
                "residual_hash": array_digest(residual),
                "reconstructed_hash": array_digest(reconstructed),
            }
        )
    return result


def verify_snapshot(
    *,
    cache_root: str,
    state_root: str,
    snapshot_version: int | None = None,
    require_current: bool = True,
) -> dict[str, Any]:
    cache = CacheStore(cache_root)
    state = StateStore(state_root)
    try:
        state_data = state.read_state()
        if snapshot_version is None:
            snapshot_version = int(state_data["current_version"])
        snapshot = state.load_snapshot(int(snapshot_version))
        if snapshot is None:
            _fail("missing_snapshot", "state", "No snapshot is available")
        if require_current and int(snapshot_version) != int(state_data["current_version"]):
            _fail(
                "stale_snapshot",
                f"snapshot:{snapshot_version}",
                "Requested snapshot is not the current snapshot",
            )
        config = snapshot["config"]
        validate_config(config)
        graph = GraphSpec.from_config(config)
        order = graph.topological_order()
        if len(order) != len(graph.nodes):
            _fail("graph_cycle", "graph", "Graph topology contains a cycle")
        expected_lineage = {
            "vocab_version": int(snapshot["vocab"]["version"]),
            "vocab_checksum": snapshot["vocab"]["checksum"],
            "codebook_version": int(snapshot["codebook"]["version"]),
            "codebook_checksum": snapshot["codebook"]["checksum"],
            "config_digest": config_digest(config),
            "samples_digest": samples_digest(snapshot["samples"]),
            "graph_digest": graph.digest(),
        }
        if snapshot["lineage"] != expected_lineage:
            _fail(
                "lineage_digest",
                "snapshot:lineage",
                "Snapshot lineage does not match its recorded inputs",
            )
        recomputed = list(snapshot["recomputed_nodes"])
        reused = list(snapshot["reused_nodes"])
        all_instances = list(snapshot["all_node_instances"])
        if len(recomputed) != len(set(recomputed)):
            _fail("recomputed_duplicate", "nodes", "Recomputed set contains duplicates")
        if len(reused) != len(set(reused)):
            _fail("reused_duplicate", "nodes", "Reused set contains duplicates")
        if set(recomputed) & set(reused):
            _fail(
                "set_overlap",
                "nodes",
                "Recomputed and reused node sets overlap",
                overlap=sorted(set(recomputed) & set(reused))[:5],
            )
        if set(recomputed) | set(reused) != set(all_instances):
            _fail(
                "set_coverage",
                "nodes",
                "Recomputed and reused node sets do not cover every node",
                missing=sorted(set(all_instances) - (set(recomputed) | set(reused)))[
                    :5
                ],
                extra=sorted((set(recomputed) | set(reused)) - set(all_instances))[:5],
            )
        expected_instances = graph.instances(
            [sample["pair_id"] for sample in snapshot["samples"]],
            [int(block) for block in snapshot["index_blocks"]],
        )
        if sorted(all_instances) != sorted(expected_instances):
            _fail(
                "instance_set",
                "nodes",
                "Snapshot node instances do not match graph expansion",
            )
        for instance in sorted(expected_instances):
            node = snapshot["nodes"].get(instance)
            if node is None:
                _fail("missing_node", instance, "Snapshot node is missing")
            try:
                _, _, cache_meta = cache.get(node["key"])
            except PipelineError as exc:
                _fail(
                    "cache_key_unreadable",
                    instance,
                    str(exc),
                    original_code=exc.code,
                )
            if cache_meta["product_hash"] != node["product_hash"]:
                _fail(
                    "node_product_hash",
                    instance,
                    "Cached product hash differs from snapshot",
                )
        expected_blocks = set(range(int(config["index"]["num_shards"])))
        actual_blocks = {int(block) for block in snapshot["index_blocks"]}
        if actual_blocks != expected_blocks:
            _fail(
                "index_block_set",
                "index_blocks",
                "Index block ids do not match configured shard count",
            )
        index = _load_index(cache, snapshot)
        pair_block = _pair_block_map(index)
        sample_pairs = {sample["pair_id"] for sample in snapshot["samples"]}
        if set(pair_block) != sample_pairs:
            _fail(
                "index_pair_coverage",
                "index_blocks",
                "Index block pair coverage differs from sample set",
            )
        candidate_mask_digest = sha256_json(
            {
                sample["pair_id"]: list(sample["modality_mask"])
                for sample in sorted(
                    snapshot["samples"], key=lambda item: item["pair_id"]
                )
            }
        )
        index_block_hashes = {
            str(block_id): snapshot["index_blocks"][str(block_id)]["product_hash"]
            for block_id in sorted(index)
        }
        result_doc, result_bytes = _load_manifest(cache, snapshot)
        if result_doc.get("lineage") != snapshot["lineage"]:
            _fail(
                "result_lineage",
                "retrieval_manifest",
                "Retrieval result lineage is stale",
            )
        manifest_entry = snapshot["retrieval_manifest"]
        manifest_meta = cache.get(manifest_entry["key"])[2]
        recomputed_manifest_key = key_from_components(manifest_meta["key_components"])
        if recomputed_manifest_key != manifest_entry["key"]:
            _fail(
                "manifest_cache_key",
                "retrieval_manifest",
                "Retrieval manifest cache key cannot be recomputed",
            )
        if manifest_meta["product_hash"] != manifest_entry["product_hash"]:
            _fail(
                "manifest_product_hash",
                "retrieval_manifest",
                "Retrieval manifest product hash differs",
            )
        queries = result_doc.get("queries", [])
        query_ids = [query["query_pair_id"] for query in queries]
        if query_ids != sorted(query_ids):
            _fail("query_order", "retrieval_manifest", "Queries are not sorted")
        if set(query_ids) != sample_pairs or len(query_ids) != len(sample_pairs):
            _fail(
                "query_set",
                "retrieval_manifest",
                "Retrieval query set differs from sample set",
            )
        seen_hits: set[tuple[str, int]] = set()
        for query in queries:
            query_pair = query["query_pair_id"]
            hits = query["hits"]
            ranks = [int(hit["rank"]) for hit in hits]
            expected_ranks = list(range(1, len(hits) + 1))
            if ranks != expected_ranks:
                _fail(
                    "rank_sequence",
                    f"retrieve:{query_pair}",
                    "Hit ranks are not contiguous from 1",
                )
            for hit in hits:
                location = f"retrieve:{query_pair}#rank={hit['rank']}"
                key = (query_pair, int(hit["rank"]))
                if key in seen_hits:
                    _fail(
                        "duplicate_hit",
                        location,
                        "Retrieval hit appears more than once",
                    )
                seen_hits.add(key)
                query_arrays, _ = _load_node_arrays(
                    cache,
                    snapshot,
                    f"align:{query_pair}",
                )
                query_vector_digest = array_digest(query_arrays["image_aligned"])
                if hit["query_vector_hash"] != query_vector_digest:
                    _fail(
                        "hit_query_vector_hash",
                        location,
                        "Hit query vector digest differs from aligned query",
                    )
                expected_hit_lineage = {
                    "vocab_version": int(snapshot["vocab"]["version"]),
                    "vocab_checksum": snapshot["vocab"]["checksum"],
                    "codebook_version": int(snapshot["codebook"]["version"]),
                    "codebook_checksum": snapshot["codebook"]["checksum"],
                    "graph_digest": snapshot["lineage"]["graph_digest"],
                    "query_vector_hash": query_vector_digest,
                    "index_block_hashes": index_block_hashes,
                    "candidate_mask_digest": candidate_mask_digest,
                }
                if hit.get("lineage") != expected_hit_lineage:
                    _fail(
                        "hit_lineage",
                        location,
                        "Hit dependency lineage differs from its retrieval inputs",
                    )
                if int(hit["vocab_version"]) != int(snapshot["vocab"]["version"]):
                    _fail(
                        "hit_vocab_version",
                        location,
                        "Hit vocabulary version differs from snapshot",
                    )
                if int(hit["codebook_version"]) != int(
                    snapshot["codebook"]["version"]
                ):
                    _fail(
                        "hit_codebook_version",
                        location,
                        "Hit codebook version differs from snapshot",
                    )
                if hit["cache_keys"]["retrieve"] != snapshot["nodes"][
                    f"retrieve:{query_pair}"
                ]["key"]:
                    _fail(
                        "hit_retrieve_key",
                        location,
                        "Hit retrieve cache key differs from snapshot node",
                    )
                target = hit["target_pair_id"]
                target_align_arrays, _ = _load_node_arrays(
                    cache,
                    snapshot,
                    f"align:{target}",
                )
                if hit.get("target_text_alignment_hash") != array_digest(
                    target_align_arrays["text_aligned"]
                ):
                    _fail(
                        "hit_target_alignment_hash",
                        location,
                        "Hit target text-alignment digest differs from align node",
                    )
                block_id = pair_block[target]
                block = index[block_id]
                row = block["pair_ids"].index(target)
                if int(hit["target_code"]) != int(block["codes"][row]):
                    _fail(
                        "hit_target_code",
                        location,
                        "Hit code differs from indexed code",
                        block_id=block_id,
                        version=int(snapshot["codebook"]["version"]),
                    )
                if hit["target_residual_hash"] != array_digest(
                    block["residuals"][row]
                ):
                    _fail(
                        "hit_residual_hash",
                        location,
                        "Hit residual hash differs from index block",
                        block_id=block_id,
                        version=int(snapshot["codebook"]["version"]),
                    )
                if hit["target_reconstructed_hash"] != array_digest(
                    block["reconstructed"][row]
                ):
                    _fail(
                        "hit_reconstruction_hash",
                        location,
                        "Hit reconstruction digest differs",
                        block_id=block_id,
                        version=int(snapshot["codebook"]["version"]),
                    )
                if hit["cache_keys"]["index_build"] != block["entry"]["key"]:
                    _fail(
                        "hit_index_key",
                        location,
                        "Hit index cache key differs",
                    )
            expected_hits = _recompute_query(
                cache,
                snapshot,
                index,
                pair_block,
                query_pair,
                list(query["query_modality_mask"]),
            )
            if len(expected_hits) != len(hits):
                _fail(
                    "topk_length",
                    f"retrieve:{query_pair}",
                    "Recomputed top-k length differs",
                )
            for expected, actual in zip(expected_hits, hits, strict=True):
                if expected["rank"] != int(actual["rank"]):
                    _fail(
                        "topk_rank",
                        f"retrieve:{query_pair}#rank={actual['rank']}",
                        "Recomputed rank differs",
                    )
                if expected["target_pair_id"] != actual["target_pair_id"]:
                    _fail(
                        "topk_target",
                        f"retrieve:{query_pair}#rank={actual['rank']}",
                        "Recomputed top-k target differs",
                    )
                if float(expected["score"]) != float(actual["score"]):
                    _fail(
                        "topk_score",
                        f"retrieve:{query_pair}#rank={actual['rank']}",
                        "Recomputed score differs",
                    )
                if expected["block_id"] != int(actual["block_id"]):
                    _fail(
                        "topk_block",
                        f"retrieve:{query_pair}#rank={actual['rank']}",
                        "Recomputed block id differs",
                    )
                if expected["code"] != int(actual["target_code"]):
                    _fail(
                        "topk_code",
                        f"retrieve:{query_pair}#rank={actual['rank']}",
                        "Recomputed code differs",
                    )
            ordered = sorted(
                hits,
                key=lambda item: (-float(item["score"]), item["target_pair_id"]),
            )
            if ordered != hits:
                _fail(
                    "score_order",
                    f"retrieve:{query_pair}",
                    "Top-k hits are not ordered by score desc, pair_id asc",
                )
        expected_results_hash = sha256_bytes(result_bytes)
        if expected_results_hash != snapshot["retrieval_manifest"]["results_sha256"]:
            _fail(
                "results_digest",
                "retrieval_manifest",
                "Snapshot result digest differs from manifest bytes",
            )
        return {
            "schema": "cm-verify-report-v1",
            "ok": True,
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_version": int(snapshot["version"]),
            "checks": {
                "graph_acyclic": True,
                "lineage": True,
                "cache_keys_and_hashes": True,
                "node_sets_disjoint_and_complete": True,
                "hits_unique": True,
                "topk_order_and_reconstruction": True,
                "results_digest": True,
            },
            "counts": {
                "nodes": len(all_instances),
                "recomputed_nodes": len(recomputed),
                "reused_nodes": len(reused),
                "index_blocks": len(index),
                "queries": len(queries),
                "hits": len(seen_hits),
                "cache_entries_checked": len(all_instances) + 1 + len(index),
            },
            "results_sha256": expected_results_hash,
        }
    except PipelineError as exc:
        if exc.code == "E_VERIFY_FAIL":
            raise
        raise PipelineError(
            "E_VERIFY_FAIL",
            str(exc),
            stage="verify",
            details={
                "category": "audit_exception",
                "location": "verify_snapshot",
                "original_code": exc.code,
                "original_details": exc.details,
            },
        ) from exc


def assert_active_lineage(
    active_lineage: dict[str, Any],
    expected_lineage: dict[str, Any],
    *,
    location: str,
) -> None:
    if active_lineage != expected_lineage:
        diff_fields = sorted(
            key
            for key in set(active_lineage) | set(expected_lineage)
            if active_lineage.get(key) != expected_lineage.get(key)
        )
        raise PipelineError(
            "E_LINEAGE_BROKEN",
            "Retrieval result belongs to a stale dependency lineage",
            stage="lineage",
            field=diff_fields[0] if diff_fields else "lineage",
            details={
                "location": location,
                "different_fields": diff_fields,
                "active_lineage": active_lineage,
                "result_lineage": expected_lineage,
            },
        )

