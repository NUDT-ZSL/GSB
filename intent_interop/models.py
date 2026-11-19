"""Immutable domain models shared by readers, migrations and matchers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

DEFAULT_THRESHOLD = 0.1
DEFAULT_FALLBACK = "fallback_unknown"
SUPPORTED_VERSIONS = ("v1", "v2", "v3")


@dataclass(frozen=True)
class SlotSpec:
    name: str
    placeholder: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "placeholder": self.placeholder}


@dataclass(frozen=True)
class IntentSpec:
    intent: str
    keywords: tuple[str, ...]
    threshold: float | None = None
    fallback_intent: str | None = None
    label_path: tuple[str, ...] | None = None
    slots: tuple[SlotSpec, ...] | None = None
    weight_vector: tuple[float, ...] | None = None
    priority: int | None = None
    line_no: int = 0

    def with_changes(self, **changes: Any) -> "IntentSpec":
        return replace(self, **changes)

    def to_dict(self, version: str) -> dict[str, Any]:
        common: dict[str, Any] = {
            "intent": self.intent,
            "keywords": list(self.keywords),
        }
        if version == "v1":
            common["priority"] = self.priority if self.priority is not None else 0
            return common
        common["threshold"] = (
            self.threshold if self.threshold is not None else DEFAULT_THRESHOLD
        )
        common["fallback_intent"] = (
            self.fallback_intent
            if self.fallback_intent is not None
            else DEFAULT_FALLBACK
        )
        if version == "v2":
            return common
        common["label_path"] = list(self.label_path or (self.intent,))
        common["slots"] = [slot.to_dict() for slot in (self.slots or ())]
        common["weight_vector"] = list(
            self.weight_vector or (1.0 for _ in self.keywords)
        )
        return common


@dataclass(frozen=True)
class ConfigDocument:
    source_version: str
    snapshot_version: int
    controlled_labels: frozenset[str]
    global_fallback: str
    intents: tuple[IntentSpec, ...]
    checksum: str | None = None
    source_path: str | None = None
    metadata: dict[str, str] = field(default_factory=dict, compare=False)
    raw_text: str | None = field(default=None, compare=False, repr=False)

    @property
    def intent_labels(self) -> tuple[str, ...]:
        return tuple(spec.intent for spec in self.intents)

    @property
    def retired_intents(self) -> tuple[str, ...]:
        return tuple(
            spec.intent
            for spec in self.intents
            if spec.intent not in self.controlled_labels
        )

    def with_changes(self, **changes: Any) -> "ConfigDocument":
        return replace(self, **changes)

    def snapshot_summary(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "snapshot_version": self.snapshot_version,
            "checksum": self.checksum,
            "controlled_labels": sorted(self.controlled_labels),
            "global_fallback": self.global_fallback,
            "intent_count": len(self.intents),
            "keyword_count": sum(len(spec.keywords) for spec in self.intents),
            "retired_intents": list(self.retired_intents),
            "source_path": self.source_path,
        }


@dataclass(frozen=True)
class QuerySample:
    query_id: str
    query_text: str
    locale: str
    label_raw: str
    line_no: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query_text": self.query_text,
            "locale": self.locale,
            "label_raw": self.label_raw,
            "line_no": self.line_no,
        }


@dataclass(frozen=True)
class Hit:
    intent: str
    keyword: str
    start: int
    end: int
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "keyword": self.keyword,
            "start": self.start,
            "end": self.end,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class MatchResult:
    query_id: str
    query_text: str
    locale: str
    label_raw: str
    matched_intent: str | None
    intent: str
    confidence: float
    threshold: float
    fallback_category: str | None
    trigger_label: str | None
    hits: tuple[Hit, ...]
    code: str | None = None
    degradation_categories: tuple[str, ...] = ()
    top_hit: Hit | None = None

    @property
    def is_fallback(self) -> bool:
        return self.fallback_category is not None

    @property
    def decision(self) -> str:
        return "fallback" if self.is_fallback else "direct"

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query_text": self.query_text,
            "locale": self.locale,
            "label_raw": self.label_raw,
            "matched_intent": self.matched_intent,
            "intent": self.intent,
            "confidence": self.confidence,
            "threshold": self.threshold,
            "fallback_category": self.fallback_category,
            "trigger_label": self.trigger_label,
            "decision": self.decision,
            "top_hit": self.top_hit.to_dict() if self.top_hit is not None else None,
            "hits": [hit.to_dict() for hit in self.hits],
            "offset_pairs": [[hit.start, hit.end] for hit in self.hits],
            "code": self.code,
            "degradation_categories": list(self.degradation_categories),
            "query_codepoints": len(self.query_text),
        }
