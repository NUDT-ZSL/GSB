from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Iterable

from .config import PipelineConfig
from .models import QueryTurn
from .util import q6

TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*|[\u4e00-\u9fff]+")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "with",
    "如何",
    "什么",
    "哪些",
    "的",
    "了",
    "与",
    "和",
    "在",
    "中",
    "后",
}


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def tokenize(text: str, drop_stopwords: bool = True) -> list[str]:
    tokens = [match.group(0) for match in TOKEN_RE.finditer(normalize_text(text))]
    if drop_stopwords:
        tokens = [token for token in tokens if token not in STOPWORDS]
    return tokens


def term_counts(text: str) -> dict[str, int]:
    return dict(sorted(Counter(tokenize(text)).items()))


def parse_session_hint(session_hint: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in session_hint.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            result[normalize_text(key.strip())] = normalize_text(value.strip())
        else:
            result[normalize_text(part)] = "true"
    return dict(sorted(result.items()))


def context_turns(
    queries: Iterable[QueryTurn],
    conv_id: str,
    turn_no: int,
    limit: int,
) -> list[QueryTurn]:
    eligible = sorted(
        (
            query
            for query in queries
            if query.conv_id == conv_id and query.turn_no <= turn_no
        ),
        key=lambda item: (item.turn_no, item.query_text),
    )
    return eligible[-limit:]


def context_chain(
    queries: Iterable[QueryTurn],
    conv_id: str,
    turn_no: int,
    config: PipelineConfig,
) -> list[str]:
    return [
        f"{query.conv_id}:{query.turn_no}"
        for query in context_turns(queries, conv_id, turn_no, config.context_turns)
    ]


def normalize_query(
    query: QueryTurn,
    queries: Iterable[QueryTurn],
    config: PipelineConfig,
) -> dict[str, object]:
    turns = context_turns(queries, query.conv_id, query.turn_no, config.context_turns)
    merged: dict[str, float] = {}
    for distance, turn in enumerate(reversed(turns)):
        weight = 1.0 if distance == 0 else config.previous_turn_decay**distance
        for term, count in Counter(tokenize(turn.query_text)).items():
            merged[term] = merged.get(term, 0.0) + weight * count
    hint = parse_session_hint(query.session_hint)
    if "topic" not in hint:
        for turn in reversed(turns):
            previous_hint = parse_session_hint(turn.session_hint)
            if "topic" in previous_hint:
                hint.update(previous_hint)
                break
    return {
        "conv_id": query.conv_id,
        "turn_no": query.turn_no,
        "locale": query.locale,
        "normalized_text": normalize_text(query.query_text),
        "tokens": [
            {"term": term, "weight": q6(weight)}
            for term, weight in sorted(merged.items())
        ],
        "context_chain": [
            f"{turn.conv_id}:{turn.turn_no}" for turn in turns
        ],
        "session_hint": hint,
        "hint_category": hint.get("topic"),
        "config_hash": config.content_hash,
    }
