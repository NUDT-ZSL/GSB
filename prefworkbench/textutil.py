"""Shared text normalization and deterministic feature helpers."""

from __future__ import annotations

import re
import unicodedata


TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
NOISE_CHARS = frozenset("#@$%&*~")


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(normalize_text(text))


def ngrams(tokens: list[str], size: int) -> list[tuple[str, ...]]:
    if not tokens:
        return []
    if len(tokens) < size:
        return [tuple(tokens)]
    return [tuple(tokens[index : index + size]) for index in range(len(tokens) - size + 1)]


def repetition_rate(text: str, shingle_size: int) -> float:
    grams = ngrams(tokenize(text), max(1, shingle_size))
    if len(grams) <= 1:
        return 0.0
    return 1.0 - len(set(grams)) / len(grams)


def noise_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for char in text if char in NOISE_CHARS) / len(text)


def sample_text(sample: dict[str, object]) -> str:
    return "\n".join(
        [
            str(sample.get("prompt", "")),
            str(sample.get("chosen", "")),
            str(sample.get("rejected", "")),
        ]
    )

