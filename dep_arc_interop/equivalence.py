from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import ControlledSnapshot
from .errors import ErrorCode, fail
from .models import ArcGraph, FormatVersion


@dataclass(frozen=True)
class DriftItem:
    sent_id: str
    token_id: int | None
    field: str
    versions: tuple[str, ...]
    reason: str
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent_id": self.sent_id,
            "token_id": self.token_id,
            "field": self.field,
            "versions": list(self.versions),
            "reason": self.reason,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class EquivalenceResult:
    versions: tuple[str, ...]
    sent_ids: list[str]
    arc_count: int
    reconstructed_fields: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "equivalent": True,
            "versions": list(self.versions),
            "sent_ids": self.sent_ids,
            "arc_count": self.arc_count,
            "reconstructed_fields": self.reconstructed_fields,
        }


def _float_equal(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= tolerance


def judge_graph_equivalence(
    graphs: Mapping[str | FormatVersion, ArcGraph],
    snapshot: ControlledSnapshot,
) -> EquivalenceResult:
    if not graphs:
        fail(
            ErrorCode.SEMANTIC_DRIFT,
            "no format views were supplied for semantic equivalence",
            unrestorable_fields=[],
            tokens=[],
            drift=[],
        )
    normalized: dict[FormatVersion, ArcGraph] = {}
    for raw_version, graph in graphs.items():
        version = FormatVersion.parse(raw_version)
        normalized[version] = graph
    ordered_versions = tuple(
        version for version in FormatVersion if version in normalized
    )
    if not ordered_versions:
        fail(
            ErrorCode.SEMANTIC_DRIFT,
            "no supported format views were supplied",
            unrestorable_fields=[],
            tokens=[],
            drift=[],
        )
    baseline_version = ordered_versions[0]
    version_labels = tuple(version.value for version in ordered_versions)
    drift: list[DriftItem] = []
    reconstructed: list[dict[str, Any]] = []
    all_sent_ids: set[str] = set()
    for graph in normalized.values():
        all_sent_ids.update(graph.sentences)
    for sent_id in sorted(all_sent_ids):
        missing_versions = tuple(
            version.value
            for version, graph in normalized.items()
            if sent_id not in graph.sentences
        )
        if missing_versions:
            present_versions = tuple(
                version.value
                for version, graph in normalized.items()
                if sent_id in graph.sentences
            )
            drift.append(
                DriftItem(
                    sent_id=sent_id,
                    token_id=None,
                    field="sent_id",
                    versions=missing_versions,
                    reason="sentence is absent from one or more format views",
                    expected=list(present_versions),
                    actual=list(missing_versions),
                )
            )
            continue
        sentence_views = {
            version: graph.sentences[sent_id] for version, graph in normalized.items()
        }
        token_ids: set[int] = set()
        for sentence in sentence_views.values():
            token_ids.update(sentence.arcs)
        for token_id in sorted(token_ids):
            token_views = {
                version: sentence.arcs.get(token_id)
                for version, sentence in sentence_views.items()
            }
            missing_token_versions = tuple(
                version.value for version, arc in token_views.items() if arc is None
            )
            if missing_token_versions:
                drift.append(
                    DriftItem(
                        sent_id=sent_id,
                        token_id=token_id,
                        field="token_id",
                        versions=missing_token_versions,
                        reason="token arc is absent from one or more format views",
                    )
                )
                continue
            arcs = {
                version: arc for version, arc in token_views.items() if arc is not None
            }
            for field_name in ("head", "deprel"):
                values = {
                    version: getattr(arc, field_name) for version, arc in arcs.items()
                }
                first = values[baseline_version]
                changed_versions = tuple(
                    version.value for version, value in values.items() if value != first
                )
                if changed_versions:
                    drift.append(
                        DriftItem(
                            sent_id=sent_id,
                            token_id=token_id,
                            field=field_name,
                            versions=changed_versions,
                            reason="core arc semantics differ across format views",
                            expected=first,
                            actual={
                                version.value: values[version]
                                for version in ordered_versions
                            },
                        )
                    )
            observed_confidences: dict[str, list[tuple[str, float]]] = {
                "confidence_low": [],
                "confidence_high": [],
            }
            for field_name in ("confidence_low", "confidence_high"):
                for version, arc in arcs.items():
                    value = getattr(arc, field_name)
                    if value is not None:
                        observed_confidences[field_name].append((version.value, value))
                values = observed_confidences[field_name]
                if not values:
                    continue
                first_value = values[0][1]
                if any(
                    not _float_equal(
                        first_value, value, snapshot.policy.confidence_tolerance
                    )
                    for _, value in values[1:]
                ):
                    drift.append(
                        DriftItem(
                            sent_id=sent_id,
                            token_id=token_id,
                            field=field_name,
                            versions=tuple(version for version, _ in values),
                            reason="confidence intervals differ across format views",
                            expected=first_value,
                            actual={version: value for version, value in values},
                        )
                    )
                default_value = getattr(snapshot.policy, field_name)
                missing_versions = tuple(
                    version.value
                    for version, arc in arcs.items()
                    if getattr(arc, field_name) is None
                )
                if missing_versions:
                    if _float_equal(
                        first_value,
                        default_value,
                        snapshot.policy.confidence_tolerance,
                    ):
                        reconstructed.append(
                            {
                                "sent_id": sent_id,
                                "token_id": token_id,
                                "field": field_name,
                                "value": default_value,
                                "source": "policy_default",
                                "missing_versions": list(missing_versions),
                            }
                        )
                    else:
                        drift.append(
                            DriftItem(
                                sent_id=sent_id,
                                token_id=token_id,
                                field=field_name,
                                versions=missing_versions,
                                reason=(
                                    "v1 cannot restore a non-default confidence value"
                                ),
                                expected=first_value,
                                actual=None,
                            )
                        )
            for field_name in ("forward_check", "reverse_check"):
                observed = [
                    (version.value, getattr(arc, field_name))
                    for version, arc in arcs.items()
                    if getattr(arc, field_name) is not None
                ]
                if observed and len({value for _, value in observed}) > 1:
                    drift.append(
                        DriftItem(
                            sent_id=sent_id,
                            token_id=token_id,
                            field=field_name,
                            versions=tuple(version for version, _ in observed),
                            reason="dependency check markers differ across format views",
                            expected=observed[0][1],
                            actual={version: value for version, value in observed},
                        )
                    )
                missing_versions = tuple(
                    version.value
                    for version, arc in arcs.items()
                    if getattr(arc, field_name) is None
                )
                if observed and missing_versions:
                    expected_check = (
                        snapshot.policy.forward_check
                        if field_name == "forward_check"
                        else snapshot.policy.reverse_check
                    )
                    if any(value is not expected_check for _, value in observed):
                        drift.append(
                            DriftItem(
                                sent_id=sent_id,
                                token_id=token_id,
                                field=field_name,
                                versions=missing_versions,
                                reason=(
                                    "a non-default dependency check cannot be restored "
                                    "by formats without the marker"
                                ),
                                expected={
                                    version: value for version, value in observed
                                },
                                actual=None,
                            )
                        )
                    else:
                        reconstructed.append(
                            {
                                "sent_id": sent_id,
                                "token_id": token_id,
                                "field": field_name,
                                "value": (
                                    snapshot.policy.forward_check
                                    if field_name == "forward_check"
                                    else snapshot.policy.reverse_check
                                ),
                                "source": "policy_default",
                                "missing_versions": list(missing_versions),
                            }
                        )
        roots: dict[str, int | None] = {}
        for version, sentence in sentence_views.items():
            roots[version.value] = sentence.effective_root()
        root_values = set(roots.values())
        if len(root_values) > 1:
            drift.append(
                DriftItem(
                    sent_id=sent_id,
                    token_id=None,
                    field="root",
                    versions=tuple(version.value for version in ordered_versions),
                    reason="effective root token differs across format views",
                    expected={
                        version.value: roots[version.value]
                        for version in ordered_versions
                    },
                    actual=None,
                )
            )
        if any(
            sentence.root_marker is None for sentence in sentence_views.values()
        ) and any(
            sentence.root_marker is not None for sentence in sentence_views.values()
        ):
            reconstructed.append(
                {
                    "sent_id": sent_id,
                    "token_id": roots.get("v1"),
                    "field": "root_marker",
                    "value": roots.get("v1"),
                    "source": "inferred:head==0",
                    "missing_versions": [
                        version.value
                        for version, sentence in sentence_views.items()
                        if sentence.root_marker is None
                    ],
                }
            )
        for version, sentence in sentence_views.items():
            if sentence.bidirectional_chains:
                drift.append(
                    DriftItem(
                        sent_id=sent_id,
                        token_id=sentence.bidirectional_chains[0].token_ids[0],
                        field="bidirectional_arc_chains",
                        versions=(version.value,),
                        reason=(
                            "bidirectional arc chains exist only in the v3 view "
                            "and cannot be reconstructed from v1/v2"
                        ),
                        expected=[
                            chain.to_dict() for chain in sentence.bidirectional_chains
                        ],
                        actual=None,
                    )
                )
            if sentence.cross_sentence_refs:
                drift.append(
                    DriftItem(
                        sent_id=sent_id,
                        token_id=sentence.cross_sentence_refs[0].source_token_id,
                        field="cross_sentence_refs",
                        versions=(version.value,),
                        reason=(
                            "cross-sentence references exist only in the v3 view "
                            "and cannot be reconstructed from v1/v2"
                        ),
                        expected=[
                            ref.to_dict() for ref in sentence.cross_sentence_refs
                        ],
                        actual=None,
                    )
                )
    if drift:
        unrestorable_fields = sorted({item.field for item in drift})
        first_drift = drift[0]
        tokens = sorted(
            {
                f"{item.sent_id}#{item.token_id}"
                for item in drift
                if item.token_id is not None
            }
        )
        fail(
            ErrorCode.SEMANTIC_DRIFT,
            "format views are not semantically equivalent",
            sent_id=first_drift.sent_id,
            token_id=first_drift.token_id,
            field=first_drift.field,
            versions=list(version_labels),
            unrestorable_fields=unrestorable_fields,
            tokens=tokens,
            drift=[item.to_dict() for item in drift],
        )
    arc_count = sum(
        len(graph.sentences[sent_id].arcs)
        for graph in normalized.values()
        for sent_id in graph.sentences
    ) // max(len(normalized), 1)
    return EquivalenceResult(
        versions=version_labels,
        sent_ids=sorted(all_sent_ids),
        arc_count=arc_count,
        reconstructed_fields=reconstructed,
    )
