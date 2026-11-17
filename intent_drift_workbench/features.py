"""Deterministic text folding and intent feature extraction."""

from __future__ import annotations

import hashlib
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .canonical import sha256_json, sha256_text
from .config import ConfigError


@dataclass(frozen=True)
class VocabState:
    version: str
    categories: tuple[str, ...]
    terms: tuple[str, ...]
    category_by_term: dict[str, str]
    hash_value: str

    def payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "categories": list(self.categories),
            "terms": list(self.terms),
        }


def stable_category(term: str, categories: Iterable[str]) -> str:
    ordered = tuple(sorted(categories))
    if not ordered:
        raise ValueError("categories must not be empty")
    digest = hashlib.sha256(term.encode("utf-8")).hexdigest()
    index = int(digest[:16], 16) % len(ordered)
    return ordered[index]


def fold_text(text: str, fold_rules: dict[str, Any]) -> tuple[str, list[str]]:
    folded = text
    if fold_rules.get("nfkc", True):
        folded = unicodedata.normalize("NFKC", folded)
    if fold_rules.get("lowercase", True):
        folded = folded.lower()
    if fold_rules.get("strip_punctuation", True):
        characters: list[str] = []
        for character in folded:
            if unicodedata.category(character)[0] in {"P", "S", "Z"} and not character.isspace():
                characters.append(" ")
            else:
                characters.append(character)
        folded = "".join(characters)
    if fold_rules.get("collapse_whitespace", True):
        folded = " ".join(folded.split())
    else:
        folded = folded.strip()
    tokens = folded.split() if folded else []
    return folded, tokens


def normalize_vocabulary_terms(
    terms: Iterable[str], fold_rules: dict[str, Any], categories: Iterable[str]
) -> tuple[str, ...]:
    normalized: dict[str, str] = {}
    for original in terms:
        folded, tokens = fold_text(original, fold_rules)
        if len(tokens) != 1:
            raise ConfigError(
                "E_CONFIG_INVALID",
                f"vocabulary term must fold to exactly one token: {original!r} -> {folded!r}",
            )
        term = tokens[0]
        if term in normalized and normalized[term] != original:
            raise ConfigError(
                "E_CONFIG_INVALID",
                f"vocabulary terms collide after folding: {original!r} and {normalized[term]!r}",
            )
        normalized[term] = original
    return tuple(sorted(normalized))


def build_vocab(
    categories: Iterable[str], terms: Iterable[str], version: str
) -> VocabState:
    ordered_categories = tuple(sorted(categories))
    ordered_terms = tuple(sorted(set(terms)))
    category_by_term = {
        term: stable_category(term, ordered_categories) for term in ordered_terms
    }
    payload = {
        "version": version,
        "categories": list(ordered_categories),
        "terms": list(ordered_terms),
    }
    return VocabState(
        version=version,
        categories=ordered_categories,
        terms=ordered_terms,
        category_by_term=category_by_term,
        hash_value=sha256_json(payload),
    )


def extract_features(
    event: dict[str, Any],
    vocab: VocabState,
    fold_rules: dict[str, Any],
) -> dict[str, Any]:
    folded, tokens = fold_text(event["text"], fold_rules)
    token_counts = Counter(tokens)
    term_counts = {
        term: int(token_counts[term]) for term in sorted(token_counts) if term in vocab.category_by_term
    }
    category_counts = [0 for _ in vocab.categories]
    category_index = {category: index for index, category in enumerate(vocab.categories)}
    for term, count in term_counts.items():
        category_counts[category_index[vocab.category_by_term[term]]] += count
    hit_terms = sorted(term_counts)
    raw_terms = sorted(token_counts)
    content_fingerprint = sha256_text(folded)
    cache_key = sha256_json(
        {
            "content_fingerprint": content_fingerprint,
            "vocab_version": vocab.version,
            "fold_rules_version": fold_rules["version"],
            "categories": list(vocab.categories),
        }
    )
    return {
        "session_id": event["session_id"],
        "turn_no": int(event["turn_no"]),
        "speaker": event["speaker"],
        "text": event["text"],
        "timestamp_iso": event["timestamp_iso"],
        "injection_id": event.get("injection_id"),
        "normalized_text": folded,
        "tokens": raw_terms,
        "term_counts": term_counts,
        "category_vector": category_counts,
        "hit_terms": hit_terms,
        "raw_terms": raw_terms,
        "empty_terms": not bool(hit_terms),
        "content_fingerprint": content_fingerprint,
        "cache_key": cache_key,
        "stable_key": turn_sort_key(event["session_id"], int(event["turn_no"])),
    }


def refresh_feature_cache_key(
    feature: dict[str, Any], vocab: VocabState, fold_rules: dict[str, Any]
) -> dict[str, Any]:
    updated = dict(feature)
    updated["cache_key"] = sha256_json(
        {
            "content_fingerprint": feature["content_fingerprint"],
            "vocab_version": vocab.version,
            "fold_rules_version": fold_rules["version"],
            "categories": list(vocab.categories),
        }
    )
    return updated


def category_distribution(category_counts: Iterable[int], precision: int, quantizer: Any) -> tuple[list[float], bool]:
    counts = np.asarray(list(category_counts), dtype=np.int64)
    total = int(counts.sum())
    if total == 0:
        uniform = [quantizer(1.0 / len(counts), precision) for _ in range(len(counts))]
        return uniform, True
    raw = counts.astype(np.float64) / float(total)
    return [quantizer(float(value), precision) for value in raw], False


def turn_sort_key(session_id: str, turn_no: int) -> str:
    return f"{session_id}:{turn_no:020d}"


def window_sort_key(session_id: str, start_turn: int) -> str:
    return f"{session_id}:{start_turn:020d}"


def lookup_category_counts(
    term_counts: dict[str, int], vocab: VocabState
) -> list[int]:
    output = [0 for _ in vocab.categories]
    category_index = {category: index for index, category in enumerate(vocab.categories)}
    for term, count in term_counts.items():
        category = vocab.category_by_term.get(term)
        if category is not None:
            output[category_index[category]] += int(count)
    return output
