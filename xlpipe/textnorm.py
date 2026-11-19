"""Deterministic text normalization and language identification."""

import re
import unicodedata

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

_STOPWORDS = {
    "en": ("the", "of", "and", "in", "to", "for", "on", "is"),
    "es": ("el", "la", "de", "y", "en", "para", "los", "las"),
    "de": ("der", "die", "und", "das", "zu", "fur", "den", "ist"),
    "fr": ("le", "la", "de", "et", "les", "pour", "des", "est"),
    "zh": (),
}


def normalize_text(text):
    """NFKC + casefold + tokenize. Returns a deterministic term list."""
    normed = unicodedata.normalize("NFKC", text).casefold()
    return _TOKEN_RE.findall(normed)


def normalize_term(term):
    """Normalize a single dictionary term; None if it is not exactly one token."""
    tokens = normalize_text(term)
    return tokens[0] if len(tokens) == 1 else None


def detect_language(tokens, raw_text, languages):
    """Deterministic heuristic: CJK script, then stopword voting.

    Ties break by language code order so the result is stable.
    """
    if any("\u4e00" <= ch <= "\u9fff" for ch in raw_text):
        return "zh" if "zh" in languages else sorted(languages)[0]
    scores = {}
    for lang in languages:
        stops = set(_STOPWORDS.get(lang, ()))
        scores[lang] = sum(1 for tok in tokens if tok in stops)
    return max(sorted(scores), key=lambda lang: scores[lang])
