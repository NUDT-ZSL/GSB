"""Pairwise coreference evidence with independently recomputable components."""

from __future__ import annotations

import math
from typing import Any, Iterable

from .cache import CacheStore, cache_key
from .canonical import content_hash, q6, short_hash
from .config import RulesConfig, TimeEquivalence, normalize_text, trigger_similarity


EVIDENCE_SCHEMA = "evidence/v1"
COMPONENT_NAMES = ("entity", "time", "trigger", "source_trust")


def _matched_entities(
    left: dict[str, Any],
    right: dict[str, Any],
    config: RulesConfig,
) -> list[dict[str, Any]]:
    role_weights = config.role_weights
    used_right: set[int] = set()
    matches: list[dict[str, Any]] = []
    for left_idx, left_entity in enumerate(left["entities"]):
        left_key = (
            left_entity["role"],
            normalize_text(left_entity["name"]),
        )
        for right_idx, right_entity in enumerate(right["entities"]):
            if right_idx in used_right:
                continue
            right_key = (
                right_entity["role"],
                normalize_text(right_entity["name"]),
            )
            if left_key == right_key:
                used_right.add(right_idx)
                matches.append(
                    {
                        "name": left_entity["name"],
                        "normalized_name": left_key[1],
                        "role": left_entity["role"],
                        "role_weight": q6(role_weights.get(left_entity["role"], 0.0)),
                        "a_index": left_idx,
                        "b_index": right_idx,
                    }
                )
                break
    matches.sort(key=lambda item: (item["role"], item["normalized_name"], item["a_index"]))
    return matches


def _best_trigger(
    left: dict[str, Any],
    right: dict[str, Any],
    config: RulesConfig,
) -> tuple[float, dict[str, Any], dict[str, Any]]:
    best_score = -1.0
    best_left: dict[str, Any] | None = None
    best_right: dict[str, Any] | None = None
    best_key: tuple[Any, ...] | None = None
    for left_mention in left["mentions"]:
        for right_mention in right["mentions"]:
            score = trigger_similarity(
                left_mention["trigger"], right_mention["trigger"], config
            )
            key = (
                -score,
                left_mention["start"],
                left_mention["end"],
                left_mention["trigger"],
                right_mention["start"],
                right_mention["end"],
                right_mention["trigger"],
            )
            if best_key is None or key < best_key:
                best_score = score
                best_left = left_mention
                best_right = right_mention
                best_key = key
    assert best_left is not None and best_right is not None
    return q6(best_score), dict(best_left), dict(best_right)


def compute_evidence_record(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_hash: str,
    right_hash: str,
    config: RulesConfig,
    effective_floor: float,
    time_index: TimeEquivalence | None = None,
) -> dict[str, Any]:
    """Build one score record. Endpoints are ordered by event_id."""

    if left["event_id"] > right["event_id"]:
        left, right = right, left
        left_hash, right_hash = right_hash, left_hash
    time_index = time_index or TimeEquivalence(config)
    weights = config.weights

    matches = _matched_entities(left, right, config)
    matched_weight = sum(item["role_weight"] for item in matches)
    entity_denominator = max(len(left["entities"]), len(right["entities"]))
    entity_raw = q6(
        min(1.0, matched_weight / entity_denominator) if entity_denominator else 0.0
    )

    equivalent, time_rule = time_index.equivalent(
        left["time_expr"], right["time_expr"]
    )
    time_raw = 1.0 if equivalent else 0.0

    trigger_raw, left_mention, right_mention = _best_trigger(left, right, config)

    trust_a = float(left["source_trust"])
    trust_b = float(right["source_trust"])
    trust_raw = q6(math.sqrt(max(0.0, trust_a * trust_b)))

    components = {
        "entity": {
            "weight": q6(float(weights["entity"])),
            "raw": entity_raw,
            "contribution": q6(float(weights["entity"]) * entity_raw),
        },
        "time": {
            "weight": q6(float(weights["time"])),
            "raw": time_raw,
            "contribution": q6(float(weights["time"]) * time_raw),
            "equivalent": equivalent,
            "rule": time_rule,
            "a_expr": left["time_expr"],
            "b_expr": right["time_expr"],
        },
        "trigger": {
            "weight": q6(float(weights["trigger"])),
            "raw": trigger_raw,
            "contribution": q6(float(weights["trigger"]) * trigger_raw),
            "a_mention": left_mention,
            "b_mention": right_mention,
            "method": config.payload["trigger_similarity"]["method"],
        },
        "source_trust": {
            "weight": q6(float(weights["source_trust"])),
            "raw": trust_raw,
            "contribution": q6(float(weights["source_trust"]) * trust_raw),
            "a": q6(trust_a),
            "b": q6(trust_b),
        },
    }
    score = q6(sum(component["contribution"] for component in components.values()))
    base = {
        "schema_version": EVIDENCE_SCHEMA,
        "event_a": left["event_id"],
        "event_b": right["event_id"],
        "event_hashes": [left_hash, right_hash],
        "event_keys": [
            [left["batch_no"], left["event_id"]],
            [right["batch_no"], right["event_id"]],
        ],
        "score": score,
        "score_floor": q6(effective_floor),
        "components": components,
        "matched_entities": matches,
        "config": {
            "config_hash": config.config_hash,
            "weights_version": config.weights_version,
            "time_rules_version": config.time_rules_version,
            "trigger_version": config.trigger_version,
            "candidate_floor": q6(config.candidate_floor),
            "effective_floor": q6(effective_floor),
        },
    }
    digest = content_hash(base)
    return {
        "evidence_id": "evd_" + short_hash({"base": digest}, 24),
        **base,
        "evidence_hash": digest,
    }


def recompute_score(evidence: dict[str, Any]) -> dict[str, Any]:
    """Independent component-only score recomputation used by verification."""

    contributions = {
        name: q6(float(evidence["components"][name]["contribution"]))
        for name in COMPONENT_NAMES
    }
    expected_raw = {
        name: q6(evidence["components"][name]["weight"] * evidence["components"][name]["raw"])
        for name in COMPONENT_NAMES
    }
    total = q6(sum(contributions.values()))
    return {
        "ok": all(
            contributions[name] == expected_raw[name] for name in COMPONENT_NAMES
        )
        and total == q6(float(evidence["score"])),
        "component_contributions": contributions,
        "component_products": expected_raw,
        "recomputed_total": total,
        "recorded_total": q6(float(evidence["score"])),
    }


def candidate_pairs_for_batch(
    new_events: list[dict[str, Any]],
    previous_events: list[dict[str, Any]],
    *,
    compare_within_new_batch: bool = True,
) -> list[tuple[str, str]]:
    """Return deterministic unordered pairs involving at least one new event."""

    pairs: set[tuple[str, str]] = set()
    new_ids = {event["event_id"] for event in new_events}
    for new_event in new_events:
        for old_event in previous_events:
            if old_event["event_id"] in new_ids:
                continue
            a, b = sorted((new_event["event_id"], old_event["event_id"]))
            pairs.add((a, b))
    if compare_within_new_batch:
        ordered = sorted(new_events, key=lambda event: event["event_id"])
        for idx, first in enumerate(ordered):
            for second in ordered[idx + 1 :]:
                a, b = sorted((first["event_id"], second["event_id"]))
                pairs.add((a, b))
    return sorted(pairs)


def generate_evidence(
    events_by_id: dict[str, dict[str, Any]],
    event_hashes: dict[str, str],
    pairs: Iterable[tuple[str, str]],
    *,
    config: RulesConfig,
    effective_floor: float,
    cache: CacheStore | None = None,
    time_index: TimeEquivalence | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    time_index = time_index or TimeEquivalence(config)
    records: list[dict[str, Any]] = []
    stats = {
        "compared_pairs": 0,
        "candidates": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "cache_hit_pairs": [],
        "cache_miss_pairs": [],
    }
    for event_a, event_b in sorted(set(pairs)):
        if event_a == event_b:
            continue
        if event_a > event_b:
            event_a, event_b = event_b, event_a
        stats["compared_pairs"] += 1
        left = events_by_id[event_a]
        right = events_by_id[event_b]
        key = cache_key(
            "evidence/v1",
            {
                "event_hashes": [event_hashes[event_a], event_hashes[event_b]],
                "config_hash": config.config_hash,
                "weights_version": config.weights_version,
                "time_rules_version": config.time_rules_version,
                "trigger_version": config.trigger_version,
                "candidate_floor": q6(config.candidate_floor),
                "effective_floor": q6(effective_floor),
                "upstream": [event_hashes[event_a], event_hashes[event_b]],
            },
        )
        cached = cache.get("evidence", key) if cache is not None else None
        record: dict[str, Any] | None
        if isinstance(cached, dict) and cached.get("evidence_id"):
            record = cached
            stats["cache_hits"] += 1
            stats["cache_hit_pairs"].append([event_a, event_b])
        else:
            record = compute_evidence_record(
                left,
                right,
                left_hash=event_hashes[event_a],
                right_hash=event_hashes[event_b],
                config=config,
                effective_floor=effective_floor,
                time_index=time_index,
            )
            stats["cache_misses"] += 1
            stats["cache_miss_pairs"].append([event_a, event_b])
            if cache is not None:
                cache.put("evidence", key, record)
        if q6(float(record["score"])) >= q6(effective_floor):
            records.append(record)
            stats["candidates"] += 1
    records.sort(key=lambda item: (item["event_a"], item["event_b"]))
    return records, stats


def evidence_input_digest(
    events: list[dict[str, Any]],
    *,
    config: RulesConfig,
    effective_floor: float,
) -> str:
    return content_hash(
        {
            "schema_version": "evidence-input/v1",
            "events": [
                {
                    "event_id": event["event_id"],
                    "batch_no": event["batch_no"],
                    "content_hash": content_hash(event),
                }
                for event in events
            ],
            "rules": {
                "config_hash": config.config_hash,
                "weights_version": config.weights_version,
                "time_rules_version": config.time_rules_version,
                "trigger_version": config.trigger_version,
                "candidate_floor": q6(config.candidate_floor),
                "effective_floor": q6(effective_floor),
            },
        }
    )

