"""Revision diffing and order-independent transitive invalidation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import ParseResult


@dataclass(frozen=True)
class RevisionBatch:
    file_id: str
    revision_id: str
    segments: dict[str, str]
    edges: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    parent_revision: str | None = None

    @classmethod
    def from_result(
        cls,
        result: ParseResult,
        revision_id: str,
        parent_revision: str | None = None,
    ) -> "RevisionBatch":
        segments: dict[str, str] = {}
        for segment in result.normalized_segments():
            if segment.duplicate_of is not None:
                continue
            segments[segment.key] = segment.content_digest or segment.actual_digest or segment.declared_digest
        return cls(
            file_id=result.file_id,
            revision_id=revision_id,
            segments=segments,
            edges=frozenset(result.edges),
            parent_revision=parent_revision,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "revision_id": self.revision_id,
            "parent_revision": self.parent_revision,
            "segments": {key: self.segments[key] for key in sorted(self.segments)},
            "edges": sorted([list(edge) for edge in self.edges]),
        }


def compute_invalidation(
    old: RevisionBatch | dict[str, Any] | None,
    new: RevisionBatch | dict[str, Any],
    *,
    old_edges: Iterable[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    old_batch = _coerce_batch(old)
    new_batch = _coerce_batch(new)
    if old_batch is not None and old_batch.file_id != new_batch.file_id:
        raise ValueError("cannot compare revisions for different file_id values")

    old_segments = old_batch.segments if old_batch else {}
    new_segments = new_batch.segments
    old_edge_set = set(old_batch.edges if old_batch else [])
    if old_edges is not None:
        old_edge_set.update(tuple(edge) for edge in old_edges)
    edge_set = set(new_batch.edges) | old_edge_set

    changes: dict[str, dict[str, str | None]] = {}
    for key in sorted(set(old_segments) | set(new_segments)):
        old_digest = old_segments.get(key)
        new_digest = new_segments.get(key)
        if old_digest is None and new_digest is not None:
            changes[key] = {"kind": "added", "old_digest": None, "new_digest": new_digest}
        elif old_digest is not None and new_digest is None:
            changes[key] = {"kind": "removed", "old_digest": old_digest, "new_digest": None}
        elif old_digest != new_digest:
            changes[key] = {
                "kind": "modified",
                "old_digest": old_digest,
                "new_digest": new_digest,
            }

    reverse_graph: dict[str, set[str]] = {}
    all_keys = set(old_segments) | set(new_segments)
    for dependent, prerequisite in edge_set:
        reverse_graph.setdefault(prerequisite, set()).add(dependent)
        all_keys.add(dependent)
        all_keys.add(prerequisite)

    affected_sources: dict[str, set[str]] = {key: {key} for key in changes}
    distances: dict[str, dict[str, int]] = {key: {key: 0} for key in changes}

    for source in sorted(changes):
        queue: list[tuple[str, int]] = [(source, 0)]
        seen = {source: 0}
        while queue:
            current, distance = queue.pop(0)
            for dependent in sorted(reverse_graph.get(current, set())):
                next_distance = distance + 1
                if dependent not in seen or next_distance < seen[dependent]:
                    seen[dependent] = next_distance
                    queue.append((dependent, next_distance))
        for affected, distance in seen.items():
            if affected == source:
                continue
            affected_sources.setdefault(affected, set()).add(source)
            distances.setdefault(affected, {})[source] = distance

    recompute = sorted(set(changes) | set(affected_sources))
    items: list[dict[str, Any]] = []
    for key in recompute:
        sources = sorted(affected_sources.get(key, {key}))
        source_distances = distances.get(key, {})
        items.append(
            {
                "segment_key": key,
                "needs_recompute": True,
                "is_change_source": key in changes,
                "change": changes.get(key),
                "invalidation_sources": sources,
                "minimum_dependency_distance": min(
                    source_distances.get(source, 0) for source in sources
                ),
            }
        )

    source_report = [
        {"segment_key": key, **changes[key]} for key in sorted(changes)
    ]
    return {
        "file_id": new_batch.file_id,
        "old_revision_id": old_batch.revision_id if old_batch else None,
        "new_revision_id": new_batch.revision_id,
        "changed_sources": source_report,
        "recompute_segments": recompute,
        "invalidations": sorted(items, key=lambda item: item["segment_key"]),
        "edges": sorted([list(edge) for edge in edge_set]),
    }


def _coerce_batch(value: RevisionBatch | dict[str, Any] | None) -> RevisionBatch | None:
    if value is None or isinstance(value, RevisionBatch):
        return value
    segments = value.get("segments", {})
    normalized_segments = {
        str(key): str(item["content_digest"] if isinstance(item, dict) else item)
        for key, item in segments.items()
    }
    edges = frozenset(tuple(edge) for edge in value.get("edges", []))
    return RevisionBatch(
        file_id=str(value["file_id"]),
        revision_id=str(value.get("revision_id", "unknown")),
        segments=normalized_segments,
        edges=edges,
        parent_revision=value.get("parent_revision"),
    )


class RevisionStore:
    """Immutable batch registry; insertion order does not affect evaluation."""

    def __init__(self) -> None:
        self._batches: dict[tuple[str, str], RevisionBatch] = {}

    def add(self, batch: RevisionBatch | dict[str, Any]) -> "RevisionStore":
        normalized = _coerce_batch(batch)
        assert normalized is not None
        key = (normalized.file_id, normalized.revision_id)
        existing = self._batches.get(key)
        if existing is not None and existing != normalized:
            raise ValueError(f"conflicting immutable revision batch {key}")
        self._batches[key] = normalized
        return self

    def get(self, file_id: str, revision_id: str) -> RevisionBatch:
        return self._batches[(file_id, revision_id)]

    def evaluate(
        self,
        file_id: str,
        old_revision: str,
        new_revision: str,
    ) -> dict[str, Any]:
        return compute_invalidation(
            self.get(file_id, old_revision),
            self.get(file_id, new_revision),
        )
