from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .errors import WorkbenchError


SEGMENT_PATTERN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)


class SubwordTokenizer:
    def __init__(self, vocab_document: dict[str, Any], rules: dict[str, Any]) -> None:
        tokens = vocab_document.get("tokens")
        if not isinstance(tokens, list) or not all(isinstance(item, str) for item in tokens):
            raise WorkbenchError("E_INPUT_INVALID", "vocab.tokens must be a string array", "input")
        self.vocabulary = {token.lower() for token in tokens}
        self.max_token_length = int(rules.get("max_token_length", 8))
        if self.max_token_length < 1:
            raise WorkbenchError("E_INPUT_INVALID", "max_token_length must be >= 1", "input")
        self.normalize = str(rules.get("normalize", "lower"))
        if self.normalize not in {"lower", "identity"}:
            raise WorkbenchError("E_INPUT_INVALID", "normalize must be lower or identity", "input")
        self.fallback = str(rules.get("unknown_fallback", "raw_char"))
        if self.fallback not in {"raw_char", "unknown_token"}:
            raise WorkbenchError(
                "E_INPUT_INVALID",
                "unknown_fallback must be raw_char or unknown_token",
                "input",
            )
        self.unknown_token = str(rules.get("unknown_token", "<unk>"))

    def _segment(self, segment: str) -> list[str]:
        output: list[str] = []
        index = 0
        while index < len(segment):
            limit = min(self.max_token_length, len(segment) - index)
            matched = None
            for size in range(limit, 0, -1):
                candidate = segment[index:index + size]
                if candidate in self.vocabulary:
                    matched = candidate
                    break
            if matched is None:
                if self.fallback == "unknown_token":
                    output.append(self.unknown_token)
                else:
                    output.append(segment[index])
                index += 1
            else:
                output.append(matched)
                index += len(matched)
        return output

    def tokenize(self, text: str) -> list[str]:
        normalized = text.lower() if self.normalize == "lower" else text
        result: list[str] = []
        for match in SEGMENT_PATTERN.finditer(normalized):
            result.extend(self._segment(match.group(0)))
        return result

    def token_set(self, text: str) -> set[str]:
        return set(self.tokenize(text))


def load_tokenizer(input_dir: Path, vocab: dict[str, Any], rules: dict[str, Any]) -> SubwordTokenizer:
    return SubwordTokenizer(vocab, rules)

