"""Exact and restricted-approximate evidence extraction."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

from .dictionary import Dictionary, DictionaryEntry
from .taxonomy import Taxonomy


@dataclass
class EvidenceResult:
    evidence: list[dict[str, Any]]
    candidate_intents: list[str]
    lexical_scores: dict[tuple[str, str], float]
    stats: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence": self.evidence,
            "candidate_intents": [
                {"domain": domain, "intent_class": intent}
                for domain, intent in sorted(self.lexical_scores)
            ],
            "lexical_scores": [
                {
                    "domain": domain,
                    "intent_class": intent,
                    "score": round(score, 6),
                }
                for (domain, intent), score in sorted(
                    self.lexical_scores.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "stats": self.stats,
        }


class EvidenceExtractor:
    """Builds dictionary evidence for exact and pruned-approximate paths."""

    def __init__(self, dictionary: Dictionary, taxonomy: Taxonomy):
        self.dictionary = dictionary
        self.taxonomy = taxonomy
        self._bigram_index: dict[str, set[str]] | None = None
        self._char_index: dict[str, set[str]] | None = None

    def _build_indexes(self) -> None:
        if self._bigram_index is not None:
            return
        bigram_index: dict[str, set[str]] = {}
        char_index: dict[str, set[str]] = {}
        for surface in self.dictionary.by_surface:
            for char in set(surface):
                char_index.setdefault(char, set()).add(surface)
            if len(surface) == 1:
                bigram_index.setdefault(surface, set()).add(surface)
            else:
                for index in range(len(surface) - 1):
                    bigram = surface[index : index + 2]
                    bigram_index.setdefault(bigram, set()).add(surface)
        self._bigram_index = bigram_index
        self._char_index = char_index

    def _entry_evidence(
        self,
        entry: DictionaryEntry,
        token: dict[str, Any],
        method: str,
        similarity: float,
    ) -> dict[str, Any]:
        return {
            "entry_id": entry.entry_id,
            "surface": entry.surface,
            "token_id": token["token_id"],
            "token_text": token["text"],
            "start": token["start"],
            "end": token["end"],
            "kind": entry.kind,
            "domain": entry.domain,
            "intent_class": entry.intent_class,
            "slot": entry.slot,
            "canonical_value": entry.canonical_value,
            "weight": round(entry.weight, 6),
            "method": method,
            "similarity": round(similarity, 6),
        }

    def _score_evidence(
        self,
        evidence: list[dict[str, Any]],
        *,
        max_candidate_intents: int | None,
    ) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], set[str]]]:
        scores: dict[tuple[str, str], float] = {}
        slots: dict[tuple[str, str], set[str]] = {}
        for row in evidence:
            key = (row["domain"], row["intent_class"])
            if self.taxonomy.intent(*key) is None:
                continue
            if row["kind"] == "intent_cue":
                contribution = 0.60 * row["weight"] * row["similarity"]
            elif row["kind"] == "slot_value" and row["slot"] is not None:
                contribution = 0.45 * row["weight"] * row["similarity"]
                slots.setdefault(key, set()).add(str(row["slot"]))
            else:
                contribution = 0.40 * row["weight"] * row["similarity"]
            scores[key] = scores.get(key, 0.0) + contribution
        for key in list(scores):
            intent = self.taxonomy.require_intent(*key)
            if intent.slot_schema:
                coverage = len(slots.get(key, set())) / len(intent.slot_schema)
                scores[key] += 0.20 * coverage
        if max_candidate_intents is not None and len(scores) > max_candidate_intents:
            ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            scores = dict(ranked[:max_candidate_intents])
        return scores, slots

    def exact(self, tokens: Iterable[dict[str, Any]]) -> EvidenceResult:
        token_list = list(tokens)
        evidence: list[dict[str, Any]] = []
        lookup_count = 0
        for token in token_list:
            for entry_id in token["entry_ids"]:
                entry = self.dictionary.by_id[entry_id]
                evidence.append(self._entry_evidence(entry, token, "exact", 1.0))
                lookup_count += 1
        lexical_scores, _ = self._score_evidence(
            evidence, max_candidate_intents=None
        )
        return EvidenceResult(
            evidence=evidence,
            candidate_intents=sorted(f"{domain}:{intent}" for domain, intent in lexical_scores),
            lexical_scores=lexical_scores,
            stats={
                "token_count": len(token_list),
                "exact_entry_hits": lookup_count,
                "fuzzy_comparisons": 0,
                "pruned_entry_hits": lookup_count,
            },
        )

    def _fuzzy_surfaces(self, token_text: str) -> set[str]:
        self._build_indexes()
        assert self._bigram_index is not None and self._char_index is not None
        candidates: set[str] = set()
        if len(token_text) == 1:
            candidates.update(self._char_index.get(token_text, set()))
        else:
            for index in range(len(token_text) - 1):
                candidates.update(
                    self._bigram_index.get(token_text[index : index + 2], set())
                )
        return candidates

    def approximate(
        self,
        tokens: Iterable[dict[str, Any]],
        *,
        max_candidates_per_token: int = 16,
        max_candidate_intents: int = 8,
        fuzzy_threshold: float = 0.62,
    ) -> EvidenceResult:
        evidence: list[dict[str, Any]] = []
        fuzzy_comparisons = 0
        pruned_hits = 0
        token_list = list(tokens)
        for token in token_list:
            exact_rows: list[tuple[DictionaryEntry, str, float]] = []
            for entry_id in token["entry_ids"]:
                exact_rows.append((self.dictionary.by_id[entry_id], "exact", 1.0))

            fuzzy_eligible = (
                len(token["text"]) >= 2
                and all(not char.isascii() for char in token["text"])
            )
            if exact_rows:
                candidates = exact_rows
            elif fuzzy_eligible:
                candidates = []
                for surface in sorted(self._fuzzy_surfaces(token["text"])):
                    similarity = SequenceMatcher(None, token["text"], surface).ratio()
                    fuzzy_comparisons += 1
                    if similarity < fuzzy_threshold:
                        continue
                    for entry_id in self.dictionary.by_surface[surface]:
                        candidates.append(
                            (self.dictionary.by_id[entry_id], "fuzzy", similarity)
                        )
                candidates.sort(
                    key=lambda item: (-item[2], item[0].entry_id)
                )
            else:
                candidates = []

            selected = candidates[:max_candidates_per_token]
            pruned_hits += len(selected)
            for entry, method, similarity in selected:
                evidence.append(
                    self._entry_evidence(entry, token, method, similarity)
                )

        lexical_scores, _ = self._score_evidence(
            evidence, max_candidate_intents=max_candidate_intents
        )
        return EvidenceResult(
            evidence=evidence,
            candidate_intents=sorted(f"{domain}:{intent}" for domain, intent in lexical_scores),
            lexical_scores=lexical_scores,
            stats={
                "token_count": len(token_list),
                "exact_entry_hits": sum(len(token["entry_ids"]) for token in token_list),
                "fuzzy_comparisons": fuzzy_comparisons,
                "pruned_entry_hits": pruned_hits,
            },
        )

