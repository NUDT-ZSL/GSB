"""Codepoint-offset tokenization with dictionary longest matching."""

from __future__ import annotations

from typing import Any

from .dictionary import Dictionary


def _is_ascii_word(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in {"_", "-", ".", "%", "+"})


def tokenize_text(normalized_text: str, dictionary: Dictionary) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    cursor = 0
    length = len(normalized_text)
    while cursor < length:
        char = normalized_text[cursor]
        if char.isspace():
            cursor += 1
            continue
        match = dictionary.longest_match(normalized_text, cursor)
        if match is not None:
            end, entry_ids = match
            tokens.append(
                {
                    "token_id": f"t{len(tokens):04d}",
                    "start": cursor,
                    "end": end,
                    "text": normalized_text[cursor:end],
                    "kind": "DICT",
                    "entry_ids": list(entry_ids),
                }
            )
            cursor = end
            continue
        if _is_ascii_word(char):
            end = cursor + 1
            while end < length and _is_ascii_word(normalized_text[end]):
                end += 1
            tokens.append(
                {
                    "token_id": f"t{len(tokens):04d}",
                    "start": cursor,
                    "end": end,
                    "text": normalized_text[cursor:end],
                    "kind": "ASCII",
                    "entry_ids": [],
                }
            )
            cursor = end
            continue
        tokens.append(
            {
                "token_id": f"t{len(tokens):04d}",
                "start": cursor,
                "end": cursor + 1,
                "text": char,
                "kind": "CJK" if not char.isascii() else "PUNCT",
                "entry_ids": [],
            }
        )
        cursor += 1
    return tokens


def char_fallback_tokens(normalized_text: str, reason: str) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    for index, char in enumerate(normalized_text):
        if char.isspace():
            continue
        tokens.append(
            {
                "token_id": f"t{len(tokens):04d}",
                "start": index,
                "end": index + 1,
                "text": char,
                "kind": "CHAR_FALLBACK",
                "entry_ids": [],
                "fallback_reason": reason,
            }
        )
    return tokens

