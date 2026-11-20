from __future__ import annotations

import re

_SENTENCE_RE = re.compile(r"[^.!?]+(?:[.!?]+|$)")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")


def split_sentence_text(text: str) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    sentences = []
    for match in _SENTENCE_RE.finditer(normalized):
        sentence = match.group(0).strip()
        if sentence:
            sentences.append(sentence)
    return sentences


def tokenize(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(text.lower()))


def compress_tokens(tokens: tuple[str, ...], target_chars: int) -> tuple[str, int]:
    if not tokens:
        return "", 0
    limit = max(1, int(target_chars))
    selected: list[str] = []
    used = 0
    for token in tokens:
        extra = len(token) + (1 if selected else 0)
        if used + extra > limit and selected:
            break
        selected.append(token)
        used += extra
    return " ".join(selected), len(selected)

