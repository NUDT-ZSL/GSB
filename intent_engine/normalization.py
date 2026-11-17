"""Sentence normalization and synonym replacement."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import BusinessError
from .injection import Injection
from .models import Sample
from .resources import EngineConfig, SynonymRule
from .utils import stable_hash


@dataclass(frozen=True)
class SynonymApplication:
    source: str
    target: str
    start: int
    end: int
    rule_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "start": self.start,
            "end": self.end,
            "rule_hash": self.rule_hash,
        }


def _base_normalize(text: str, config: EngineConfig) -> str:
    result = unicodedata.normalize(str(config.normalize.get("unicode_form", "NFKC")), text)
    if config.normalize.get("collapse_whitespace", True):
        result = " ".join(result.split())
    if config.normalize.get("strip", True):
        result = result.strip()
    return result


def _apply_synonyms(text: str, synonyms: Iterable[SynonymRule]) -> tuple[str, list[SynonymApplication]]:
    rules = [rule for rule in synonyms if rule.source]
    if not rules:
        return text, []
    output: list[str] = []
    applications: list[SynonymApplication] = []
    position = 0
    while position < len(text):
        matches = [rule for rule in rules if text.startswith(rule.source, position)]
        if not matches:
            output.append(text[position])
            position += 1
            continue
        matches.sort(key=lambda rule: (-len(rule.source), rule.source, rule.target))
        rule = matches[0]
        end = position + len(rule.source)
        output.append(rule.target)
        applications.append(
            SynonymApplication(
                source=rule.source,
                target=rule.target,
                start=position,
                end=end,
                rule_hash=stable_hash(rule.raw),
            )
        )
        position = end
    return "".join(output), applications


class Normalizer:
    def __init__(self, config: EngineConfig, synonyms: Iterable[SynonymRule]) -> None:
        self.config = config
        self.synonyms = tuple(synonyms)

    def normalize(
        self,
        sample: Sample,
        stage_injections: tuple[Injection, ...] = (),
    ) -> dict[str, Any]:
        injection_ids = [item.injection_id for item in stage_injections]
        for injection in stage_injections:
            if injection.fault == "empty_sentence":
                raise BusinessError(
                    "E_EMPTY_SENTENCE",
                    "injected empty sentence",
                    stage="normalize",
                    details={"injection_ids": injection_ids},
                )
            if injection.fault == "missing_chars":
                raise BusinessError(
                    "E_BAD_RECORD",
                    "injected missing character sequence",
                    stage="normalize",
                    details={"injection_ids": injection_ids},
                )
        base = _base_normalize(sample.text, self.config)
        if not base:
            raise BusinessError(
                "E_EMPTY_SENTENCE",
                "sentence is empty after normalization",
                stage="normalize",
                details={"injection_ids": injection_ids},
            )
        if self.config.normalize.get("apply_synonyms", True):
            normalized, applications = _apply_synonyms(base, self.synonyms)
        else:
            normalized, applications = base, []
        if not normalized:
            raise BusinessError(
                "E_EMPTY_SENTENCE",
                "sentence became empty after synonym normalization",
                stage="normalize",
                details={"injection_ids": injection_ids},
            )
        return {
            "text": normalized,
            "base_text": base,
            "chars": list(normalized),
            "applied_synonyms": [item.as_dict() for item in applications],
            "source_text": sample.text,
            "injection_ids": injection_ids,
            "params": {
                "unicode_form": self.config.normalize.get("unicode_form", "NFKC"),
                "collapse_whitespace": bool(self.config.normalize.get("collapse_whitespace", True)),
                "strip": bool(self.config.normalize.get("strip", True)),
                "apply_synonyms": bool(self.config.normalize.get("apply_synonyms", True)),
                "applied_rule_hashes": [item.rule_hash for item in applications],
            },
        }
