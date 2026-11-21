from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .compare import Difference, compare_graphs
from .config import ControlledSnapshot
from .errors import ErrorCode, fail
from .models import ArcGraph, FormatVersion, SentenceGraph
from .validation import validate_graph


@dataclass(frozen=True)
class FillRecord:
    sent_id: str
    token_id: int | None
    field: str
    value: Any
    source: str
    reason: str
    policy_version: int
    policy_checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent_id": self.sent_id,
            "token_id": self.token_id,
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "policy_checksum": self.policy_checksum,
        }


@dataclass(frozen=True)
class DropRecord:
    sent_id: str
    token_id: int | None
    field: str
    value: Any
    reason: str
    policy_rule: str
    policy_version: int
    policy_checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent_id": self.sent_id,
            "token_id": self.token_id,
            "field": self.field,
            "value": self.value,
            "reason": self.reason,
            "policy_rule": self.policy_rule,
            "policy_version": self.policy_version,
            "policy_checksum": self.policy_checksum,
        }


@dataclass
class MigrationResult:
    source_version: FormatVersion
    target_version: FormatVersion
    source_graph: ArcGraph
    graph: ArcGraph
    fills: list[FillRecord] = field(default_factory=list)
    drops: list[DropRecord] = field(default_factory=list)
    policy_version: int = 0
    policy_checksum: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version.value,
            "target_version": self.target_version.value,
            "sent_ids": self.graph.sent_ids(),
            "fill_count": len(self.fills),
            "drop_count": len(self.drops),
            "fills": [record.to_dict() for record in self.fills],
            "drops": [record.to_dict() for record in self.drops],
            "policy_version": self.policy_version,
            "policy_checksum": self.policy_checksum,
        }

    def replay(self) -> ArcGraph:
        replayed = self.source_graph.clone()
        replayed.format_version = self.target_version
        for record in self.drops:
            _apply_drop(replayed, record)
        for record in self.fills:
            _apply_fill(replayed, record)
        return replayed

    def replay_differences(self, tolerance: float = 1e-6) -> list[Difference]:
        return compare_graphs(
            self.graph,
            self.replay(),
            version=self.target_version,
            confidence_tolerance=tolerance,
            include_v3_extras=True,
            full_internal=True,
        )


def _source_version(
    graph: ArcGraph, override: str | FormatVersion | None
) -> FormatVersion:
    raw = override if override is not None else graph.format_version
    if raw in (None, "raw-jsonl"):
        return FormatVersion.V1
    return FormatVersion.parse(raw)


def _root_for_upgrade(sentence: SentenceGraph) -> int:
    roots = sentence.root_arc_ids()
    if len(roots) != 1:
        fail(
            ErrorCode.ROOT_VIOLATION,
            "cannot infer an explicit root marker for migration",
            field="root",
            sent_id=sentence.sent_id,
            token_id=roots[0] if len(roots) == 1 else None,
            root_token_ids=roots,
            expected_root_count=1,
            actual_root_count=len(roots),
        )
    return roots[0]


def _fill(
    records: list[FillRecord],
    *,
    sent_id: str,
    token_id: int | None,
    field_name: str,
    value: Any,
    source: str,
    reason: str,
    snapshot: ControlledSnapshot,
) -> None:
    records.append(
        FillRecord(
            sent_id=sent_id,
            token_id=token_id,
            field=field_name,
            value=value,
            source=source,
            reason=reason,
            policy_version=snapshot.policy.version,
            policy_checksum=snapshot.policy.checksum,
        )
    )


def _drop(
    records: list[DropRecord],
    *,
    sent_id: str,
    token_id: int | None,
    field_name: str,
    value: Any,
    target: FormatVersion,
    reason: str,
    snapshot: ControlledSnapshot,
) -> None:
    declared = snapshot.policy.discard_fields_for(target.value)
    if field_name not in declared:
        fail(
            ErrorCode.INCONSISTENT,
            "migration policy does not declare a discard rule for this field",
            category="DISCARD_RULE_MISSING",
            sent_id=sent_id,
            token_id=token_id,
            format_version=target.value,
            field=field_name,
            policy_version=snapshot.policy.version,
        )
    records.append(
        DropRecord(
            sent_id=sent_id,
            token_id=token_id,
            field=field_name,
            value=value,
            reason=reason,
            policy_rule=f"downgrade_to_{target.value}",
            policy_version=snapshot.policy.version,
            policy_checksum=snapshot.policy.checksum,
        )
    )


def _apply_fill(graph: ArcGraph, record: FillRecord) -> None:
    sentence = graph.sentences[record.sent_id]
    if record.field == "root_marker":
        sentence.root_marker = int(record.value)
        sentence.root_source = record.source
        return
    if record.field == "bidirectional_arc_chains":
        sentence.bidirectional_chains = []
        return
    if record.field == "cross_sentence_refs":
        sentence.cross_sentence_refs = []
        return
    if record.token_id is not None:
        arc = sentence.arcs[record.token_id]
        if record.field == "confidence_low":
            arc.confidence_low = float(record.value)
        elif record.field == "confidence_high":
            arc.confidence_high = float(record.value)
        elif record.field == "forward_check":
            arc.forward_check = bool(record.value)
        elif record.field == "reverse_check":
            arc.reverse_check = bool(record.value)
        arc.field_sources[record.field] = record.source


def _apply_drop(graph: ArcGraph, record: DropRecord) -> None:
    sentence = graph.sentences[record.sent_id]
    if record.field == "root_marker":
        sentence.root_marker = None
        sentence.root_source = "inferred"
        return
    if record.field == "bidirectional_arc_chains":
        sentence.bidirectional_chains = []
        return
    if record.field == "cross_sentence_refs":
        sentence.cross_sentence_refs = []
        return
    if record.token_id is not None:
        arc = sentence.arcs[record.token_id]
        if record.field == "confidence_low":
            arc.confidence_low = None
        elif record.field == "confidence_high":
            arc.confidence_high = None
        elif record.field == "forward_check":
            arc.forward_check = None
        elif record.field == "reverse_check":
            arc.reverse_check = None
        arc.field_sources.pop(record.field, None)


def migrate(
    graph: ArcGraph,
    target_version: str | FormatVersion,
    snapshot: ControlledSnapshot,
    *,
    source_version: str | FormatVersion | None = None,
) -> MigrationResult:
    source = _source_version(graph, source_version)
    target = FormatVersion.parse(target_version)
    validate_graph(graph, snapshot)
    migrated = graph.clone()
    migrated.format_version = target
    fills: list[FillRecord] = []
    drops: list[DropRecord] = []
    policy = snapshot.policy
    for sent_id, sentence in migrated.sentences.items():
        original_root = sentence.root_marker
        if target is FormatVersion.V1:
            if original_root is not None:
                _drop(
                    drops,
                    sent_id=sent_id,
                    token_id=original_root,
                    field_name="root_marker",
                    value=original_root,
                    target=target,
                    reason="v1 infers the root from the arc whose head is 0",
                    snapshot=snapshot,
                )
                sentence.root_marker = None
                sentence.root_source = "inferred"
            for arc in sentence.ordered_arcs():
                if arc.confidence_low is not None:
                    _drop(
                        drops,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="confidence_low",
                        value=arc.confidence_low,
                        target=target,
                        reason="v1 has no confidence interval field",
                        snapshot=snapshot,
                    )
                    arc.confidence_low = None
                if arc.confidence_high is not None:
                    _drop(
                        drops,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="confidence_high",
                        value=arc.confidence_high,
                        target=target,
                        reason="v1 has no confidence interval field",
                        snapshot=snapshot,
                    )
                    arc.confidence_high = None
                if arc.forward_check is not None:
                    _drop(
                        drops,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="forward_check",
                        value=arc.forward_check,
                        target=target,
                        reason="v1 has no dependency-direction validation field",
                        snapshot=snapshot,
                    )
                    arc.forward_check = None
                if arc.reverse_check is not None:
                    _drop(
                        drops,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="reverse_check",
                        value=arc.reverse_check,
                        target=target,
                        reason="v1 has no dependency-direction validation field",
                        snapshot=snapshot,
                    )
                    arc.reverse_check = None
            if sentence.bidirectional_chains:
                _drop(
                    drops,
                    sent_id=sent_id,
                    token_id=None,
                    field_name="bidirectional_arc_chains",
                    value=[chain.to_dict() for chain in sentence.bidirectional_chains],
                    target=target,
                    reason="v1 cannot express bidirectional arc chain declarations",
                    snapshot=snapshot,
                )
                sentence.bidirectional_chains = []
            if sentence.cross_sentence_refs:
                _drop(
                    drops,
                    sent_id=sent_id,
                    token_id=None,
                    field_name="cross_sentence_refs",
                    value=[ref.to_dict() for ref in sentence.cross_sentence_refs],
                    target=target,
                    reason="v1 cannot express cross-sentence references",
                    snapshot=snapshot,
                )
                sentence.cross_sentence_refs = []
        elif target is FormatVersion.V2:
            if sentence.root_marker is None:
                root = _root_for_upgrade(sentence)
                _fill(
                    fills,
                    sent_id=sent_id,
                    token_id=root,
                    field_name="root_marker",
                    value=root,
                    source="inferred:head==0",
                    reason="v2 requires an explicit sentence root marker",
                    snapshot=snapshot,
                )
                sentence.root_marker = root
                sentence.root_source = "inferred:head==0"
            for arc in sentence.ordered_arcs():
                if arc.confidence_low is None or arc.confidence_high is None:
                    if (
                        arc.confidence_low is not None
                        or arc.confidence_high is not None
                    ):
                        fail(
                            ErrorCode.INCONSISTENT,
                            "arc has a partial confidence interval",
                            category="PARTIAL_CONFIDENCE",
                            sent_id=sent_id,
                            token_id=arc.token_id,
                            format_version=source.value,
                            field="confidence",
                        )
                    _fill(
                        fills,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="confidence_low",
                        value=policy.confidence_low,
                        source="policy_default",
                        reason="v1 carries no confidence interval",
                        snapshot=snapshot,
                    )
                    _fill(
                        fills,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="confidence_high",
                        value=policy.confidence_high,
                        source="policy_default",
                        reason="v1 carries no confidence interval",
                        snapshot=snapshot,
                    )
                    arc.confidence_low = policy.confidence_low
                    arc.confidence_high = policy.confidence_high
                    arc.field_sources["confidence_low"] = "policy_default"
                    arc.field_sources["confidence_high"] = "policy_default"
                if arc.forward_check is not None:
                    _drop(
                        drops,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="forward_check",
                        value=arc.forward_check,
                        target=target,
                        reason="v2 has no dependency-direction validation field",
                        snapshot=snapshot,
                    )
                    arc.forward_check = None
                if arc.reverse_check is not None:
                    _drop(
                        drops,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="reverse_check",
                        value=arc.reverse_check,
                        target=target,
                        reason="v2 has no dependency-direction validation field",
                        snapshot=snapshot,
                    )
                    arc.reverse_check = None
            if sentence.bidirectional_chains:
                _drop(
                    drops,
                    sent_id=sent_id,
                    token_id=None,
                    field_name="bidirectional_arc_chains",
                    value=[chain.to_dict() for chain in sentence.bidirectional_chains],
                    target=target,
                    reason="v2 cannot express bidirectional arc chain declarations",
                    snapshot=snapshot,
                )
                sentence.bidirectional_chains = []
            if sentence.cross_sentence_refs:
                _drop(
                    drops,
                    sent_id=sent_id,
                    token_id=None,
                    field_name="cross_sentence_refs",
                    value=[ref.to_dict() for ref in sentence.cross_sentence_refs],
                    target=target,
                    reason="v2 cannot express cross-sentence references",
                    snapshot=snapshot,
                )
                sentence.cross_sentence_refs = []
        else:
            if sentence.root_marker is None:
                root = _root_for_upgrade(sentence)
                _fill(
                    fills,
                    sent_id=sent_id,
                    token_id=root,
                    field_name="root_marker",
                    value=root,
                    source="inferred:head==0",
                    reason="v3 requires an explicit sentence root marker",
                    snapshot=snapshot,
                )
                sentence.root_marker = root
                sentence.root_source = "inferred:head==0"
            for arc in sentence.ordered_arcs():
                if arc.confidence_low is None or arc.confidence_high is None:
                    if (
                        arc.confidence_low is not None
                        or arc.confidence_high is not None
                    ):
                        fail(
                            ErrorCode.INCONSISTENT,
                            "arc has a partial confidence interval",
                            category="PARTIAL_CONFIDENCE",
                            sent_id=sent_id,
                            token_id=arc.token_id,
                            format_version=source.value,
                            field="confidence",
                        )
                    _fill(
                        fills,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="confidence_low",
                        value=policy.confidence_low,
                        source="policy_default",
                        reason="source format carries no confidence interval",
                        snapshot=snapshot,
                    )
                    _fill(
                        fills,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="confidence_high",
                        value=policy.confidence_high,
                        source="policy_default",
                        reason="source format carries no confidence interval",
                        snapshot=snapshot,
                    )
                    arc.confidence_low = policy.confidence_low
                    arc.confidence_high = policy.confidence_high
                    arc.field_sources["confidence_low"] = "policy_default"
                    arc.field_sources["confidence_high"] = "policy_default"
                if arc.forward_check is None:
                    _fill(
                        fills,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="forward_check",
                        value=policy.forward_check,
                        source="policy_default",
                        reason="source format has no forward dependency check marker",
                        snapshot=snapshot,
                    )
                    arc.forward_check = policy.forward_check
                    arc.field_sources["forward_check"] = "policy_default"
                if arc.reverse_check is None:
                    _fill(
                        fills,
                        sent_id=sent_id,
                        token_id=arc.token_id,
                        field_name="reverse_check",
                        value=policy.reverse_check,
                        source="policy_default",
                        reason="source format has no reverse dependency check marker",
                        snapshot=snapshot,
                    )
                    arc.reverse_check = policy.reverse_check
                    arc.field_sources["reverse_check"] = "policy_default"
            if source is not FormatVersion.V3:
                _fill(
                    fills,
                    sent_id=sent_id,
                    token_id=None,
                    field_name="bidirectional_arc_chains",
                    value=[],
                    source="format_absence_empty",
                    reason="source format has no bidirectional chain field",
                    snapshot=snapshot,
                )
                _fill(
                    fills,
                    sent_id=sent_id,
                    token_id=None,
                    field_name="cross_sentence_refs",
                    value=[],
                    source="format_absence_empty",
                    reason="source format has no cross-sentence reference field",
                    snapshot=snapshot,
                )
    if migrated.core_signature() != graph.core_signature():
        fail(
            ErrorCode.INCONSISTENT,
            "migration changed token-to-head arc references",
            category="ARC_REFERENCE_CHANGED",
            sent_id=next(iter(graph.sentences), None),
            format_version=f"{source.value}->{target.value}",
            field="head",
        )
    validate_graph(migrated, snapshot)
    return MigrationResult(
        source_version=source,
        target_version=target,
        source_graph=graph,
        graph=migrated,
        fills=fills,
        drops=drops,
        policy_version=policy.version,
        policy_checksum=policy.checksum,
    )


def migrate_source(
    source,
    target_version: str | FormatVersion,
    snapshot: ControlledSnapshot,
    *,
    format_hint: str | FormatVersion | None = None,
) -> MigrationResult:
    from .formats import parse_format

    loaded = parse_format(source, format_hint=format_hint)
    return migrate(
        loaded.graph,
        target_version,
        snapshot,
        source_version=loaded.version,
    )
