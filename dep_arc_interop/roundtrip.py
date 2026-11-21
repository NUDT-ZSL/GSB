from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .compare import Difference, compare_graphs
from .config import ControlledSnapshot
from .errors import ErrorCode, fail
from .formats import parse_format, serialize_graph
from .models import FormatVersion as ModelFormatVersion
from .source import SourceLike
from .validation import validate_graph


@dataclass
class RoundtripResult:
    version: ModelFormatVersion
    sent_ids: list[str]
    tolerance: float
    serialized_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "consistent": True,
            "format_version": self.version.value,
            "sent_ids": self.sent_ids,
            "confidence_tolerance": self.tolerance,
            "serialized_bytes": self.serialized_bytes,
        }


def verify_roundtrip(
    source: SourceLike,
    snapshot: ControlledSnapshot,
    *,
    format_hint: str | ModelFormatVersion | None = None,
    confidence_tolerance: float | None = None,
) -> RoundtripResult:
    loaded = parse_format(source, format_hint=format_hint)
    validate_graph(loaded.graph, snapshot)
    tolerance = (
        snapshot.policy.confidence_tolerance
        if confidence_tolerance is None
        else float(confidence_tolerance)
    )
    if tolerance <= 0:
        fail(
            ErrorCode.ROUNDTRIP_MISMATCH,
            "confidence tolerance must be positive",
            format_version=loaded.version.value,
            sent_id=None,
            token_id=None,
            field="confidence_tolerance",
            expected="> 0",
            actual=tolerance,
        )
    serialized = serialize_graph(loaded.graph, version=loaded.version)
    reparsed = parse_format(serialized, format_hint=loaded.version)
    validate_graph(reparsed.graph, snapshot)
    differences: list[Difference] = compare_graphs(
        loaded.graph,
        reparsed.graph,
        version=loaded.version,
        confidence_tolerance=tolerance,
        include_v3_extras=True,
    )
    if differences:
        first = differences[0]
        fail(
            ErrorCode.ROUNDTRIP_MISMATCH,
            "format roundtrip is not arc-consistent",
            format_version=loaded.version.value,
            sent_id=first.sent_id,
            token_id=first.token_id,
            field=first.field,
            expected=first.expected,
            actual=first.actual,
            difference_count=len(differences),
            differences=[item.to_dict() for item in differences],
        )
    return RoundtripResult(
        version=loaded.version,
        sent_ids=loaded.graph.sent_ids(),
        tolerance=tolerance,
        serialized_bytes=len(serialized.encode("utf-8")),
    )
