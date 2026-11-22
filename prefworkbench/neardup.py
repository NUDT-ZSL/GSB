"""Chunk-signature near-duplicate candidate generation and connected components."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from fractions import Fraction
from typing import Any

from .errors import W_DEGRADED_GROUPING
from .quality import tokenize


SPACE_RE = re.compile(r"\s+", re.UNICODE)


def normalize_text(text: str) -> str:
    return SPACE_RE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def pair_signature(
    pair: dict[str, Any], chunk_tokens: int
) -> tuple[tuple[str, ...], list[str]]:
    normalized = normalize_text(
        f"{pair['prompt']}\n{pair['chosen']}\n{pair['rejected']}"
    )
    tokens = tokenize(normalized)
    if not tokens:
        return tuple(), []
    if len(tokens) <= chunk_tokens:
        chunks = [tuple(tokens)]
    else:
        chunks = [
            tuple(tokens[index : index + chunk_tokens])
            for index in range(0, len(tokens) - chunk_tokens + 1)
        ]
    hashes = sorted(
        {
            hashlib.blake2b(
                "\x1f".join(chunk).encode("utf-8"), digest_size=8
            ).hexdigest()
            for chunk in chunks
        }
    )
    return tuple(tokens), hashes


def signature_candidates(
    pairs: list[dict[str, Any]], chunk_tokens: int
) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    signatures: dict[str, list[str]] = {}
    inverted: dict[str, list[str]] = {}
    for pair in sorted(pairs, key=lambda item: str(item["pair_id"])):
        pair_id = str(pair["pair_id"])
        _, hashes = pair_signature(pair, chunk_tokens)
        signatures[pair_id] = hashes
        for signature in hashes:
            inverted.setdefault(signature, []).append(pair_id)
    candidates: set[tuple[str, str]] = set()
    for pair_ids in inverted.values():
        ordered = sorted(pair_ids)
        for left_index, left in enumerate(ordered):
            for right in ordered[left_index + 1 :]:
                candidates.add((left, right))
    return signatures, sorted(candidates)


def all_pair_candidates(pair_ids: list[str]) -> list[tuple[str, str]]:
    ordered = sorted(pair_ids)
    return [
        (ordered[left], ordered[right])
        for left in range(len(ordered))
        for right in range(left + 1, len(ordered))
    ]


def similarity_for_ids(
    left: str,
    right: str,
    signature_map: dict[str, list[str]],
) -> tuple[Fraction, int, int]:
    left_set = set(signature_map[left])
    right_set = set(signature_map[right])
    union = left_set | right_set
    if not union:
        return Fraction(1, 1), 0, 0
    shared = len(left_set & right_set)
    return Fraction(shared, len(union)), shared, len(union)


class UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {item: item for item in items}
        self.rank = {item: 0 for item in items}

    def find(self, item: str) -> str:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def _rounded_fraction(value: Fraction, decimals: int = 8) -> float:
    return round(float(value), decimals)


def compute_neardup(
    pairs: list[dict[str, Any]],
    quality_records: dict[str, dict[str, Any]],
    neardup_config: dict[str, Any],
    *,
    candidate_mode: str,
    comparison_limit: int,
    decimals: int = 8,
) -> dict[str, Any]:
    ordered_pairs = sorted(pairs, key=lambda item: str(item["pair_id"]))
    pair_ids = [str(pair["pair_id"]) for pair in ordered_pairs]
    chunk_tokens = int(neardup_config["chunk_tokens"])
    signature_map, signature_candidate_pairs = signature_candidates(
        ordered_pairs, chunk_tokens
    )
    if candidate_mode == "all":
        candidate_pairs = all_pair_candidates(pair_ids)
    elif candidate_mode == "signature":
        candidate_pairs = signature_candidate_pairs
    elif candidate_mode == "none":
        candidate_pairs = []
    else:
        raise ValueError(f"unknown candidate mode: {candidate_mode}")

    bounded_limit = max(0, min(int(comparison_limit), len(candidate_pairs)))
    comparisons_to_run = candidate_pairs[:bounded_limit]
    threshold = Fraction(str(neardup_config["threshold"]))
    tolerance = Fraction(str(neardup_config.get("tolerance", 0.0)))
    if neardup_config["threshold_mode"] == "strict":
        effective_threshold = threshold
    else:
        effective_threshold = max(Fraction(0, 1), threshold - tolerance)

    union_find = UnionFind(pair_ids)
    comparison_records: list[dict[str, Any]] = []
    for left, right in comparisons_to_run:
        similarity, shared, union_size = similarity_for_ids(left, right, signature_map)
        is_similar = similarity >= effective_threshold
        comparison_records.append(
            {
                "pair_a": left,
                "pair_b": right,
                "similarity": _rounded_fraction(similarity, decimals),
                "shared_signatures": shared,
                "union_signatures": union_size,
                "similar": is_similar,
            }
        )
        if is_similar:
            union_find.union(left, right)

    groups: dict[str, list[str]] = {}
    for pair_id in pair_ids:
        groups.setdefault(union_find.find(pair_id), []).append(pair_id)
    cluster_members = [sorted(members) for members in groups.values()]

    def representative_key(pair_id: str) -> tuple[float, str]:
        score = float(quality_records[pair_id]["pair_score"])
        return (-score, pair_id)

    representatives = {
        min(members, key=representative_key): sorted(members)
        for members in cluster_members
    }
    ordered_representatives = sorted(representatives)
    assignments: dict[str, dict[str, Any]] = {}
    cluster_output: list[dict[str, Any]] = []
    for index, representative in enumerate(ordered_representatives, start=1):
        cluster_id = f"C{index:05d}"
        members = representatives[representative]
        for member in members:
            similarity, shared, union_size = similarity_for_ids(
                representative, member, signature_map
            )
            assignments[member] = {
                "cluster_id": cluster_id,
                "cluster_size": len(members),
                "representative_pair_id": representative,
                "is_representative": member == representative,
                "similarity_to_representative": _rounded_fraction(similarity, decimals),
                "shared_signatures_with_representative": shared,
                "union_signatures_with_representative": union_size,
            }
        cluster_output.append(
            {
                "cluster_id": cluster_id,
                "representative_pair_id": representative,
                "members": members,
                "representative_quality": float(
                    quality_records[representative]["pair_score"]
                ),
            }
        )

    if sorted(assignments) != pair_ids:
        raise AssertionError("near-duplicate assignment does not cover every pair exactly once")
    if len({item["representative_pair_id"] for item in cluster_output}) != len(
        cluster_output
    ):
        raise AssertionError("cluster representatives are not unique")

    degraded = candidate_mode != "all" or bounded_limit < len(candidate_pairs)
    warnings = [W_DEGRADED_GROUPING] if degraded else []
    return {
        "threshold": float(threshold),
        "tolerance": float(tolerance),
        "effective_threshold": _rounded_fraction(effective_threshold, decimals),
        "threshold_mode": neardup_config["threshold_mode"],
        "chunk_tokens": chunk_tokens,
        "candidate_mode": candidate_mode,
        "candidate_pair_count": len(candidate_pairs),
        "comparison_limit": bounded_limit,
        "comparison_count": len(comparison_records),
        "degraded_grouping": degraded,
        "warnings": warnings,
        "comparisons": comparison_records,
        "clusters": cluster_output,
        "assignments": {
            pair_id: assignments[pair_id] for pair_id in sorted(assignments)
        },
        "signature_counts": {
            pair_id: len(signature_map[pair_id]) for pair_id in sorted(signature_map)
        },
    }
