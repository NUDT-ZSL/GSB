from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from .config import ControlledSnapshot
from .errors import ErrorCode, fail
from .models import Arc, ArcGraph, SentenceGraph
from .source import SourceLike, read_source
from .validation import validate_pos, validate_sentence


@dataclass(frozen=True)
class ResourceLimits:
    max_total_nodes: int = 10_000
    max_tokens_per_sentence: int = 512
    mode: Literal["strict", "stream"] = "strict"

    def __post_init__(self) -> None:
        if self.max_total_nodes < 1:
            raise ValueError("max_total_nodes must be positive")
        if self.max_tokens_per_sentence < 1:
            raise ValueError("max_tokens_per_sentence must be positive")
        if self.mode not in ("strict", "stream"):
            raise ValueError("mode must be 'strict' or 'stream'")


@dataclass(frozen=True)
class RawToken:
    token_id: int
    word: str
    pos: str
    head_gold: int
    deprel_gold: str


@dataclass(frozen=True)
class AnnotatedSentence:
    sent_id: str
    tokens: tuple[RawToken, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent_id": self.sent_id,
            "tokens": [
                {
                    "id": token.token_id,
                    "word": token.word,
                    "pos": token.pos,
                    "head_gold": token.head_gold,
                    "deprel_gold": token.deprel_gold,
                }
                for token in self.tokens
            ],
        }


@dataclass(frozen=True)
class Degradation:
    category: str
    reason: str
    field: str
    limit: int
    observed_at_sentence: str
    sentences_emitted_before: int
    nodes_emitted_before: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "reason": self.reason,
            "field": self.field,
            "limit": self.limit,
            "observed_at_sentence": self.observed_at_sentence,
            "sentences_emitted_before": self.sentences_emitted_before,
            "nodes_emitted_before": self.nodes_emitted_before,
        }


@dataclass
class SampleLoadResult:
    annotated: list[AnnotatedSentence] = field(default_factory=list)
    graphs: list[ArcGraph] = field(default_factory=list)
    degraded: bool = False
    degradation: Degradation | None = None
    node_count: int = 0
    source_name: str = "<memory>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "sentence_count": len(self.annotated),
            "node_count": self.node_count,
            "sent_ids": [sentence.sent_id for sentence in self.annotated],
            "degraded": self.degraded,
            "degradation": self.degradation.to_dict() if self.degradation else None,
            "arc_signatures": {
                graph.sent_ids()[0]: graph.core_signature()[graph.sent_ids()[0]]
                for graph in self.graphs
                if graph.sent_ids()
            },
        }


def _json_field(
    mapping: dict[str, Any],
    key: str,
    *,
    line: int,
    sent_id: str | None,
    token_id: int | None = None,
) -> Any:
    if key not in mapping:
        fail(
            ErrorCode.FORMAT_CORRUPT,
            f"required JSONL field {key!r} is missing",
            field=key,
            line=line,
            sent_id=sent_id,
            token_id=token_id,
        )
    return mapping[key]


def _parse_token(
    raw: Any,
    *,
    expected_id: int,
    sent_id: str,
    line: int,
    snapshot: ControlledSnapshot,
) -> RawToken:
    if not isinstance(raw, dict):
        fail(
            ErrorCode.FORMAT_CORRUPT,
            "token entry must be a JSON object",
            field="tokens",
            line=line,
            sent_id=sent_id,
        )
    token_id = _json_field(raw, "id", line=line, sent_id=sent_id)
    if (
        not isinstance(token_id, int)
        or isinstance(token_id, bool)
        or token_id != expected_id
    ):
        fail(
            ErrorCode.TOKEN_SEQ,
            "token ids must be consecutive and start at 1",
            field="id",
            line=line,
            sent_id=sent_id,
            token_id=token_id,
            expected=expected_id,
            actual=token_id,
        )
    word = _json_field(raw, "word", line=line, sent_id=sent_id, token_id=token_id)
    if not isinstance(word, str):
        fail(
            ErrorCode.FORMAT_CORRUPT,
            "token word must be a string",
            field="word",
            line=line,
            sent_id=sent_id,
            token_id=token_id,
        )
    pos = _json_field(raw, "pos", line=line, sent_id=sent_id, token_id=token_id)
    validate_pos(sent_id=sent_id, token_id=token_id, pos=pos, snapshot=snapshot)
    head = _json_field(
        raw,
        "head_gold",
        line=line,
        sent_id=sent_id,
        token_id=token_id,
    )
    if not isinstance(head, int) or isinstance(head, bool):
        fail(
            ErrorCode.FORMAT_CORRUPT,
            "head_gold must be an integer",
            field="head_gold",
            line=line,
            sent_id=sent_id,
            token_id=token_id,
            actual=head,
        )
    deprel = _json_field(
        raw,
        "deprel_gold",
        line=line,
        sent_id=sent_id,
        token_id=token_id,
    )
    if not isinstance(deprel, str) or not deprel:
        fail(
            ErrorCode.FORMAT_CORRUPT,
            "deprel_gold must be a non-empty string",
            field="deprel_gold",
            line=line,
            sent_id=sent_id,
            token_id=token_id,
        )
    return RawToken(
        token_id=token_id,
        word=word,
        pos=pos,
        head_gold=head,
        deprel_gold=deprel,
    )


def _parse_sentence(
    raw: Any,
    *,
    line: int,
    snapshot: ControlledSnapshot,
) -> tuple[AnnotatedSentence, ArcGraph]:
    if not isinstance(raw, dict):
        fail(
            ErrorCode.FORMAT_CORRUPT,
            "JSONL row must be a JSON object",
            field="root",
            line=line,
        )
    sent_id = _json_field(raw, "sent_id", line=line, sent_id=None)
    if not isinstance(sent_id, str) or not sent_id:
        fail(
            ErrorCode.FORMAT_CORRUPT,
            "sent_id must be a non-empty string",
            field="sent_id",
            line=line,
        )
    raw_tokens = _json_field(raw, "tokens", line=line, sent_id=sent_id)
    if not isinstance(raw_tokens, list) or not raw_tokens:
        fail(
            ErrorCode.FORMAT_CORRUPT,
            "tokens must be a non-empty list",
            field="tokens",
            line=line,
            sent_id=sent_id,
        )
    tokens = tuple(
        _parse_token(
            token,
            expected_id=expected,
            sent_id=sent_id,
            line=line,
            snapshot=snapshot,
        )
        for expected, token in enumerate(raw_tokens, start=1)
    )
    arcs = {
        token.token_id: Arc(
            token_id=token.token_id,
            head=token.head_gold,
            deprel=token.deprel_gold,
        )
        for token in tokens
    }
    sentence = SentenceGraph(
        sent_id=sent_id,
        arcs=arcs,
        root_marker=None,
        root_source="inferred",
    )
    validate_sentence(sentence, snapshot)
    graph = ArcGraph(
        sentences={sent_id: sentence},
        format_version="raw-jsonl",
        source_name="raw-jsonl",
    )
    return AnnotatedSentence(sent_id=sent_id, tokens=tokens), graph


def load_annotated_samples(
    source: SourceLike,
    snapshot: ControlledSnapshot,
    limits: ResourceLimits | None = None,
) -> SampleLoadResult:
    limits = limits or ResourceLimits()
    source_text = read_source(source)
    result = SampleLoadResult(source_name=source_text.name)
    seen_sent_ids: set[str] = set()
    for line_no, raw_line in enumerate(source_text.text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            fail(
                ErrorCode.FORMAT_CORRUPT,
                "JSONL row is not valid JSON",
                field="json",
                line=line_no,
                column=exc.colno,
            )
        annotated, graph = _parse_sentence(raw, line=line_no, snapshot=snapshot)
        if annotated.sent_id in seen_sent_ids:
            fail(
                ErrorCode.DUP_SENT,
                "duplicate sent_id in JSONL corpus",
                field="sent_id",
                line=line_no,
                sent_id=annotated.sent_id,
            )
        seen_sent_ids.add(annotated.sent_id)
        token_count = len(annotated.tokens)
        if token_count > limits.max_tokens_per_sentence:
            fail(
                ErrorCode.RESOURCE_LIMIT,
                "sentence token count exceeds the configured limit",
                field="token_count",
                line=line_no,
                sent_id=annotated.sent_id,
                limit=limits.max_tokens_per_sentence,
                actual=token_count,
                degradation=None,
            )
        if result.node_count + token_count > limits.max_total_nodes:
            if limits.mode == "strict":
                fail(
                    ErrorCode.RESOURCE_LIMIT,
                    "total dependency graph node count exceeds the configured limit",
                    field="node_count",
                    line=line_no,
                    sent_id=annotated.sent_id,
                    limit=limits.max_total_nodes,
                    actual=result.node_count + token_count,
                    degradation_available="stream",
                )
            if not result.degraded:
                result.degraded = True
                result.degradation = Degradation(
                    category="TOTAL_NODE_LIMIT_STREAMING",
                    reason=(
                        "total node limit reached; remaining sentences are processed "
                        "in sentence-by-sentence streaming mode"
                    ),
                    field="node_count",
                    limit=limits.max_total_nodes,
                    observed_at_sentence=annotated.sent_id,
                    sentences_emitted_before=len(result.annotated),
                    nodes_emitted_before=result.node_count,
                )
        result.annotated.append(annotated)
        result.graphs.append(graph)
        result.node_count += token_count
    if not result.annotated:
        fail(
            ErrorCode.FORMAT_CORRUPT,
            "JSONL corpus contains no sentences",
            field="sent_id",
            line=1,
        )
    return result


def parse_jsonl_sentence(
    raw_line: str,
    *,
    line_no: int,
    snapshot: ControlledSnapshot,
) -> tuple[AnnotatedSentence, ArcGraph]:
    try:
        raw = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        fail(
            ErrorCode.FORMAT_CORRUPT,
            "JSONL row is not valid JSON",
            field="json",
            line=line_no,
            column=exc.colno,
        )
    return _parse_sentence(raw, line=line_no, snapshot=snapshot)


def iter_annotated_sentences(
    source: SourceLike,
    snapshot: ControlledSnapshot,
    *,
    max_tokens_per_sentence: int = 512,
) -> Iterator[tuple[str, int, AnnotatedSentence, ArcGraph]]:
    source_text = read_source(source)
    seen_sent_ids: set[str] = set()
    for line_no, raw_line in enumerate(source_text.text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        annotated, graph = parse_jsonl_sentence(
            line,
            line_no=line_no,
            snapshot=snapshot,
        )
        if annotated.sent_id in seen_sent_ids:
            fail(
                ErrorCode.DUP_SENT,
                "duplicate sent_id in JSONL corpus",
                field="sent_id",
                line=line_no,
                sent_id=annotated.sent_id,
            )
        seen_sent_ids.add(annotated.sent_id)
        token_count = len(annotated.tokens)
        if token_count > max_tokens_per_sentence:
            fail(
                ErrorCode.RESOURCE_LIMIT,
                "sentence token count exceeds the configured limit",
                field="token_count",
                line=line_no,
                sent_id=annotated.sent_id,
                limit=max_tokens_per_sentence,
                actual=token_count,
                degradation=None,
            )
        yield source_text.name, line_no, annotated, graph
