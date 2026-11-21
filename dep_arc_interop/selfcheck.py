from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import SnapshotStore
from .equivalence import EquivalenceResult, judge_graph_equivalence
from .errors import BusinessError, ErrorCode, fail
from .formats import LoadedDocument, parse_format
from .migration import migrate
from .models import FormatVersion
from .source import SourceLike
from .validation import validate_graph


@dataclass
class SelfCheckResult:
    checks: list[dict[str, Any]] = field(default_factory=list)
    equivalence: EquivalenceResult | None = None
    replay_pairs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "consistent": True,
            "checks": self.checks,
            "replay_pairs": self.replay_pairs,
            "equivalence": (
                {
                    "equivalent": True,
                    "versions": list(self.equivalence.versions),
                    "sent_ids": self.equivalence.sent_ids,
                    "arc_count": self.equivalence.arc_count,
                    "reconstructed_field_count": len(
                        self.equivalence.reconstructed_fields
                    ),
                }
                if self.equivalence is not None
                else None
            ),
        }


def _inconsistent(
    category: str,
    message: str,
    *,
    sent_id: str | None,
    format_version: str,
    **details: Any,
) -> None:
    fail(
        ErrorCode.INCONSISTENT,
        message,
        category=category,
        sent_id=sent_id if sent_id is not None else "<global>",
        format_version=format_version,
        **details,
    )


def _validate_snapshot_trace(store: SnapshotStore) -> list[dict[str, Any]]:
    history = store.history
    if not history:
        _inconsistent(
            "SNAPSHOT_TRACE",
            "snapshot history is empty",
            sent_id=None,
            format_version="all",
        )
    previous_versions: dict[str, int] = {}
    checks: list[dict[str, Any]] = []
    for trace in history:
        sections = {
            "pos_tags": (trace.pos_version, trace.pos_checksum),
            "deprels": (trace.deprel_version, trace.deprel_checksum),
            "migration_policy": (trace.policy_version, trace.policy_checksum),
        }
        for name, (version, checksum) in sections.items():
            if not checksum:
                _inconsistent(
                    "SNAPSHOT_TRACE",
                    "snapshot trace is missing a content checksum",
                    sent_id=None,
                    format_version="all",
                    section=name,
                    sequence=trace.sequence,
                )
            old_version = previous_versions.get(name)
            if old_version is not None and version < old_version:
                _inconsistent(
                    "SNAPSHOT_TRACE",
                    "snapshot version history is not monotonic",
                    sent_id=None,
                    format_version="all",
                    section=name,
                    previous=old_version,
                    current=version,
                )
            previous_versions[name] = version
        checks.append(trace.to_dict())
    latest = history[-1]
    current = store.current
    if (
        current.pos_tags.version != latest.pos_version
        or current.pos_tags.checksum != latest.pos_checksum
        or current.deprels.version != latest.deprel_version
        or current.deprels.checksum != latest.deprel_checksum
        or current.policy.version != latest.policy_version
        or current.policy.checksum != latest.policy_checksum
    ):
        _inconsistent(
            "SNAPSHOT_TRACE",
            "current snapshot does not match the latest history entry",
            sent_id=None,
            format_version="all",
        )
    return checks


def run_self_check(
    sources: dict[str | FormatVersion, SourceLike | LoadedDocument],
    store: SnapshotStore,
    *,
    confidence_tolerance: float | None = None,
) -> SelfCheckResult:
    snapshot = store.current
    tolerance = (
        snapshot.policy.confidence_tolerance
        if confidence_tolerance is None
        else confidence_tolerance
    )
    loaded_by_version: dict[FormatVersion, LoadedDocument] = {}
    for raw_version, source in sources.items():
        version = FormatVersion.parse(raw_version)
        if isinstance(source, LoadedDocument):
            loaded = source
        else:
            loaded = parse_format(source, format_hint=version)
        validate_graph(loaded.graph, snapshot)
        if loaded.version != version:
            _inconsistent(
                "FORMAT_VIEW_ARCS",
                "declared self-check version does not match the loaded source",
                sent_id=next(iter(loaded.graph.sentences), None),
                format_version=version.value,
                loaded_version=loaded.version.value,
            )
        loaded_by_version[version] = loaded
    required_versions = {
        FormatVersion.V1,
        FormatVersion.V2,
        FormatVersion.V3,
    }
    missing_versions = required_versions - set(loaded_by_version)
    if missing_versions:
        _inconsistent(
            "FORMAT_VIEW_ARCS",
            "self-check requires v1, v2 and v3 views",
            sent_id=None,
            format_version=",".join(sorted(item.value for item in missing_versions)),
        )
    result = SelfCheckResult()
    trace_checks = _validate_snapshot_trace(store)
    result.checks.append(
        {
            "category": "SNAPSHOT_TRACE",
            "status": "pass",
            "history": trace_checks,
        }
    )
    try:
        equivalence = judge_graph_equivalence(
            {version: loaded.graph for version, loaded in loaded_by_version.items()},
            snapshot,
        )
    except BusinessError as exc:
        if exc.code is ErrorCode.SEMANTIC_DRIFT:
            drift = exc.details.get("drift") or []
            first = drift[0] if drift else {}
            _inconsistent(
                "FORMAT_VIEW_ARCS",
                "three-format arc views are not mutually consistent",
                sent_id=first.get("sent_id"),
                format_version="v1/v2/v3",
                drift=drift,
            )
        raise
    result.equivalence = equivalence
    result.checks.append(
        {
            "category": "FORMAT_VIEW_ARCS",
            "status": "pass",
            "sent_ids": equivalence.sent_ids,
            "arc_count": equivalence.arc_count,
        }
    )
    pair_count = 0
    for source_version, loaded in loaded_by_version.items():
        for target_version in FormatVersion:
            migration = migrate(
                loaded.graph,
                target_version,
                snapshot,
                source_version=source_version,
            )
            differences = migration.replay_differences(tolerance=tolerance)
            pair_count += 1
            if differences:
                first = differences[0]
                _inconsistent(
                    "AUDIT_REPLAY",
                    "migration audit replay does not reproduce the target arc graph",
                    sent_id=first.sent_id,
                    format_version=target_version.value,
                    source_version=source_version.value,
                    field=first.field,
                    expected=first.expected,
                    actual=first.actual,
                )
    result.replay_pairs = pair_count
    result.checks.append(
        {
            "category": "AUDIT_REPLAY",
            "status": "pass",
            "pairs": pair_count,
            "sent_ids": sorted(
                {
                    sent_id
                    for loaded in loaded_by_version.values()
                    for sent_id in loaded.graph.sentences
                }
            ),
        }
    )
    return result
