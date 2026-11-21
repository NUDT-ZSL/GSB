from __future__ import annotations

from collections.abc import Mapping

from .config import ControlledSnapshot, SnapshotStore
from .equivalence import EquivalenceResult, judge_graph_equivalence
from .errors import ErrorCode, fail
from .formats import LoadedDocument, parse_format
from .migration import MigrationResult
from .migration import migrate as _migrate
from .models import ArcGraph, FormatVersion
from .roundtrip import RoundtripResult
from .roundtrip import verify_roundtrip as _verify_roundtrip
from .selfcheck import SelfCheckResult
from .selfcheck import run_self_check as _run_self_check
from .source import SourceLike
from .validation import validate_graph


def load_format(
    source: SourceLike,
    snapshot: ControlledSnapshot,
    *,
    format_hint: str | FormatVersion | None = None,
    validate: bool = True,
) -> LoadedDocument:
    loaded = parse_format(source, format_hint=format_hint)
    if validate:
        validate_graph(loaded.graph, snapshot)
    return loaded


def migrate(
    source: ArcGraph | LoadedDocument | SourceLike,
    target_version: str | FormatVersion,
    snapshot: ControlledSnapshot,
    *,
    format_hint: str | FormatVersion | None = None,
) -> MigrationResult:
    if isinstance(source, LoadedDocument):
        graph = source.graph
        source_version: str | FormatVersion = source.version
    elif isinstance(source, ArcGraph):
        graph = source
        source_version = source.format_version or "v1"
    else:
        loaded = load_format(source, snapshot, format_hint=format_hint)
        graph = loaded.graph
        source_version = loaded.version
    return _migrate(
        graph,
        target_version,
        snapshot,
        source_version=source_version,
    )


def judge_equivalence(
    sources: Mapping[
        str | FormatVersion,
        ArcGraph | LoadedDocument | SourceLike,
    ],
    snapshot: ControlledSnapshot,
) -> EquivalenceResult:
    graphs: dict[FormatVersion, ArcGraph] = {}
    for raw_version, source in sources.items():
        version = FormatVersion.parse(raw_version)
        if isinstance(source, LoadedDocument):
            if source.version != version:
                fail(
                    ErrorCode.SEMANTIC_DRIFT,
                    "declared format version does not match the loaded source",
                    versions=[version.value],
                    unrestorable_fields=["format_version"],
                    tokens=[],
                    drift=[],
                    expected=version.value,
                    actual=source.version.value,
                )
            graph = source.graph
        elif isinstance(source, ArcGraph):
            graph = source
        else:
            graph = load_format(source, snapshot, format_hint=version).graph
        if graph.format_version not in (
            None,
            "raw-jsonl",
            version.value,
        ) and not isinstance(source, LoadedDocument):
            fail(
                ErrorCode.SEMANTIC_DRIFT,
                "declared format version does not match the graph metadata",
                versions=[version.value],
                unrestorable_fields=["format_version"],
                tokens=[],
                drift=[],
                expected=version.value,
                actual=str(graph.format_version),
            )
        validate_graph(graph, snapshot)
        graphs[version] = graph
    return judge_graph_equivalence(graphs, snapshot)


def verify_roundtrip(
    source: SourceLike,
    snapshot: ControlledSnapshot,
    *,
    format_hint: str | FormatVersion | None = None,
    confidence_tolerance: float | None = None,
) -> RoundtripResult:
    return _verify_roundtrip(
        source,
        snapshot,
        format_hint=format_hint,
        confidence_tolerance=confidence_tolerance,
    )


def run_self_check(
    sources: dict[str | FormatVersion, SourceLike | LoadedDocument],
    snapshot_store: SnapshotStore,
    *,
    confidence_tolerance: float | None = None,
) -> SelfCheckResult:
    return _run_self_check(
        sources,
        snapshot_store,
        confidence_tolerance=confidence_tolerance,
    )
