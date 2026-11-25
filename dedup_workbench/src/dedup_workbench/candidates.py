"""Hash-bucket grouping and bounded candidate-pair generation."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .config import stage_parameters


def candidate_cache_inputs(signatures: list[dict[str, Any]], config: dict[str, Any], signature_hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "signature_hashes": dict(sorted(signature_hashes.items())),
        "candidate_parameters": stage_parameters(config, "candidate"),
    }


def make_pair_id(doc_id_a: str, doc_id_b: str) -> str:
    left, right = sorted((doc_id_a, doc_id_b))
    return f"{left}⇔{right}"


def generate_candidates(signatures: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    window = int(config["candidate_window"])
    ordered = sorted(signatures, key=lambda item: (item["shard_id"], item["doc_id"]))
    by_id = {item["doc_id"]: item for item in ordered}

    bucket_members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signature in ordered:
        for bucket_key in signature["bucket_keys"]:
            bucket_members[bucket_key].append(signature)

    bucket_records: list[dict[str, Any]] = []
    pair_sources: dict[str, dict[str, Any]] = {}

    for bucket_key in sorted(bucket_members):
        members = sorted(bucket_members[bucket_key], key=lambda item: (item["shard_id"], item["doc_id"]))
        member_refs = [
            {"rank": rank, "shard_id": item["shard_id"], "doc_id": item["doc_id"]}
            for rank, item in enumerate(members)
        ]
        bucket_records.append({"bucket_key": bucket_key, "size": len(members), "members": member_refs})

        for rank_left, left in enumerate(members):
            max_right = min(len(members), rank_left + 1 + window)
            for rank_right in range(rank_left + 1, max_right):
                right = members[rank_right]
                pair_id = make_pair_id(left["doc_id"], right["doc_id"])
                doc_a, doc_b = sorted((left["doc_id"], right["doc_id"]))
                source = {
                    "bucket_key": bucket_key,
                    "bucket_size": len(members),
                    "rank_left": rank_left,
                    "rank_right": rank_right,
                    "rank_distance": rank_right - rank_left,
                    "window": window,
                    "left_doc_id": left["doc_id"],
                    "right_doc_id": right["doc_id"],
                }
                pair = pair_sources.setdefault(
                    pair_id,
                    {
                        "pair_id": pair_id,
                        "doc_id_a": doc_a,
                        "doc_id_b": doc_b,
                        "source_buckets": [],
                    },
                )
                pair["source_buckets"].append(source)

    pairs: list[dict[str, Any]] = []
    doc_pair_index: dict[str, list[str]] = {doc_id: [] for doc_id in by_id}
    for pair_id in sorted(pair_sources):
        pair = pair_sources[pair_id]
        pair["source_buckets"].sort(key=lambda source: source["bucket_key"])
        pair["primary_bucket"] = pair["source_buckets"][0]["bucket_key"]
        pairs.append(pair)
        doc_pair_index[pair["doc_id_a"]].append(pair_id)
        doc_pair_index[pair["doc_id_b"]].append(pair_id)

    for doc_id in doc_pair_index:
        doc_pair_index[doc_id].sort()

    return {
        "candidate_window": window,
        "bucket_count": len(bucket_records),
        "buckets": bucket_records,
        "pair_count": len(pairs),
        "pairs": pairs,
        "doc_pair_index": dict(sorted(doc_pair_index.items())),
    }
