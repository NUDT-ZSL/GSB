"""Deterministic query rewrite expansion and multi-hop bridge traversal."""

from __future__ import annotations

import heapq
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .data import Query
from .errors import format_corrupt
from .models import BridgeEntry, BridgeResource, Condition, RewriteRule, RuleSet
from .util import round_score, stable_json_bytes


@dataclass(frozen=True)
class ExpansionConfig:
    max_candidates: int = 64
    min_confidence: float = 0.0

    def validate(self) -> None:
        if self.max_candidates < 1:
            raise format_corrupt(
                "max_candidates must be at least one",
                max_candidates=self.max_candidates,
            )
        if not 0.0 <= self.min_confidence <= 1.0:
            raise format_corrupt(
                "min_confidence must be within [0, 1]",
                min_confidence=self.min_confidence,
            )


def expand_query(
    query: Query,
    ruleset: RuleSet,
    bridge: BridgeResource,
    *,
    config: ExpansionConfig | None = None,
) -> dict[str, Any]:
    """Expand one query and return a canonicalizable diagnostic document."""

    cfg = config or ExpansionConfig()
    cfg.validate()
    term_counts = dict(query.terms)
    raw_candidates: list[dict[str, Any]] = []

    for rule in sorted(ruleset.rules, key=lambda item: item.rule_id):
        if not rule.effective_enabled():
            continue
        if rule.source_lang != query.query_lang:
            continue
        if rule.source_term not in term_counts:
            continue
        condition = _effective_condition(rule)
        if condition is not None and not condition.matches(term_counts):
            continue

        for target in rule.targets:
            paths = bridge_paths(
                bridge,
                rule.source_lang,
                rule.source_term,
                target.lang,
                max_depth=rule.effective_depth(),
            )
            for path, bridge_confidence, target_term in paths:
                effective_confidence = round_score(
                    bridge_confidence * rule.effective_weight()
                )
                if effective_confidence < cfg.min_confidence:
                    continue
                raw_candidates.append(
                    {
                        "target_lang": target.lang,
                        "target_term": target_term,
                        "source_lang": rule.source_lang,
                        "source_term": rule.source_term,
                        "source_query_freq": term_counts[rule.source_term],
                        "bridge_confidence": round_score(bridge_confidence),
                        "rule_weight": round_score(rule.effective_weight()),
                        "confidence": effective_confidence,
                        "depth": len(path),
                        "path": path,
                        "rule_ids": [rule.rule_id],
                    }
                )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for candidate in raw_candidates:
        grouped.setdefault(
            (candidate["target_lang"], candidate["target_term"]),
            [],
        ).append(candidate)

    candidates: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda item: (item[0], item[1])):
        group = grouped[key]
        group.sort(
            key=lambda item: (
                -item["confidence"],
                item["depth"],
                stable_json_bytes(item["path"], newline=False),
                item["rule_ids"][0],
            )
        )
        best = group[0]
        merged_rule_ids = sorted(
            {
                rule_id
                for item in group
                if item["confidence"] == best["confidence"]
                for rule_id in item["rule_ids"]
            }
        )
        merged_paths = sorted(
            {
                stable_json_bytes(item["path"], newline=False)
                for item in group
                if item["confidence"] == best["confidence"]
            }
        )
        candidate = dict(best)
        candidate["rule_ids"] = merged_rule_ids
        candidate["alternate_paths"] = [
            json.loads(path.decode("utf-8")) for path in merged_paths
        ]
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            -item["confidence"],
            item["target_term"],
            item["target_lang"],
            item["source_term"],
            tuple(item["rule_ids"]),
        )
    )
    candidates = candidates[: cfg.max_candidates]

    document = {
        "query_id": query.query_id,
        "query_lang": query.query_lang,
        "rule_version_format": ruleset.format_version,
        "bridge_version": bridge.version,
        "bridge_checksum": bridge.checksum,
        "config": {
            "max_candidates": cfg.max_candidates,
            "min_confidence": cfg.min_confidence,
        },
        "candidates": candidates,
        "candidate_count": len(candidates),
    }
    return document


def _effective_condition(rule: RewriteRule) -> Condition | None:
    if rule.condition is not None:
        return rule.condition
    if rule.condition_terms:
        return Condition(
            op="all",
            children=tuple(
                Condition(op="term_present", term=term)
                for term in rule.condition_terms
            ),
        )
    return None


def bridge_paths(
    bridge: BridgeResource,
    source_lang: str,
    source_term: str,
    target_lang: str,
    *,
    max_depth: int,
) -> list[tuple[list[dict[str, Any]], float, str]]:
    """Enumerate the highest-confidence path to each reachable target term."""

    if max_depth < 1:
        raise format_corrupt("max_depth must be at least one", max_depth=max_depth)
    if source_lang == target_lang:
        return [([], 1.0, source_term)]

    # entry tuple -> bucket. This index is built from already canonical entries.
    buckets: dict[tuple[str, str], list[BridgeEntry]] = {}
    for entry in bridge.entries:
        buckets.setdefault((entry.source_lang, entry.source_term), []).append(entry)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: (item.target_lang, item.target_term, -item.confidence))

    best: dict[tuple[str, str], tuple[float, tuple[bytes, ...], tuple[dict[str, Any], ...]]] = {
        (source_lang, source_term): (1.0, (), ())
    }
    heap: list[tuple[float, bytes, int, str, str, tuple[dict[str, Any], ...], tuple[bytes, ...]]] = [
        (-1.0, b"", 0, source_lang, source_term, (), ())
    ]
    results: list[tuple[list[dict[str, Any]], float, str]] = []

    while heap:
        negative_confidence, _, depth, lang, term, path, path_identity = heapq.heappop(heap)
        confidence = -negative_confidence
        current = best.get((lang, term))
        if current is None:
            continue
        if (
            current[0] != confidence
            or current[1] != path_identity
        ):
            continue
        if lang == target_lang:
            results.append((list(path), confidence, term))
            continue
        if depth >= max_depth:
            continue

        for entry in buckets.get((lang, term), ()):  # type: ignore[arg-type]
            if any(item["target_lang"] == entry.target_lang and item["target_term"] == entry.target_term for item in path):
                continue
            next_depth = depth + 1
            next_confidence = confidence * entry.confidence
            next_path = path + (
                {
                    "source_lang": entry.source_lang,
                    "source_term": entry.source_term,
                    "target_lang": entry.target_lang,
                    "target_term": entry.target_term,
                    "confidence": round_score(entry.confidence),
                },
            )
            next_identity = path_identity + (
                stable_json_bytes(entry.to_dict(), newline=False),
            )
            existing = best.get((entry.target_lang, entry.target_term))
            if existing is not None:
                if next_confidence < existing[0]:
                    continue
                if next_confidence == existing[0] and next_identity >= existing[1]:
                    continue
            best[(entry.target_lang, entry.target_term)] = (
                next_confidence,
                next_identity,
                next_path,
            )
            heapq.heappush(
                heap,
                (
                    -next_confidence,
                    next_identity[-1],
                    next_depth,
                    entry.target_lang,
                    entry.target_term,
                    next_path,
                    next_identity,
                ),
            )

    results.sort(key=lambda item: (-item[1], item[2]))
    return results


def expansion_bytes(document: Mapping[str, Any]) -> bytes:
    return stable_json_bytes(document)
