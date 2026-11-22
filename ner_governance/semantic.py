"""Cross-format semantic equivalence checks."""

from __future__ import annotations

from .config import GovernanceConfig
from .errors import E_SEMANTIC_DRIFT, fail
from .merge import MergeBundle, merge_annotations
from .models import AnnotationRecord, TypeSet, canonical_json


def _first_doc_difference(
    left: dict[str, dict],
    right: dict[str, dict],
) -> str | None:
    for doc_id in sorted(set(left) | set(right)):
        if canonical_json(left.get(doc_id)) != canonical_json(right.get(doc_id)):
            return doc_id
    return None


def compare_format_views(
    views: dict[str, TypeSet],
    records: list[AnnotationRecord],
    config: GovernanceConfig,
) -> dict:
    """Require identical semantics and identical merged annotations."""

    if len(views) < 2:
        fail(
            E_SEMANTIC_DRIFT,
            "at least two type-set views are required",
            field="views",
            reason="insufficient_views",
        )
    names = sorted(views)
    reference_name = names[0]
    reference = views[reference_name]
    reference_view = reference.semantic_view()
    fingerprints = {
        name: views[name].semantic_fingerprint() for name in names
    }
    for name in names[1:]:
        if views[name].semantic_view() != reference_view:
            fail(
                E_SEMANTIC_DRIFT,
                f"type-set view {name!r} differs from {reference_name!r}",
                field="type_set",
                reason="type_set_semantics_differ",
                reference_view=reference_name,
                differing_view=name,
                reference_fingerprint=fingerprints[reference_name],
                differing_fingerprint=fingerprints[name],
            )
    bundles: dict[str, MergeBundle] = {}
    for name in names:
        bundles[name] = merge_annotations(records, views[name], config)
    reference_annotations = bundles[reference_name].final_annotations()
    reference_digest = bundles[reference_name].digest
    for name in names[1:]:
        differing_doc = _first_doc_difference(
            reference_annotations,
            bundles[name].final_annotations(),
        )
        if differing_doc is not None:
            fail(
                E_SEMANTIC_DRIFT,
                f"merged annotations from view {name!r} differ",
                doc_id=differing_doc,
                reason="merged_annotations_differ",
                reference_view=reference_name,
                differing_view=name,
            )
        if bundles[name].digest != reference_digest:
            fail(
                E_SEMANTIC_DRIFT,
                f"vote trace from view {name!r} differs",
                field="merge_digest",
                reason="merge_trace_differ",
                reference_view=reference_name,
                differing_view=name,
                reference_digest=reference_digest,
                differing_digest=bundles[name].digest,
            )
    return {
        "ok": True,
        "code": "OK",
        "views": names,
        "fingerprints": fingerprints,
        "merge_digests": {name: bundles[name].digest for name in names},
        "checks": {
            "enabled_and_disabled_type_semantics_equal": True,
            "alias_maps_equal": True,
            "merged_annotations_equal": True,
            "vote_traces_equal": True,
        },
        "doc_count": len(reference_annotations),
        "token_position_count": sum(
            len(value["ner_tags"]) for value in reference_annotations.values()
        ),
    }
