from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .config import ControlledSnapshot
from .input import (
    AnnotatedSentence,
    Degradation,
    ResourceLimits,
    iter_annotated_sentences,
)
from .migration import MigrationResult, migrate
from .models import FormatVersion
from .source import SourceLike


@dataclass(frozen=True)
class StreamChunk:
    sequence: int
    sent_id: str
    source_line: int
    annotated: AnnotatedSentence
    migration: MigrationResult
    degraded: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "sent_id": self.sent_id,
            "source_line": self.source_line,
            "source_version": self.migration.source_version.value,
            "target_version": self.migration.target_version.value,
            "asserted_tokens": [token.token_id for token in self.annotated.tokens],
            "core_signature": [
                list(arc.core())
                for arc in self.migration.graph.sentences[self.sent_id].ordered_arcs()
            ],
            "fill_count": len(self.migration.fills),
            "drop_count": len(self.migration.drops),
            "degraded": self.degraded,
        }


@dataclass
class StreamMigrationSummary:
    target_version: FormatVersion
    source_name: str
    chunks: list[StreamChunk] = field(default_factory=list)
    degraded: bool = False
    degradation: Degradation | None = None
    node_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "target_version": self.target_version.value,
            "sentence_count": len(self.chunks),
            "node_count": self.node_count,
            "degraded": self.degraded,
            "degradation": (
                self.degradation.to_dict() if self.degradation is not None else None
            ),
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }


def iter_stream_migrations(
    source: SourceLike,
    target_version: str | FormatVersion,
    snapshot: ControlledSnapshot,
    limits: ResourceLimits,
) -> Iterator[StreamChunk]:
    target = FormatVersion.parse(target_version)
    total_nodes = 0
    degraded = False
    for sequence, (_, line_no, annotated, graph) in enumerate(
        iter_annotated_sentences(
            source,
            snapshot,
            max_tokens_per_sentence=limits.max_tokens_per_sentence,
        ),
        start=1,
    ):
        if total_nodes + len(annotated.tokens) > limits.max_total_nodes:
            degraded = True
        migration = migrate(
            graph,
            target,
            snapshot,
            source_version=FormatVersion.V1,
        )
        total_nodes += len(annotated.tokens)
        yield StreamChunk(
            sequence=sequence,
            sent_id=annotated.sent_id,
            source_line=line_no,
            annotated=annotated,
            migration=migration,
            degraded=degraded,
        )


def stream_migrate_samples(
    source: SourceLike,
    target_version: str | FormatVersion,
    snapshot: ControlledSnapshot,
    *,
    limits: ResourceLimits | None = None,
) -> StreamMigrationSummary:
    limits = limits or ResourceLimits(mode="stream")
    target = FormatVersion.parse(target_version)
    summary = StreamMigrationSummary(target_version=target, source_name="<memory>")
    for chunk in iter_stream_migrations(source, target, snapshot, limits):
        summary.source_name = "streaming-jsonl"
        summary.chunks.append(chunk)
        summary.node_count += len(chunk.annotated.tokens)
        if chunk.degraded and not summary.degraded:
            summary.degraded = True
            summary.degradation = Degradation(
                category="TOTAL_NODE_LIMIT_STREAMING",
                reason=(
                    "total node limit reached; migration switched to per-sentence "
                    "streaming output without changing emitted arcs"
                ),
                field="node_count",
                limit=limits.max_total_nodes,
                observed_at_sentence=chunk.sent_id,
                sentences_emitted_before=chunk.sequence - 1,
                nodes_emitted_before=summary.node_count - len(chunk.annotated.tokens),
            )
    return summary
