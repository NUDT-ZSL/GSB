from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FormatVersion(str, Enum):
    V1 = "v1"
    V2 = "v2"
    V3 = "v3"

    @classmethod
    def parse(cls, value: str | FormatVersion) -> FormatVersion:
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().lower()
        normalized = normalized.removeprefix("v")
        aliases = {"1": cls.V1, "2": cls.V2, "3": cls.V3}
        if normalized not in aliases:
            from .errors import ErrorCode, fail

            fail(
                ErrorCode.VERSION_UNSUPPORTED,
                f"unsupported dependency arc format version: {value!r}",
                field="version",
                version=str(value),
            )
        return aliases[normalized]


@dataclass
class Arc:
    token_id: int
    head: int
    deprel: str
    confidence_low: float | None = None
    confidence_high: float | None = None
    forward_check: bool | None = None
    reverse_check: bool | None = None
    field_sources: dict[str, str] = field(default_factory=dict)

    def core(self) -> tuple[int, int, str]:
        return (self.token_id, self.head, self.deprel)

    def clone(self) -> Arc:
        return deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token_id": self.token_id,
            "head": self.head,
            "deprel": self.deprel,
        }
        if self.confidence_low is not None or self.confidence_high is not None:
            payload["confidence"] = {
                "low": self.confidence_low,
                "high": self.confidence_high,
            }
        if self.forward_check is not None or self.reverse_check is not None:
            payload["dependency_checks"] = {
                "forward": self.forward_check,
                "reverse": self.reverse_check,
            }
        if self.field_sources:
            payload["field_sources"] = dict(sorted(self.field_sources.items()))
        return payload


@dataclass(frozen=True)
class BidirectionalChain:
    chain_id: str
    token_ids: tuple[int, ...]
    forward: bool = True
    reverse: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "token_ids": list(self.token_ids),
            "forward": self.forward,
            "reverse": self.reverse,
        }


@dataclass(frozen=True)
class CrossSentenceRef:
    source_token_id: int
    target_sent_id: str
    target_token_id: int
    relation: str
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_token_id": self.source_token_id,
            "target_sent_id": self.target_sent_id,
            "target_token_id": self.target_token_id,
            "relation": self.relation,
        }
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        return payload


@dataclass
class SentenceGraph:
    sent_id: str
    arcs: dict[int, Arc]
    root_marker: int | None = None
    root_source: str = "unobserved"
    bidirectional_chains: list[BidirectionalChain] = field(default_factory=list)
    cross_sentence_refs: list[CrossSentenceRef] = field(default_factory=list)

    def ordered_arcs(self) -> list[Arc]:
        return [self.arcs[token_id] for token_id in sorted(self.arcs)]

    def root_arc_ids(self) -> list[int]:
        return sorted(arc.token_id for arc in self.arcs.values() if arc.head == 0)

    def effective_root(self) -> int | None:
        if self.root_marker is not None:
            return self.root_marker
        roots = self.root_arc_ids()
        return roots[0] if len(roots) == 1 else None

    def core_signature(self) -> tuple[tuple[int, int, str], ...]:
        return tuple(arc.core() for arc in self.ordered_arcs())

    def chain_token_ids(self) -> set[int]:
        result: set[int] = set()
        for chain in self.bidirectional_chains:
            result.update(chain.token_ids)
        return result

    def clone(self) -> SentenceGraph:
        return deepcopy(self)


@dataclass
class ArcGraph:
    sentences: dict[str, SentenceGraph] = field(default_factory=dict)
    format_version: FormatVersion | str | None = None
    source_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def add(self, sentence: SentenceGraph) -> None:
        self.sentences[sentence.sent_id] = sentence

    def sent_ids(self) -> list[str]:
        return list(self.sentences)

    def node_count(self) -> int:
        return sum(len(sentence.arcs) for sentence in self.sentences.values())

    def clone(self) -> ArcGraph:
        return deepcopy(self)

    def core_signature(self) -> dict[str, tuple[tuple[int, int, str], ...]]:
        return {
            sent_id: sentence.core_signature()
            for sent_id, sentence in self.sentences.items()
        }

    def iter_arcs(self) -> Iterable[tuple[str, Arc]]:
        for sent_id, sentence in self.sentences.items():
            for arc in sentence.ordered_arcs():
                yield sent_id, arc
