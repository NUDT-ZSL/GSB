"""Deterministic vocabulary-bound tokenization."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import BusinessError
from .models import Vocabulary


_SEPARATOR = re.compile(r"[\s，。！？、；：,.!?;:()（）【】\[\]\"'“”‘’]+")


@dataclass(frozen=True)
class Token:
    word: str
    turn_index: int
    message_index: int
    token_index: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "word": self.word,
            "turn_index": self.turn_index,
            "message_index": self.message_index,
            "token_index": self.token_index,
        }


def _segments(text: str) -> Iterable[str]:
    for segment in _SEPARATOR.split(text):
        if segment:
            yield segment


def tokenize_text(
    text: str,
    vocabulary: Vocabulary,
    *,
    session_id: Optional[str] = None,
    turn_index: Optional[int] = None,
    field: str = "text",
) -> List[str]:
    """Tokenize with longest-match vocabulary tokenization.

    Whitespace/punctuation separated words are accepted directly. For a
    contiguous run, the longest vocabulary word at each position wins. An
    unknown character or run is reported as E_VOCAB_MISSING rather than being
    silently discarded.
    """

    words = vocabulary.word_set
    max_word_length = max((len(word) for word in words), default=0)
    result: List[str] = []
    for segment in _segments(text):
        if segment in words:
            result.append(segment)
            continue
        index = 0
        while index < len(segment):
            matched = None
            upper = min(max_word_length, len(segment) - index)
            for size in range(upper, 0, -1):
                candidate = segment[index : index + size]
                if candidate in words:
                    matched = candidate
                    break
            if matched is None:
                missing = segment[index : index + 12]
                raise BusinessError(
                    "E_VOCAB_MISSING",
                    f"vocabulary {vocabulary.vocab_version} does not contain token {missing!r}",
                    session_id=session_id,
                    field=field,
                    details={
                        "vocab_version": vocabulary.vocab_version,
                        "word": missing,
                        "turn_index": turn_index,
                    },
                )
            result.append(matched)
            index += len(matched)
    return result


def tokenize_turn(
    text: str,
    vocabulary: Vocabulary,
    *,
    session_id: str,
    turn_index: int,
    message_index: int,
) -> List[Token]:
    words = tokenize_text(
        text,
        vocabulary,
        session_id=session_id,
        turn_index=turn_index,
        field=f"turns[{turn_index}].text",
    )
    return [
        Token(
            word=word,
            turn_index=turn_index,
            message_index=message_index,
            token_index=index,
        )
        for index, word in enumerate(words)
    ]


def frequency_rows(tokens: Sequence[Token]) -> List[Dict[str, object]]:
    groups: Dict[str, List[Token]] = {}
    for token in tokens:
        groups.setdefault(token.word, []).append(token)
    rows = []
    for word, occurrences in groups.items():
        first = min(
            occurrences,
            key=lambda item: (
                item.turn_index,
                item.message_index,
                item.token_index,
            ),
        )
        rows.append(
            {
                "word": word,
                "count": len(occurrences),
                "first_turn_index": first.turn_index,
                "first_message_index": first.message_index,
                "first_token_index": first.token_index,
            }
        )
    rows.sort(
        key=lambda row: (
            -int(row["count"]),
            int(row["first_turn_index"]),
            int(row["first_message_index"]),
            int(row["first_token_index"]),
            str(row["word"]),
        )
    )
    return rows


def token_frequencies(words: Sequence[str]) -> List[Dict[str, object]]:
    counts = Counter(words)
    return [
        {"word": word, "count": count}
        for word, count in sorted(counts.items(), key=lambda item: item[0])
    ]
