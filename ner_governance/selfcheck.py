"""Cross-module consistency self-check."""

from __future__ import annotations

from typing import Any

from .annotations import (
    group_by_doc,
    validate_document_sets,
    validate_documents,
)
from .config import GovernanceConfig
from .errors import E_INCONSISTENT, E_TRACE_BROKEN, GovernanceError, fail
from .merge import merge_annotations
from .models import AnnotationRecord, TypeSet, canonical_json
from .trace import trace_label


def _first_doc_difference(
    left: dict[str, dict],
    right: dict[str, dict],
) -> str | None:
    for doc_id in sorted(set(left) | set(right)):
        if canonical_json(left.get(doc_id)) != canonical_json(right.get(doc_id)):
            return doc_id
    return None


def run_self_check(
    records: list[AnnotationRecord],
    type_sets: dict[str, TypeSet],
    config: GovernanceConfig,
) -> dict[str, Any]:
    if not type_sets:
        fail(
            E_INCONSISTENT,
            "self-check requires at least one type-set view",
            reason="no_type_set_views",
        )
    try:
        validate_document_sets(records)
        grouped = group_by_doc(records)
        validate_documents(grouped)
    except GovernanceError as exc:
        if exc.code == E_INCONSISTENT:
            raise
        fail(
            E_INCONSISTENT,
            "annotation invariants failed",
            doc_id=exc.details.get("doc_id"),
            reason="annotation_invariant_failed",
            cause=exc.to_dict(),
        )
    names = sorted(type_sets)
    reference_name = names[0]
    reference = type_sets[reference_name]
    reference_view = reference.semantic_view()
    for name in names[1:]:
        if type_sets[name].semantic_view() != reference_view:
            doc_id = sorted(grouped)[0]
            fail(
                E_INCONSISTENT,
                "type-set views are not semantically equal",
                doc_id=doc_id,
                reason="type_set_view_mismatch",
                reference_view=reference_name,
                differing_view=name,
            )
    bundles = {
        name: merge_annotations(records, type_sets[name], config)
        for name in names
    }
    reference_bundle = bundles[reference_name]
    reference_annotations = reference_bundle.final_annotations()
    for name in names[1:]:
        differing_doc = _first_doc_difference(
            reference_annotations,
            bundles[name].final_annotations(),
        )
        if differing_doc is not None:
            fail(
                E_INCONSISTENT,
                "merged conclusions differ between type-set views",
                doc_id=differing_doc,
                reason="cross_view_merge_mismatch",
                reference_view=reference_name,
                differing_view=name,
            )
        if bundles[name].digest != reference_bundle.digest:
            doc_id = sorted(grouped)[0]
            fail(
                E_INCONSISTENT,
                "merge traces differ between type-set views",
                doc_id=doc_id,
                reason="cross_view_trace_mismatch",
                reference_view=reference_name,
                differing_view=name,
            )
    for doc in reference_bundle.docs:
        if len(doc["tokens"]) != len(doc["ner_tags"]):
            fail(
                E_INCONSISTENT,
                "merged document does not have exactly one tag per token",
                doc_id=doc["doc_id"],
                reason="tag_count_mismatch",
                token_count=len(doc["tokens"]),
                tag_count=len(doc["ner_tags"]),
            )
        if [record["position"] for record in doc["vote_records"]] != list(
            range(len(doc["ner_tags"]))
        ):
            fail(
                E_INCONSISTENT,
                "merged document vote positions are not exactly one per token",
                doc_id=doc["doc_id"],
                reason="vote_position_mismatch",
            )
        for position in range(len(doc["ner_tags"])):
            try:
                trace_label(
                    reference_bundle,
                    records,
                    reference,
                    config,
                    doc["doc_id"],
                    position,
                )
            except GovernanceError as exc:
                if exc.code == E_TRACE_BROKEN:
                    fail(
                        E_INCONSISTENT,
                        "trace replay failed during self-check",
                        doc_id=doc["doc_id"],
                        position=position,
                        reason="trace_replay_mismatch",
                        cause=exc.to_dict(),
                    )
                raise
    return {
        "ok": True,
        "code": "OK",
        "checks": {
            "annotator_doc_id_sets_equal": True,
            "one_final_label_per_token": True,
            "vote_statistics_independently_recomputed": True,
            "all_traces_replay": True,
            "three_format_views_semantically_equal": True,
            "three_format_merge_conclusions_equal": True,
        },
        "views": names,
        "doc_count": len(reference_bundle.docs),
        "token_position_count": sum(
            len(doc["ner_tags"]) for doc in reference_bundle.docs
        ),
        "merge_digests": {name: bundles[name].digest for name in names},
        "type_set_fingerprints": {
            name: type_sets[name].semantic_fingerprint() for name in names
        },
    }
