"""Pairwise block-signature and digest comparison."""
from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
from typing import Any

from .canonical import hash_json
from .config import stage_parameters


def comparison_cache_inputs(pair: dict[str, Any], signature_a: dict[str, Any], signature_b: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    return {
        "pair_id": pair["pair_id"],
        "signature_hashes": {
            signature_a["doc_id"]: signature_a["doc_digest"],
            signature_b["doc_id"]: signature_b["doc_digest"],
        },
        "block_counts": {
            signature_a["doc_id"]: signature_a["block_count"],
            signature_b["doc_id"]: signature_b["block_count"],
        },
        "upstream_signature_artifact_hashes": {
            signature_a["doc_id"]: hash_json(signature_a),
            signature_b["doc_id"]: hash_json(signature_b),
        },
        "comparison_parameters": stage_parameters(config, "comparison"),
    }


def _positions_by_signature(signature: dict[str, Any]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = defaultdict(list)
    for block in signature["blocks"]:
        positions[block["signature"]].append(int(block["position"]))
    return dict(positions)


def compare_pair(pair: dict[str, Any], signature_by_id: dict[str, dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    doc_a = pair["doc_id_a"]
    doc_b = pair["doc_id_b"]
    signature_a = signature_by_id[doc_a]
    signature_b = signature_by_id[doc_b]
    positions_a = _positions_by_signature(signature_a)
    positions_b = _positions_by_signature(signature_b)

    hits: list[dict[str, Any]] = []
    offset_counts: dict[str, int] = defaultdict(int)
    for block_signature in sorted(set(positions_a) & set(positions_b)):
        for position_a, position_b in zip(positions_a[block_signature], positions_b[block_signature]):
            offset = position_b - position_a
            offset_key = str(offset)
            offset_counts[offset_key] += 1
            hits.append(
                {
                    "signature": block_signature,
                    "position_a": position_a,
                    "position_b": position_b,
                    "offset_b_minus_a": offset,
                    "digest_status": "match",
                }
            )

    hits.sort(key=lambda hit: (hit["position_a"], hit["position_b"], hit["signature"]))

    blocks_a = signature_a["blocks"]
    blocks_b = signature_b["blocks"]
    aligned_checks: list[dict[str, Any]] = []
    aligned_matches = 0
    aligned_conflicts = 0
    for position in range(max(len(blocks_a), len(blocks_b))):
        block_a = blocks_a[position] if position < len(blocks_a) else None
        block_b = blocks_b[position] if position < len(blocks_b) else None
        if block_a is None:
            status = "eof_a"
            aligned_conflicts += 1
        elif block_b is None:
            status = "eof_b"
            aligned_conflicts += 1
        elif block_a["signature"] == block_b["signature"]:
            status = "match"
            aligned_matches += 1
        else:
            status = "conflict"
            aligned_conflicts += 1

        check = {
            "position": position,
            "offset_b_minus_a": 0,
            "digest_a": block_a["signature"] if block_a else None,
            "digest_b": block_b["signature"] if block_b else None,
            "digest_status": status,
        }
        if config["validation_mode"] == "full":
            aligned_checks.append(check)

    hit_count = len(hits)
    denominator = max(len(blocks_a), len(blocks_b))
    similarity_fraction = Fraction(hit_count, denominator)
    union_count = len(blocks_a) + len(blocks_b) - hit_count
    jaccard_fraction = Fraction(hit_count, union_count) if union_count else Fraction(1, 1)
    aligned_fraction = Fraction(aligned_matches, denominator)
    doc_digest_match = signature_a["doc_digest"] == signature_b["doc_digest"]

    result: dict[str, Any] = {
        "pair_id": pair["pair_id"],
        "doc_id_a": doc_a,
        "doc_id_b": doc_b,
        "source_buckets": pair["source_buckets"],
        "primary_bucket": pair["primary_bucket"],
        "validation_mode": config["validation_mode"],
        "block_count_a": len(blocks_a),
        "block_count_b": len(blocks_b),
        "hit_count": hit_count,
        "hit_positions": hits,
        "offset_counts": dict(sorted(offset_counts.items(), key=lambda item: int(item[0]))),
        "aligned_match_count": aligned_matches,
        "aligned_conflict_count": aligned_conflicts,
        "length_delta": len(blocks_b) - len(blocks_a),
        "similarity": float(similarity_fraction),
        "similarity_exact": f"{similarity_fraction.numerator}/{similarity_fraction.denominator}",
        "jaccard_similarity": float(jaccard_fraction),
        "jaccard_exact": f"{jaccard_fraction.numerator}/{jaccard_fraction.denominator}",
        "aligned_similarity": float(aligned_fraction),
        "aligned_similarity_exact": f"{aligned_fraction.numerator}/{aligned_fraction.denominator}",
        "document_digest_a": signature_a["doc_digest"],
        "document_digest_b": signature_b["doc_digest"],
        "digest_conflict_status": "match" if doc_digest_match else "conflict",
        "summary_conflict": not doc_digest_match,
        "threshold_formula": "multiset_block_hits / max(block_count_a, block_count_b)",
    }
    if config["validation_mode"] == "full":
        result["aligned_checks"] = aligned_checks
    else:
        result["digest_probe"] = {
            "ordered_digest_match": doc_digest_match,
            "aligned_conflict_count": aligned_conflicts,
            "offset_state_count": len(offset_counts),
            "note": "digest mode retains exact block-hit digests but omits the expanded aligned_checks ledger",
        }
    return result
