"""Independent vocabulary and sentence-length checker.

This module intentionally re-implements tokenization and sentence splitting
instead of importing the rewrite pipeline's helpers.  It is therefore able to
catch an accidental mismatch between the decoder's notion of a sentence and
the exported prose.
"""

from __future__ import annotations

import re
from typing import Any


_TOKEN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+|[^\sA-Za-z\d]")
_WORD = re.compile(r"^[A-Za-z]+(?:'[A-Za-z]+)?$")


class IndependentReadabilityChecker:
    def __init__(self, levels: list[dict[str, Any]]):
        self.levels = {level["level_id"]: level for level in levels}

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part]

    @staticmethod
    def _words(text: str) -> list[str]:
        return [token for token in _TOKEN.findall(text) if _WORD.match(token)]

    def check(self, text: str, level_id: str, **identity: Any) -> dict[str, Any]:
        level = self.levels[level_id]
        allowed = {word.lower() for word in level["allowed_words"]}
        limit = int(level["max_words"])
        checked = []
        violations = []
        for sentence_index, sentence in enumerate(self._sentences(text), start=1):
            sentence_words = self._words(sentence)
            checked.append(
                {
                    "sentence_index": sentence_index,
                    "fragment": sentence,
                    "word_count": len(sentence_words),
                    "max_words": limit,
                }
            )
            if len(sentence_words) > limit:
                violations.append(
                    {
                        "constraint": "max_words",
                        "sentence_index": sentence_index,
                        "fragment": sentence,
                        "limit": limit,
                        "observed": len(sentence_words),
                        "excess": len(sentence_words) - limit,
                        "rule_ref": level["constraint_refs"]["max_words"],
                    }
                )
            for word_ordinal, word in enumerate(sentence_words, start=1):
                normalized = word.lower()
                if normalized not in allowed:
                    violations.append(
                        {
                            "constraint": "vocabulary",
                            "sentence_index": sentence_index,
                            "word_ordinal": word_ordinal,
                            "word": word,
                            "normalized_word": normalized,
                            "rule_ref": level["constraint_refs"]["vocabulary"],
                        }
                    )
        result = {
            "checker": "independent-readability-v1",
            "story_id": identity.get("story_id"),
            "level_id": level_id,
            "sentence_id": identity.get("sentence_id"),
            "unit_id": identity.get("unit_id"),
            "text": text,
            "limit": limit,
            "checked_sentences": checked,
            "violations": violations,
            "violation_count": len(violations),
            "passed": not violations,
        }
        return result

    def report(self, checks: list[dict[str, Any]]) -> dict[str, Any]:
        bad_units = [check for check in checks if not check["passed"]]
        violation_rows = [
            {
                "story_id": check["story_id"],
                "level_id": check["level_id"],
                "sentence_id": check["sentence_id"],
                "unit_id": check["unit_id"],
                **violation,
            }
            for check in checks
            for violation in check["violations"]
        ]
        violation_rows.sort(
            key=lambda row: (
                row["story_id"],
                row["level_id"],
                row["sentence_id"],
                row["sentence_index"],
                row["constraint"],
                row.get("word_ordinal", 0),
            )
        )
        return {
            "checker": "independent-readability-v1",
            "checked_unit_count": len(checks),
            "passed_unit_count": len(checks) - len(bad_units),
            "violating_unit_count": len(bad_units),
            "violation_count": len(violation_rows),
            "violating_units": [
                {
                    "story_id": check["story_id"],
                    "level_id": check["level_id"],
                    "sentence_id": check["sentence_id"],
                    "unit_id": check["unit_id"],
                    "text": check["text"],
                    "violations": check["violations"],
                }
                for check in bad_units
            ],
            "violations": violation_rows,
            "passed": not violation_rows,
        }
