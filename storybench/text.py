"""Small, deterministic text utilities shared by the workbench."""

from __future__ import annotations

import re
from dataclasses import dataclass


WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+|[^\sA-Za-z\d]")
SENTENCE_END = {".", "!", "?"}
WORD_PATTERN = re.compile(r"^[A-Za-z]+(?:'[A-Za-z]+)?$")


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int

    @property
    def is_word(self) -> bool:
        return bool(WORD_PATTERN.match(self.text))


def tokenize_with_spans(text: str) -> list[Token]:
    """Tokenize words and punctuation while retaining character spans."""
    return [Token(match.group(0), match.start(), match.end()) for match in WORD_RE.finditer(text)]


def tokenize(text: str) -> list[str]:
    return [token.text for token in tokenize_with_spans(text)]


def words(text: str) -> list[str]:
    return [token.text for token in tokenize_with_spans(text) if token.is_word]


def word_count(text: str) -> int:
    return sum(1 for token in tokenize_with_spans(text) if token.is_word)


def normalize_word(token: str) -> str:
    return token.lower()


def detokenize(tokens: list[str] | tuple[str, ...]) -> str:
    """Render a token stream with conventional English spacing and casing."""
    out: list[str] = []
    capitalize_next = True
    for raw in tokens:
        token = raw
        if not token:
            continue
        is_word = bool(WORD_PATTERN.match(token))
        if is_word and capitalize_next:
            token = token[:1].upper() + token[1:]
            capitalize_next = False
        if not out:
            out.append(token)
        elif token in {".", ",", "!", "?", ";", ":"}:
            out[-1] += token
        else:
            out.append(" " + token)
        if raw in SENTENCE_END:
            capitalize_next = True
    return "".join(out)


def split_sentences(text: str) -> list[str]:
    """Split rendered text after sentence-ending punctuation."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [part for part in parts if part]


def word_token_indexes(tokens: list[str]) -> list[int]:
    return [index for index, token in enumerate(tokens) if WORD_PATTERN.match(token)]


def replace_word_at_ordinal(tokens: list[str], ordinal: int, replacement: str) -> list[str]:
    indexes = word_token_indexes(tokens)
    result = list(tokens)
    result[indexes[ordinal]] = replacement
    return result


def delete_word_at_ordinal(tokens: list[str], ordinal: int) -> list[str]:
    indexes = word_token_indexes(tokens)
    index = indexes[ordinal]
    return tokens[:index] + tokens[index + 1 :]


def stable_capitalized(token: str) -> str:
    return token[:1].upper() + token[1:]
