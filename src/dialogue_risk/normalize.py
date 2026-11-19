"""Version-stable text normalization used before every lexical decision."""

from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = {
    ord("\u200b"),  # zero width space
    ord("\u200c"),  # zero width non-joiner
    ord("\u200d"),  # zero width joiner
    ord("\ufeff"),  # byte order mark
}
_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """NFKC, case-fold, remove zero-width chars and collapse whitespace."""

    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.casefold()
    normalized = normalized.translate({codepoint: None for codepoint in _ZERO_WIDTH})
    normalized = normalized.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    normalized = normalized.replace("\t", " ")
    return _WHITESPACE.sub(" ", normalized).strip()

