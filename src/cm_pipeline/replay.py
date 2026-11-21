from __future__ import annotations

from typing import Any

from .cache import CacheStore
from .canonical import array_digest
from .errors import PipelineError
from .state import StateStore
from .verify import _load_manifest, _load_node_arrays, _recompute_query, _pair_block_map, _load_index


def load_results_bytes(
    *,
    cache_root: str,
    state_root: str,
    snapshot_version: int | None = None,
) -> bytes:
    cache = CacheStore(cache_root)
    state = StateStore(state_root)
    snapshot = state.load_snapshot(snapshot_version)
    if snapshot is None:
        raise PipelineError(
            "E_LINEAGE_BROKEN",
            "No snapshot is available",
            stage="results",
            field="snapshot",
        )
    _, result_bytes = _load_manifest(cache, snapshot)
    return result_bytes


def replay_snapshot(
    *,
    cache_root: str,
    state_root: str,
    snapshot_version: int | None = None,
    require_current: bool = True,
) -> dict[str, Any]:
    cache = CacheStore(cache_root)
    state = StateStore(state_root)
    state_data = state.read_state()
    if snapshot_version is None:
        snapshot_version = int(state_data["current_version"])
    snapshot = state.load_snapshot(int(snapshot_version))
    if snapshot is None:
        raise PipelineError(
            "E_LINEAGE_BROKEN",
            "No snapshot is available for replay",
            stage="replay",
            field="snapshot",
        )
    if require_current:
        current_version = int(state_data["current_version"])
        if int(snapshot_version) != current_version:
            raise PipelineError(
                "E_LINEAGE_BROKEN",
                "Historical snapshot cannot be served as the current result",
                stage="replay",
                field="snapshot",
                details={
                    "requested_version": int(snapshot_version),
                    "current_version": current_version,
                },
            )
    result_doc, _ = _load_manifest(cache, snapshot)
    index = _load_index(cache, snapshot)
    pair_block = _pair_block_map(index)
    records: list[dict[str, Any]] = []
    for query in result_doc["queries"]:
        query_pair = query["query_pair_id"]
        expected = _recompute_query(
            cache,
            snapshot,
            index,
            pair_block,
            query_pair,
            list(query["query_modality_mask"]),
        )
        actual_hits = query["hits"]
        if len(expected) != len(actual_hits):
            raise PipelineError(
                "E_INCONSISTENT",
                "Replay top-k length differs",
                stage="replay",
                pair_id=query_pair,
                details={"location": f"retrieve:{query_pair}", "category": "topk_length"},
            )
        for expected_hit, hit in zip(expected, actual_hits, strict=True):
            location = f"retrieve:{query_pair}#rank={hit['rank']}"
            if expected_hit["target_pair_id"] != hit["target_pair_id"]:
                raise PipelineError(
                    "E_INCONSISTENT",
                    "Replay ordering differs",
                    stage="replay",
                    pair_id=query_pair,
                    details={"location": location, "category": "topk_target"},
                )
            if expected_hit["score"] != float(hit["score"]):
                raise PipelineError(
                    "E_INCONSISTENT",
                    "Replay score differs",
                    stage="replay",
                    pair_id=query_pair,
                    details={
                        "location": location,
                        "category": "score",
                        "expected": expected_hit["score"],
                        "actual": float(hit["score"]),
                    },
                )
            query_arrays, _ = _load_node_arrays(
                cache,
                snapshot,
                f"align:{query_pair}",
            )
            if array_digest(query_arrays["image_aligned"]) != hit["query_vector_hash"]:
                raise PipelineError(
                    "E_INCONSISTENT",
                    "Replay query vector hash differs",
                    stage="replay",
                    pair_id=query_pair,
                    details={"location": location, "category": "query_vector_hash"},
                )
            records.append(
                {
                    "location": location,
                    "target_pair_id": hit["target_pair_id"],
                    "score": expected_hit["score"],
                    "target_code": expected_hit["code"],
                    "index_block": expected_hit["block_id"],
                    "reconstructed_hash": expected_hit["reconstructed_hash"],
                    "cache_keys": hit["cache_keys"],
                    "vocab_version": hit["vocab_version"],
                    "codebook_version": hit["codebook_version"],
                    "matches": True,
                }
            )
    return {
        "schema": "cm-replay-report-v1",
        "ok": True,
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_version": int(snapshot["version"]),
        "replayed_hits": len(records),
        "records": records,
    }


def replay_hit(
    hit: dict[str, Any],
    *,
    cache_root: str,
    state_root: str,
    require_current: bool = True,
) -> dict[str, Any]:
    state = StateStore(state_root)
    state_data = state.read_state()
    current = state.load_snapshot(int(state_data["current_version"]))
    if current is None:
        raise PipelineError(
            "E_LINEAGE_BROKEN",
            "No current snapshot is available",
            stage="replay",
            field="snapshot",
        )
    owning_version: int | None = None
    owning_snapshot: dict[str, Any] | None = None
    retrieve_key = hit.get("cache_keys", {}).get("retrieve")
    retrieve_instance = f"retrieve:{hit.get('query_pair_id')}"
    for version in sorted(int(item) for item in state_data.get("versions", [])):
        candidate = state.load_snapshot(version)
        if candidate is None:
            continue
        node = candidate.get("nodes", {}).get(retrieve_instance)
        if node is not None and node.get("key") == retrieve_key:
            owning_version = version
            owning_snapshot = candidate
            break
    if owning_snapshot is None or owning_version is None:
        raise PipelineError(
            "E_LINEAGE_BROKEN",
            "Hit cache key does not belong to any recorded snapshot",
            stage="replay",
            field="cache_keys.retrieve",
            pair_id=hit.get("query_pair_id"),
            details={"location": f"{retrieve_instance}#rank={hit.get('rank')}"},
        )
    if require_current:
        current_node = current.get("nodes", {}).get(retrieve_instance)
        if current_node is None or current_node.get("key") != retrieve_key:
            raise PipelineError(
                "E_LINEAGE_BROKEN",
                "Hit belongs to a stale retrieval dependency set",
                stage="replay",
                field="cache_keys.retrieve",
                pair_id=hit.get("query_pair_id"),
                details={
                    "location": f"{retrieve_instance}#rank={hit.get('rank')}",
                    "hit_retrieve_key": retrieve_key,
                    "current_retrieve_key": (
                        current_node.get("key") if current_node else None
                    ),
                },
            )
        owning_version = int(current["version"])
    report = replay_snapshot(
        cache_root=cache_root,
        state_root=state_root,
        snapshot_version=int(owning_version),
        require_current=False,
    )
    location = f"retrieve:{hit['query_pair_id']}#rank={hit['rank']}"
    for record in report["records"]:
        if record["location"] == location:
            return record
    raise PipelineError(
        "E_LINEAGE_BROKEN",
        "Hit does not exist in the referenced retrieval snapshot",
        stage="replay",
        field="hit",
        pair_id=hit.get("query_pair_id"),
        details={"location": location},
    )

