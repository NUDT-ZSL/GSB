"""Deterministic bridge expansion and candidate ordering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .config import ExpandConfig
from .errors import ConfidenceOutOfRange, DirectionMissing
from .model import BridgeResource, Query


@dataclass(frozen=True)
class Candidate:
    source_term: str
    target_lang: str
    target_term: str
    confidence: float

    def to_dict(self) -> dict:
        return {
            "source_term": self.source_term,
            "target_lang": self.target_lang,
            "target_term": self.target_term,
            "confidence": self.confidence,
        }


@dataclass
class ExpansionResult:
    query_id: str
    candidates: List[Candidate] = field(default_factory=list)
    # Per target language, preserving the global deterministic order.
    by_language: Dict[str, List[Candidate]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "by_language": {
                lang: [candidate.to_dict() for candidate in items]
                for lang, items in sorted(self.by_language.items())
            },
        }


def _validate_confidence(query_id: str, term: str, confidence: float) -> None:
    if confidence < 0.0 or confidence > 1.0 or confidence != confidence:
        raise ConfidenceOutOfRange(query_id, term, confidence)


def expand_query(
    query: Query,
    bridge: BridgeResource,
    config: ExpandConfig,
    target_languages=None,
) -> ExpansionResult:
    """Expand a query into ordered target language candidates.

    Ordering is fully deterministic: confidence descending, then target term
    lexicographic ascending, then target language ascending.  Ties therefore
    always resolve by target term dictionary order.  Duplicate
    ``(target_lang, target_term)`` pairs keep the highest confidence.
    """

    config.validate()
    result = ExpansionResult(query_id=query.query_id)

    if target_languages is None:
        target_languages = _target_langs(bridge)

    # Preserve the query's declared term order for deterministic traversal.
    seen = set()
    collected: List[Candidate] = []
    for source_term in query.terms:
        for target_lang in target_languages:
            if target_lang == query.query_lang:
                continue
            if not bridge.has_direction(query.query_lang, target_lang):
                raise DirectionMissing(
                    query.query_id, source_term, query.query_lang, target_lang
                )
            mappings = bridge.lookup(query.query_lang, source_term)
            for mapped_target_lang, target_term, confidence in mappings:
                if mapped_target_lang != target_lang:
                    continue
                _validate_confidence(query.query_id, source_term, confidence)
                if confidence < config.min_confidence:
                    continue
                key = (target_lang, target_term)
                if key in seen:
                    # Keep the highest confidence for the same target term.
                    for index, existing in enumerate(collected):
                        if (existing.target_lang, existing.target_term) == key:
                            if confidence > existing.confidence:
                                collected[index] = Candidate(
                                    source_term, target_lang, target_term, confidence
                                )
                            break
                    continue
                seen.add(key)
                collected.append(Candidate(source_term, target_lang, target_term, confidence))

    # Deterministic total order.
    collected.sort(
        key=lambda candidate: (
            -candidate.confidence,
            candidate.target_term,
            candidate.target_lang,
            candidate.source_term,
        )
    )
    candidates = collected[: config.max_candidates]
    result.candidates = candidates
    grouped: Dict[str, List[Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.target_lang, []).append(candidate)
    result.by_language = grouped
    return result


def _target_langs(bridge: BridgeResource):
    return sorted({target for _, target in bridge.direction_pairs()})
