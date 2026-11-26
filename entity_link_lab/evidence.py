"""Deterministic candidate generation and exact/approx evidence paths."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

import numpy as np

from .canonical import content_hash
from .ingest import bps, char_bigrams, jaccard, overlap_coefficient, tokenize


ALGORITHM_VERSION = "evidence-v3"
CACHE_KEY_VERSION = "ckv3"
LOW_DISCRIMINATION_FEATURES = (
    "mention_order_parity",
    "sentence_id_parity",
    "start_offset_mod_8",
    "end_offset_mod_16",
)


def pair_key(left: str, right: str) -> str:
    a, b = sorted((left, right))
    return f"{a}|{b}"


def candidate_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Score-first ordering with the required codepoint/type tie breaks."""

    first = item["first_mention"]
    return (
        -int(item["score"]["total_bps"]),
        int(item.get("algebra_generation", item["rule_generation"])),
        int(item["rule_generation"]),
        int(first["start"]),
        str(first["entity_type"]),
        str(item["pair_key"]),
    )


def deterministic_pair_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Stable candidate-set ordering independent of score acceptance."""

    first = item["first_mention"]
    second = item["second_mention"]
    return (
        str(first["doc_id"]),
        int(first["order_index"]),
        int(second["order_index"]),
        int(first["start"]),
        str(first["entity_type"]),
        str(item["pair_key"]),
    )


def select_rule(book: dict[str, Any], entity_type: str) -> dict[str, Any]:
    matches = [
        rule
        for rule in book["rules"]
        if rule["entity_type"] == entity_type and not rule["tombstone"]
    ]
    if not matches:
        raise KeyError(f"no active rule for entity_type={entity_type}")
    matches.sort(key=lambda rule: (-rule["generation"], rule["rule_id"]))
    return matches[0]


def tombstoned_rules(book: dict[str, Any], entity_type: str) -> list[dict[str, Any]]:
    return [
        {"rule_id": rule["rule_id"], "generation": rule["generation"]}
        for rule in book["rules"]
        if rule["entity_type"] == entity_type and rule["tombstone"]
    ]


def _context_tokens(mention: dict[str, Any]) -> set[str]:
    mention_tokens = {token.casefold() for token in mention.get("tokens", [])}
    tokens = set(mention.get("sentence_tokens", [])) - mention_tokens
    return {token for token in tokens if len(token) > 1}


def _auxiliary_features(a: dict[str, Any], b: dict[str, Any]) -> dict[str, int]:
    return {
        "mention_order_parity": (a["order_index"] + b["order_index"]) % 2,
        "sentence_id_parity": (a["sent_id"] + b["sent_id"]) % 2,
        "start_offset_mod_8": (a["start"] + b["start"]) % 8,
        "end_offset_mod_16": (a["end"] + b["end"]) % 16,
    }


def distance_features(a: dict[str, Any], b: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    sentence_distance = abs(int(a["sent_id"]) - int(b["sent_id"]))
    if a["global_end"] <= b["global_start"]:
        codepoint_gap = max(0, b["global_start"] - a["global_end"])
    elif b["global_end"] <= a["global_start"]:
        codepoint_gap = max(0, a["global_start"] - b["global_end"])
    else:
        codepoint_gap = abs(a["global_start"] - b["global_start"])

    window = max(1, int(rule["sentence_window"]))
    sentence_decay = float(np.exp(-sentence_distance / window))
    position_scale = max(1, int(rule["position_scale_cp"]))
    position_decay = float(np.exp(-codepoint_gap / (4.0 * position_scale)))
    distance_raw = 0.45 * sentence_decay + 0.55 * position_decay
    return {
        "sentence_distance": sentence_distance,
        "codepoint_gap": codepoint_gap,
        "sentence_decay_bps": bps(sentence_decay),
        "position_decay_bps": bps(position_decay),
        "distance_raw_bps": bps(distance_raw),
    }


def compute_evidence(
    a: dict[str, Any],
    b: dict[str, Any],
    book: dict[str, Any],
    doc_stats: dict[str, Any],
    *,
    path: str = "exact",
) -> dict[str, Any]:
    if a["order_index"] > b["order_index"]:
        a, b = b, a
    if a["doc_id"] != b["doc_id"]:
        raise ValueError("evidence requires mentions from one document")
    shared_type = a["entity_type"] == b["entity_type"]
    entity_type = a["entity_type"] if shared_type else f"{a['entity_type']}|{b['entity_type']}"
    rule = select_rule(book, a["entity_type"]) if shared_type else None
    if rule is None:
        raise ValueError(f"cannot score type-mismatched pair: {entity_type}")

    name_left = set(a.get("bigrams") or char_bigrams(a["name"]))
    name_right = set(b.get("bigrams") or char_bigrams(b["name"]))
    name_jaccard_raw = jaccard(name_left, name_right)
    name_overlap_raw = overlap_coefficient(name_left, name_right)
    token_left = set(a.get("tokens") or tokenize(a["name"]))
    token_right = set(b.get("tokens") or tokenize(b["name"]))
    token_jaccard_raw = jaccard(token_left, token_right)
    name_raw = 0.45 * name_jaccard_raw + 0.20 * name_overlap_raw + 0.35 * token_jaccard_raw

    context_left = _context_tokens(a)
    context_right = _context_tokens(b)
    context_raw = jaccard(context_left, context_right)
    type_raw = 1.0 if shared_type else 0.0
    shared_raw = 0.20 * type_raw + 0.50 * name_raw + 0.30 * context_raw

    distance = distance_features(a, b, rule)
    doc_count = int(doc_stats.get(a["doc_id"], {}).get("mention_count", 1))
    scale_target = int(book.get("document_scale_target_mentions", 12))
    doc_scale_raw = min(1.0, doc_count / max(1, scale_target))
    generation = int(rule["generation"])
    algebra_generation = int(rule["algebra_generation"])
    algebra_raw = min(
        1.0,
        (int(rule["algebra_bias_raw_bps"]) + (generation - 1) * 1_000) / 10_000,
    )

    raw_values = {
        "shared_proportion_bps": bps(shared_raw),
        "distance_decay_bps": distance["distance_raw_bps"],
        "document_scale_bps": bps(doc_scale_raw),
        "rule_algebra_bps": bps(algebra_raw),
    }
    weights = rule["weights_bps"]
    components = []
    total = 0
    for name in (
        "shared_proportion",
        "distance_decay",
        "document_scale",
        "rule_algebra",
    ):
        raw_key = f"{name}_bps"
        raw = raw_values[raw_key]
        weight = int(weights[name])
        contribution = (weight * raw) // 10_000
        total += contribution
        components.append(
            {
                "component": name,
                "weight_bps": weight,
                "raw_bps": raw,
                "contribution_bps": contribution,
                "formula": f"floor({weight} * {raw} / 10000)",
            }
        )

    evidence: dict[str, Any] = {
        "algorithm_version": ALGORITHM_VERSION,
        "pair_key": pair_key(a["mention_id"], b["mention_id"]),
        "doc_id": a["doc_id"],
        "mention_a": a["mention_id"],
        "mention_b": b["mention_id"],
        "first_mention": {
            "mention_id": a["mention_id"],
            "doc_id": a["doc_id"],
            "start": a["start"],
            "end": a["end"],
            "sent_id": a["sent_id"],
            "order_index": a["order_index"],
            "entity_type": a["entity_type"],
        },
        "second_mention": {
            "mention_id": b["mention_id"],
            "doc_id": b["doc_id"],
            "start": b["start"],
            "end": b["end"],
            "sent_id": b["sent_id"],
            "order_index": b["order_index"],
            "entity_type": b["entity_type"],
        },
        "shared_entity_type": a["entity_type"],
        "type_signature": f"type:{a['entity_type']}",
        "shared_type": shared_type,
        "position_distance": distance,
        "rule_id": rule["rule_id"],
        "rule_generation": generation,
        "algebra_generation": algebra_generation,
        "tombstoned_rule_ids": tombstoned_rules(book, a["entity_type"]),
        "features": {
            "type_match_bps": bps(type_raw),
            "name_similarity_bps": bps(name_raw),
            "context_overlap_bps": bps(context_raw),
            "shared_proportion_bps": raw_values["shared_proportion_bps"],
            "name_jaccard_bps": bps(name_jaccard_raw),
            "name_overlap_bps": bps(name_overlap_raw),
            "token_jaccard_bps": bps(token_jaccard_raw),
            "context_token_overlap_bps": bps(context_raw),
            "document_mention_count": doc_count,
            "document_scale_target": scale_target,
        },
        "score": {
            "components": components,
            "total_bps": total,
            "threshold_bps": int(rule["threshold_bps"]),
            "passes": total >= int(rule["threshold_bps"]),
            "recomputed_total_bps": sum(item["contribution_bps"] for item in components),
        },
        "path": path,
        "pruned_features": [],
    }

    if path == "exact":
        evidence["all_features"] = {
            **_auxiliary_features(a, b),
            "name_bigram_intersection": sorted(name_left & name_right),
            "context_token_intersection": sorted(context_left & context_right),
        }
    elif path == "approx":
        evidence["pruned_features"] = [
            {
                "name": name,
                "reason": "low_discrimination_zero_score_weight",
                "score_weight_bps": 0,
            }
            for name in LOW_DISCRIMINATION_FEATURES
        ] + [
            {
                "name": "name_bigram_intersection",
                "reason": "aggregate_similarity_retained_detail_set_skipped",
                "score_weight_bps": 0,
            },
            {
                "name": "context_token_intersection",
                "reason": "aggregate_overlap_retained_detail_set_skipped",
                "score_weight_bps": 0,
            },
        ]
    else:
        raise ValueError(f"unknown evidence path: {path}")

    evidence["evidence_hash"] = content_hash(_hashable_evidence(evidence))
    evidence["cache_key"] = make_cache_key(evidence["evidence_hash"], book)
    return evidence


def _hashable_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in evidence.items()
        if key not in {"evidence_hash", "cache_key"}
    }


def make_cache_key(evidence_hash: str, book: dict[str, Any]) -> str:
    dependency = content_hash(
        {
            "cache_key_version": CACHE_KEY_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "rule_internal_hash": content_hash(book),
        }
    )
    return f"{CACHE_KEY_VERSION}:{dependency[:16]}:{evidence_hash[:24]}"


def document_stats(mentions: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mention in mentions:
        by_doc[mention["doc_id"]].append(mention)
    stats: dict[str, dict[str, Any]] = {}
    for doc_id, items in by_doc.items():
        counts = np.array([len(items)], dtype=np.int64)
        stats[doc_id] = {
            "mention_count": int(counts.sum()),
            "sentence_count": len({item["sent_id"] for item in items}),
            "mean_mentions_per_sentence": float(np.mean(counts)),
        }
    return stats


def _within_window(a: dict[str, Any], b: dict[str, Any], book: dict[str, Any]) -> bool:
    if a["entity_type"] != b["entity_type"]:
        return False
    rule = select_rule(book, a["entity_type"])
    return abs(a["sent_id"] - b["sent_id"]) <= int(rule["sentence_window"])


def generate_exact_candidates(
    mentions: list[dict[str, Any]],
    book: dict[str, Any],
    seed_pairs: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generate rule-window pairs and union them with the seed candidate list."""

    by_id = {mention["mention_id"]: mention for mention in mentions}
    docs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mention in mentions:
        docs[mention["doc_id"]].append(mention)

    pair_origins: dict[str, set[str]] = defaultdict(set)
    for doc_mentions in docs.values():
        ordered = sorted(doc_mentions, key=lambda item: item["order_index"])
        for index, a in enumerate(ordered):
            for b in ordered[index + 1 :]:
                if _within_window(a, b, book):
                    key = pair_key(a["mention_id"], b["mention_id"])
                    pair_origins[key].add("rule_scan")

    seed_pairs = seed_pairs or []
    for seed in seed_pairs:
        key = seed["pair_key"]
        a = by_id[seed["mention_a"]]
        b = by_id[seed["mention_b"]]
        if not _within_window(a, b, book):
            raise ValueError(f"seed candidate outside active rule window: {key}")
        pair_origins[key].add(seed.get("origin", "seed_list"))

    stats = document_stats(mentions)
    evidence_records: list[dict[str, Any]] = []
    for key in sorted(pair_origins):
        left_id, right_id = key.split("|", 1)
        a, b = by_id[left_id], by_id[right_id]
        record = compute_evidence(a, b, book, stats, path="exact")
        record["candidate_origins"] = sorted(pair_origins[key])
        record["candidate_sources"] = sorted(pair_origins[key])
        record["tie_break"] = {
            "first_mention_start": record["first_mention"]["start"],
            "entity_type": record["shared_entity_type"],
            "pair_key": key,
        }
        evidence_records.append(record)

    evidence_records.sort(key=deterministic_pair_key)
    generation_info = {
        "candidate_count": len(evidence_records),
        "seed_pair_count": len(seed_pairs),
        "rule_generated_count": sum(
            "rule_scan" in item["candidate_origins"] for item in evidence_records
        ),
        "byte_order": "doc_id,order_index,first_start,entity_type,pair_key",
    }
    return evidence_records, generation_info


def generate_approx_candidates(
    mentions: list[dict[str, Any]],
    book: dict[str, Any],
    seed_pairs: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bucket by shared ``(doc_id, type signature)`` before pair generation."""

    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for mention in sorted(mentions, key=lambda item: item["order_index"]):
        buckets[(mention["doc_id"], f"type:{mention['entity_type']}")].append(mention)

    seed_by_key = {item["pair_key"]: item for item in seed_pairs or []}
    stats = document_stats(mentions)
    records: list[dict[str, Any]] = []
    bucket_reports: list[dict[str, Any]] = []
    retained_keys: set[str] = set()

    for (doc_id, signature), bucket in sorted(buckets.items()):
        entity_type = signature.split(":", 1)[1]
        rule = select_rule(book, entity_type)
        window = int(rule["sentence_window"])
        retained: list[str] = []
        filtered_by_window = 0
        for index, a in enumerate(bucket):
            for b in bucket[index + 1 :]:
                key = pair_key(a["mention_id"], b["mention_id"])
                if abs(a["sent_id"] - b["sent_id"]) > window:
                    filtered_by_window += 1
                    continue
                record = compute_evidence(a, b, book, stats, path="approx")
                origins = {"type_bucket_scan"}
                if key in seed_by_key:
                    origins.add(seed_by_key[key].get("origin", "seed_list"))
                record["candidate_origins"] = sorted(origins)
                record["candidate_sources"] = sorted(origins)
                record["bucket"] = {
                    "bucket_key": f"{doc_id}|{signature}",
                    "type_signature": signature,
                    "bucket_size": len(bucket),
                    "filter_basis": f"same_doc+{signature}+sentence_distance<={window}",
                }
                record["tie_break"] = {
                    "first_mention_start": record["first_mention"]["start"],
                    "entity_type": record["shared_entity_type"],
                    "pair_key": key,
                }
                records.append(record)
                retained.append(key)
                retained_keys.add(key)
        bucket_reports.append(
            {
                "bucket_key": f"{doc_id}|{signature}",
                "doc_id": doc_id,
                "type_signature": signature,
                "mention_ids": [item["mention_id"] for item in bucket],
                "bucket_size": len(bucket),
                "sentence_window": window,
                "retained_pair_count": len(retained),
                "filtered_by_window_count": filtered_by_window,
                "retained_pair_keys": sorted(retained),
                "filter_basis": f"same_doc+shared_type_signature+sentence_distance<={window}",
            }
        )

    missing_seed = sorted(set(seed_by_key) - retained_keys)
    if missing_seed:
        raise ValueError(f"approx buckets dropped seed candidates: {missing_seed}")

    records.sort(key=deterministic_pair_key)
    plan = {
        "path": "approx",
        "bucket_count": len(bucket_reports),
        "buckets": bucket_reports,
        "candidate_count": len(records),
        "pruned_feature_dictionary": [
            {"name": name, "score_weight_bps": 0, "reason": "low_discrimination_zero_score_weight"}
            for name in LOW_DISCRIMINATION_FEATURES
        ],
        "bucket_filter_rule": "same doc_id, exact shared type signature, active rule sentence window",
    }
    return records, plan
