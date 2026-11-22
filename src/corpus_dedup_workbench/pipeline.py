from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from typing import Any

from .config import WorkbenchConfig
from .errors import E_RESOURCE_LIMIT, WorkbenchError
from .signatures import build_signature_record
from .threshold import stable_threshold_decision
from .util import content_hash, sha256_text

SCHEMA_VERSION = "dedup-artifact-v1"


def _quality_score(doc: dict[str, Any]) -> float:
    value = doc.get("quality_score")
    if value is not None:
        return float(value)
    digest = sha256_text(str(doc.get("text", "")))[:8]
    return round(int(digest, 16) / 0xFFFFFFFF * 100.0, 6)


def _candidate_id(bucket_key: str, a_doc: str, b_doc: str) -> str:
    return "cp-" + hashlib.sha256(
        f"{bucket_key}|{a_doc}|{b_doc}".encode()
    ).hexdigest()[:20]


def _full_compare(
    a_sig: dict[str, Any],
    b_sig: dict[str, Any],
    config: WorkbenchConfig,
) -> tuple[list[dict[str, Any]], int, int]:
    hits: list[dict[str, Any]] = []
    a_blocks = a_sig["block_signatures"]
    b_blocks = b_sig["block_signatures"]
    for position in range(min(len(a_blocks), len(b_blocks))):
        if a_blocks[position] == b_blocks[position]:
            hits.append(
                {
                    "a_position": position,
                    "b_position": position,
                    "signature": a_blocks[position],
                }
            )
    return hits, len(hits), max(len(a_blocks), len(b_blocks), 1)


def _anchor_map(signature_record: dict[str, Any]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = defaultdict(list)
    blocks = signature_record["block_signatures"]
    for position in signature_record["summary"]["anchor_positions"]:
        result[blocks[position]].append(position)
    return dict(result)


def _digest_compare(
    a_sig: dict[str, Any],
    b_sig: dict[str, Any],
    config: WorkbenchConfig,
) -> tuple[list[dict[str, Any]], int, int, bool, str | None, list[dict[str, Any]]]:
    a_bands = a_sig["summary"]["bands"]
    b_bands = b_sig["summary"]["bands"]
    common_band_indexes = [
        index
        for index, (a_band, b_band) in enumerate(zip(a_bands, b_bands))
        if a_band == b_band
    ]
    common_bands = [
        {"band_index": index, "band": a_bands[index]} for index in common_band_indexes
    ]
    if len(common_band_indexes) < config.digest_min_common_bands:
        denominator = max(
            len(a_sig["summary"]["anchor_positions"]),
            len(b_sig["summary"]["anchor_positions"]),
            1,
        )
        return (
            [],
            0,
            denominator,
            True,
            "summary_conflict_omission",
            common_bands,
        )

    a_anchors = _anchor_map(a_sig)
    b_anchors = _anchor_map(b_sig)
    hits: list[dict[str, Any]] = []
    used_b_positions: set[int] = set()
    for signature in sorted(set(a_anchors) & set(b_anchors)):
        b_position = next(
            position
            for position in b_anchors[signature]
            if position not in used_b_positions
        )
        a_position = a_anchors[signature][0]
        hits.append(
            {
                "a_position": a_position,
                "b_position": b_position,
                "signature": signature,
            }
        )
        used_b_positions.add(b_position)
    denominator = max(len(a_anchors), len(b_anchors), 1)
    return hits, len(hits), denominator, False, None, common_bands


def _trace_entry(
    candidate: dict[str, Any],
    doc_id: str,
) -> dict[str, Any]:
    is_a = candidate["a_doc"] == doc_id
    peer = candidate["b_doc"] if is_a else candidate["a_doc"]
    hit_positions = [
        {
            "own_position": hit["a_position"] if is_a else hit["b_position"],
            "peer_position": hit["b_position"] if is_a else hit["a_position"],
            "signature": hit["signature"],
        }
        for hit in candidate["hit_positions"]
    ]
    return {
        "candidate_id": candidate["candidate_id"],
        "peer_doc_id": peer,
        "direction": "a" if is_a else "b",
        "bucket_key": candidate["bucket_key"],
        "window_distance": candidate["window_distance"],
        "summary_relation": candidate["summary_relation"],
        "common_bands": candidate["common_bands"],
        "hit_positions": hit_positions,
        "similarity": candidate["similarity"],
        "near_duplicate": candidate["near_duplicate"],
        "skipped": candidate["skipped"],
        "skip_reason": candidate["skip_reason"],
        "threshold_trace": candidate["threshold_trace"],
    }


def process_shard(
    shard_id: str,
    docs: list[dict[str, Any]],
    config: WorkbenchConfig,
    pair_limit: int | None = None,
) -> dict[str, Any]:
    shard_docs = sorted(docs, key=lambda item: item["doc_id"])
    shard_content_hash = content_hash(shard_docs)
    completed: list[dict[str, Any]] = [{"name": "ingest_validate", "status": "completed"}]

    if len(shard_docs) > config.max_docs_per_batch:
        raise WorkbenchError(
            E_RESOURCE_LIMIT,
            "单批文档数超过配置上限",
            stage="ingest_validate",
            details={
                "shard_id": shard_id,
                "doc_count": len(shard_docs),
                "max_docs_per_batch": config.max_docs_per_batch,
            },
            completed_stages=[],
        )

    signatures: dict[str, dict[str, Any]] = {}
    quality: dict[str, float] = {}
    for doc in shard_docs:
        signatures[doc["doc_id"]] = build_signature_record(doc, config)
        quality[doc["doc_id"]] = _quality_score(doc)
    completed.append({"name": "signature_generation", "status": "completed"})

    bucket_members: dict[str, list[str]] = defaultdict(list)
    for doc in shard_docs:
        bucket_members[signatures[doc["doc_id"]]["bucket_key"]].append(doc["doc_id"])
    for member_ids in bucket_members.values():
        member_ids.sort()
    completed.append({"name": "bucket_and_summary", "status": "completed"})

    buckets: list[dict[str, Any]] = []
    pending_pairs: list[dict[str, Any]] = []
    for bucket_key in sorted(bucket_members):
        member_ids = bucket_members[bucket_key]
        bucket_candidates: list[str] = []
        for index, a_doc in enumerate(member_ids):
            start = max(0, index - config.candidate_window)
            for b_index in range(start, index):
                b_doc = member_ids[b_index]
                a_sig = signatures[a_doc]
                b_sig = signatures[b_doc]
                summary_relation = (
                    "same"
                    if a_sig["summary"]["key"] == b_sig["summary"]["key"]
                    else "conflict"
                )
                pair = {
                    "candidate_id": _candidate_id(bucket_key, a_doc, b_doc),
                    "bucket_key": bucket_key,
                    "a_doc": a_doc,
                    "b_doc": b_doc,
                    "a_index": b_index,
                    "b_index": index,
                    "window_distance": index - b_index,
                    "summary_relation": summary_relation,
                    "summary_a": a_sig["summary"]["key"],
                    "summary_b": b_sig["summary"]["key"],
                }
                bucket_candidates.append(pair["candidate_id"])
                pending_pairs.append(pair)
        buckets.append(
            {
                "bucket_key": bucket_key,
                "doc_ids": member_ids,
                "size": len(member_ids),
                "summary_groups": [
                    {"summary_key": key, "doc_ids": ids}
                    for key, ids in sorted(
                        (
                            (key, sorted(group_ids))
                            for key, group_ids in _group_by_summary(
                                member_ids, signatures
                            ).items()
                        )
                    )
                ],
                "candidate_ids": bucket_candidates,
                "candidate_count": len(bucket_candidates),
                "empty_candidates": len(bucket_candidates) == 0,
            }
        )

    generated_pair_count = len(pending_pairs)
    degradation: dict[str, Any] | None = None
    effective_pair_limit = config.max_candidate_comparisons
    if pair_limit is not None:
        effective_pair_limit = min(effective_pair_limit, max(0, pair_limit))
    if generated_pair_count > effective_pair_limit:
        if config.resource_policy == "error":
            raise WorkbenchError(
                E_RESOURCE_LIMIT,
                "候选比较次数超过配置上限",
                stage="candidate_comparison",
                details={
                    "shard_id": shard_id,
                    "generated_candidate_pairs": generated_pair_count,
                    "max_candidate_comparisons": config.max_candidate_comparisons,
                    "effective_batch_remaining": effective_pair_limit,
                },
                completed_stages=completed,
            )
        pending_pairs = pending_pairs[:effective_pair_limit]
        degradation = {
            "degraded": True,
            "category": "candidate_comparisons_truncated",
            "generated_candidate_pairs": generated_pair_count,
            "compared_candidate_pairs": len(pending_pairs),
            "skipped_candidate_pairs": generated_pair_count - len(pending_pairs),
            "effective_batch_remaining": effective_pair_limit,
        }

    candidates: list[dict[str, Any]] = []
    for pair in pending_pairs:
        a_sig = signatures[pair["a_doc"]]
        b_sig = signatures[pair["b_doc"]]
        if config.validation_mode == "full":
            hits, matched, denominator = _full_compare(a_sig, b_sig, config)
            common_bands = [
                {
                    "band_index": index,
                    "band": band,
                }
                for index, (band, other) in enumerate(
                    zip(a_sig["summary"]["bands"], b_sig["summary"]["bands"])
                )
                if band == other
            ]
            skipped = False
            skip_reason = None
            comparison_stage = "full_block"
        else:
            (
                hits,
                matched,
                denominator,
                skipped,
                skip_reason,
                common_bands,
            ) = _digest_compare(a_sig, b_sig, config)
            comparison_stage = "digest_anchor"

        threshold_trace = stable_threshold_decision(
            matched,
            denominator,
            config.threshold,
            config.threshold_policy,
            config.tolerance,
        )
        near_duplicate = (
            False if skipped else bool(threshold_trace["decision"])
        )
        candidate = {
            **pair,
            "comparison_stage": comparison_stage,
            "summary_relation": pair["summary_relation"],
            "common_bands": common_bands,
            "common_band_count": len(common_bands),
            "digest_min_common_bands": config.digest_min_common_bands,
            "hit_positions": hits,
            "matched_blocks": matched,
            "compared_blocks": denominator,
            "similarity": threshold_trace["score"],
            "similarity_fraction": threshold_trace["score_fraction"],
            "threshold_trace": threshold_trace,
            "near_duplicate": near_duplicate,
            "skipped": skipped,
            "skip_reason": skip_reason,
            "dedup_effect": None,
        }
        candidates.append(candidate)
    completed.append({"name": "candidate_comparison", "status": "completed"})

    doc_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    doc_candidate_ids: dict[str, list[str]] = defaultdict(list)
    for candidate in candidates:
        doc_trace[candidate["a_doc"]].append(_trace_entry(candidate, candidate["a_doc"]))
        doc_trace[candidate["b_doc"]].append(_trace_entry(candidate, candidate["b_doc"]))
        doc_candidate_ids[candidate["a_doc"]].append(candidate["candidate_id"])
        doc_candidate_ids[candidate["b_doc"]].append(candidate["candidate_id"])
        if candidate["near_duplicate"]:
            adjacency[candidate["a_doc"]].append(candidate)
            adjacency[candidate["b_doc"]].append(candidate)

    for trace in doc_trace.values():
        trace.sort(key=lambda item: item["candidate_id"])
    for candidate_ids in doc_candidate_ids.values():
        candidate_ids.sort()

    doc_order = sorted(
        [doc["doc_id"] for doc in shard_docs],
        key=lambda doc_id: (-quality[doc_id], doc_id),
    )
    retained: set[str] = set()
    removed: set[str] = set()
    decisions: dict[str, dict[str, Any]] = {}
    removal_reasons: dict[str, dict[str, Any]] = {}

    for doc_id in doc_order:
        retained_neighbors = [
            candidate
            for candidate in adjacency.get(doc_id, [])
            if (
                candidate["b_doc"]
                if candidate["a_doc"] == doc_id
                else candidate["a_doc"]
            )
            in retained
        ]
        if retained_neighbors:
            retained_neighbors.sort(
                key=lambda candidate: (
                    -quality[
                        candidate["b_doc"]
                        if candidate["a_doc"] == doc_id
                        else candidate["a_doc"]
                    ],
                    candidate["b_doc"]
                    if candidate["a_doc"] == doc_id
                    else candidate["a_doc"],
                )
            )
            candidate = retained_neighbors[0]
            neighbor = (
                candidate["b_doc"]
                if candidate["a_doc"] == doc_id
                else candidate["a_doc"]
            )
            removed.add(doc_id)
            decisions[doc_id] = {
                "kept": False,
                "outcome": "removed_near_duplicate",
                "representative_doc_id": neighbor,
                "near_neighbor_count": len(retained_neighbors),
                "quality_score": quality[doc_id],
            }
            removal_reasons[doc_id] = {
                "doc_id": doc_id,
                "reason": "near_duplicate",
                "candidate_id": candidate["candidate_id"],
                "neighbor_doc_id": neighbor,
                "bucket_key": candidate["bucket_key"],
                "summary_key": signatures[doc_id]["summary"]["key"],
                "neighbor_summary_key": signatures[neighbor]["summary"]["key"],
                "hit_positions": candidate["hit_positions"],
                "similarity": candidate["similarity"],
                "similarity_fraction": candidate["similarity_fraction"],
                "threshold_trace": candidate["threshold_trace"],
                "comparison_stage": candidate["comparison_stage"],
            }
            candidate["dedup_effect"] = "one_removed"
        else:
            retained.add(doc_id)
            decisions[doc_id] = {
                "kept": True,
                "outcome": "kept",
                "representative_doc_id": doc_id,
                "near_neighbor_count": 0,
                "quality_score": quality[doc_id],
            }
    completed.append({"name": "dedup_selection", "status": "completed"})

    for candidate in candidates:
        a_removed = candidate["a_doc"] in removed
        b_removed = candidate["b_doc"] in removed
        if a_removed and b_removed:
            candidate["dedup_effect"] = "both_removed"
        elif a_removed:
            candidate["dedup_effect"] = "a_removed"
        elif b_removed:
            candidate["dedup_effect"] = "b_removed"
        else:
            candidate["dedup_effect"] = "both_kept"

    doc_records: list[dict[str, Any]] = []
    for doc in shard_docs:
        sig = signatures[doc["doc_id"]]
        decision = decisions[doc["doc_id"]]
        doc_records.append(
            {
                "doc_id": doc["doc_id"],
                "shard_id": doc["shard_id"],
                "lang": doc.get("lang"),
                "source": doc.get("source"),
                "token_estimate": doc.get("token_estimate"),
                "quality_score": quality[doc["doc_id"]],
                "text_sha256": sha256_text(str(doc.get("text", ""))),
                "text_excerpt": str(doc.get("text", ""))[:160],
                "normalized_token_count": sig["normalized_token_count"],
                "block_signatures": sig["block_signatures"],
                "blocks": sig["blocks"],
                "summary": sig["summary"],
                "bucket_key": sig["bucket_key"],
                "candidate_ids": doc_candidate_ids.get(doc["doc_id"], []),
                "candidate_trace": doc_trace.get(doc["doc_id"], []),
                "similar_decision": decision,
                "removed": doc["doc_id"] in removed,
                "removal_reason": removal_reasons.get(doc["doc_id"]),
                "content_hash": sig["content_hash"],
            }
        )

    block_counts = [len(record["block_signatures"]) for record in doc_records]
    prefix_counter = Counter(
        signature[:4]
        for record in doc_records
        for signature in record["block_signatures"]
    )
    summary_conflicts = sum(
        1 for candidate in candidates if candidate["summary_relation"] == "conflict"
    )
    skipped = sum(1 for candidate in candidates if candidate["skipped"])
    stats = {
        "shard_id": shard_id,
        "doc_count": len(doc_records),
        "kept_count": len(retained),
        "removed_count": len(removed),
        "bucket_count": len(buckets),
        "empty_bucket_count": sum(1 for bucket in buckets if bucket["empty_candidates"]),
        "generated_candidate_pairs": generated_pair_count,
        "candidate_pair_count": len(candidates),
        "comparison_count": len(candidates) - skipped,
        "skipped_candidate_count": skipped,
        "summary_same_count": len(candidates) - summary_conflicts,
        "summary_conflict_count": summary_conflicts,
        "near_duplicate_pair_count": sum(
            1 for candidate in candidates if candidate["near_duplicate"]
        ),
        "signature_block_count": sum(block_counts),
        "mean_signature_blocks": (
            round(sum(block_counts) / len(block_counts), 6) if block_counts else 0.0
        ),
        "signature_distribution": {
            "block_count_histogram": {
                str(key): value
                for key, value in sorted(Counter(block_counts).items())
            },
            "signature_prefix_histogram": {
                key: value for key, value in sorted(prefix_counter.items())
            },
        },
        "summary_relation_counts": {
            "same": len(candidates) - summary_conflicts,
            "conflict": summary_conflicts,
        },
        "degraded": bool(degradation),
        "degradation": degradation,
    }
    completed.append({"name": "shard_statistics", "status": "completed"})

    return {
        "schema_version": SCHEMA_VERSION,
        "shard_id": shard_id,
        "shard_content_hash": shard_content_hash,
        "config_hash": config.config_hash,
        "config": config.to_dict(),
        "docs": doc_records,
        "buckets": buckets,
        "candidate_pairs": candidates,
        "removed": [
            removal_reasons[doc_id] for doc_id in sorted(removal_reasons)
        ],
        "kept": sorted(retained),
        "stats": stats,
        "completed_stages": completed,
    }


def _group_by_summary(
    member_ids: list[str],
    signatures: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for doc_id in member_ids:
        groups[signatures[doc_id]["summary"]["key"]].append(doc_id)
    return dict(groups)


def merge_shard_artifacts(
    artifacts: list[dict[str, Any]],
    config: WorkbenchConfig,
) -> dict[str, Any]:
    docs = {
        doc["doc_id"]: doc
        for artifact in artifacts
        for doc in artifact["docs"]
    }
    candidates = [
        candidate
        for artifact in artifacts
        for candidate in artifact["candidate_pairs"]
    ]
    removed = [
        reason for artifact in artifacts for reason in artifact["removed"]
    ]
    kept = sorted(doc_id for artifact in artifacts for doc_id in artifact["kept"])
    buckets = [
        {**bucket, "shard_id": artifact["shard_id"]}
        for artifact in artifacts
        for bucket in artifact["buckets"]
    ]
    shard_stats = {
        artifact["shard_id"]: artifact["stats"] for artifact in artifacts
    }
    totals = {
        "doc_count": len(docs),
        "kept_count": len(kept),
        "removed_count": len(removed),
        "bucket_count": len(buckets),
        "empty_bucket_count": sum(
            1 for bucket in buckets if bucket["empty_candidates"]
        ),
        "generated_candidate_pairs": sum(
            stats["generated_candidate_pairs"] for stats in shard_stats.values()
        ),
        "candidate_pair_count": len(candidates),
        "comparison_count": sum(
            stats["comparison_count"] for stats in shard_stats.values()
        ),
        "skipped_candidate_count": sum(
            stats["skipped_candidate_count"] for stats in shard_stats.values()
        ),
        "near_duplicate_pair_count": sum(
            stats["near_duplicate_pair_count"] for stats in shard_stats.values()
        ),
        "signature_block_count": sum(
            stats["signature_block_count"] for stats in shard_stats.values()
        ),
        "degraded": any(stats["degraded"] for stats in shard_stats.values()),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "config": config.to_dict(),
        "config_hash": config.config_hash,
        "shard_content_hashes": {
            artifact["shard_id"]: artifact["shard_content_hash"]
            for artifact in artifacts
        },
        "shard_stats": shard_stats,
        "totals": totals,
        "docs": docs,
        "buckets": buckets,
        "candidate_pairs": candidates,
        "removed": removed,
        "kept": kept,
        "artifacts": {
            artifact["shard_id"]: artifact for artifact in artifacts
        },
    }

