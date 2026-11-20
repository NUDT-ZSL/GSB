"""Shared immutable domain models."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping


@dataclass(frozen=True)
class Condition:
    op: str
    term: str | None = None
    value: int | None = None
    children: tuple["Condition", ...] = ()

    def to_dict(self) -> dict[str, Any]:
        if self.op in {"term_present", "min_freq"}:
            result: dict[str, Any] = {"op": self.op, "term": self.term}
            if self.op == "min_freq":
                result["value"] = self.value
            return result
        return {"op": self.op, "children": [child.to_dict() for child in self.children]}

    def matches(self, term_counts: Mapping[str, int]) -> bool:
        if self.op == "term_present":
            return self.term in term_counts
        if self.op == "min_freq":
            return term_counts.get(self.term or "", 0) >= (self.value or 0)
        if self.op == "all":
            return all(child.matches(term_counts) for child in self.children)
        if self.op == "any":
            return any(child.matches(term_counts) for child in self.children)
        return False

    def conjunction_terms(self) -> tuple[str, ...] | None:
        """Return terms when this condition is only an AND of term_present."""

        if self.op == "term_present":
            return (self.term or "",)
        if self.op != "all" or not self.children:
            return None
        collected: list[str] = []
        for child in self.children:
            terms = child.conjunction_terms()
            if terms is None:
                return None
            collected.extend(terms)
        return tuple(sorted(set(collected)))


@dataclass(frozen=True)
class Target:
    lang: str
    term: str

    def to_dict(self) -> dict[str, str]:
        return {"lang": self.lang, "term": self.term}


@dataclass(frozen=True)
class RewriteRule:
    rule_id: str
    source_lang: str
    source_term: str
    targets: tuple[Target, ...]
    condition: Condition | None = None
    bridge_confidence_weight: float | None = None
    max_expansion_depth: int | None = None
    enabled: bool | None = None
    condition_terms: tuple[str, ...] = ()
    source_location: Mapping[str, Any] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )

    def effective_weight(self) -> float:
        return 1.0 if self.bridge_confidence_weight is None else self.bridge_confidence_weight

    def effective_depth(self) -> int:
        return 1 if self.max_expansion_depth is None else self.max_expansion_depth

    def effective_enabled(self) -> bool:
        return True if self.enabled is None else self.enabled

    def with_targets(self, targets: tuple[Target, ...]) -> "RewriteRule":
        return replace(self, targets=targets)


@dataclass(frozen=True)
class RuleSet:
    format_version: str
    rules: tuple[RewriteRule, ...]

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.rule_id for rule in self.rules)


@dataclass(frozen=True)
class BridgeEntry:
    source_lang: str
    target_lang: str
    source_term: str
    target_term: str
    confidence: float

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.source_lang, self.target_lang, self.source_term, self.target_term)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "source_term": self.source_term,
            "target_term": self.target_term,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class LanguagePair:
    source_lang: str
    target_lang: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
        }


@dataclass(frozen=True)
class BridgeResource:
    version: int
    format_version: str
    language_pairs: tuple[LanguagePair, ...]
    entries: tuple[BridgeEntry, ...]
    checksum: str

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "format_version": self.format_version,
            "version": self.version,
            "language_pairs": [pair.to_dict() for pair in self.language_pairs],
            "entries": [entry.to_dict() for entry in self.entries],
        }
        if include_checksum:
            result["checksum"] = self.checksum
        return result
