"""Independent replay of a merged token from raw annotator votes."""

from __future__ import annotations

from typing import Any

from .annotations import group_by_doc
from .config import GovernanceConfig
from .errors import E_TRACE_BROKEN, fail
from .merge import (
    MergeBundle,
    _check_alias_config,
    _select_majority,
    _select_priority,
    normalize_tag,
)
from .models import AnnotationRecord, TypeSet, canonical_json, sha256_value


def _find_doc(bundle: MergeBundle, doc_id: str) -> dict[str, Any]:
    for doc in bundle.docs:
        if doc["doc_id"] == doc_id:
            return doc
    fail(
        E_TRACE_BROKEN,
        f"merged result has no document {doc_id!r}",
        doc_id=doc_id,
        reason="doc_missing",
    )
    raise AssertionError("unreachable")


def _trace_context_guard(
    bundle: MergeBundle,
    type_set: TypeSet,
    config: GovernanceConfig,
    *,
    doc_id: str,
    position: int,
) -> None:
    if type_set.label_set_version != bundle.label_set_version:
        fail(
            E_TRACE_BROKEN,
            "type-set version does not match the merge trace",
            doc_id=doc_id,
            position=position,
            reason="label_set_version_mismatch",
            expected=bundle.label_set_version,
            actual=type_set.label_set_version,
        )
    if type_set.semantic_fingerprint() != bundle.type_set_fingerprint:
        fail(
            E_TRACE_BROKEN,
            "type-set semantics do not match the merge trace",
            doc_id=doc_id,
            position=position,
            reason="type_set_fingerprint_mismatch",
            expected=bundle.type_set_fingerprint,
            actual=type_set.semantic_fingerprint(),
        )
    if config.digest() != bundle.config_digest:
        fail(
            E_TRACE_BROKEN,
            "config does not match the merge trace",
            doc_id=doc_id,
            position=position,
            reason="config_digest_mismatch",
            expected=bundle.config_digest,
            actual=config.digest(),
        )


def trace_label(
    bundle: MergeBundle,
    records: list[AnnotationRecord],
    type_set: TypeSet,
    config: GovernanceConfig,
    doc_id: str,
    position: int,
) -> dict[str, Any]:
    """Recompute one token using raw votes and compare it with the trace."""

    _trace_context_guard(
        bundle,
        type_set,
        config,
        doc_id=doc_id,
        position=position,
    )
    doc = _find_doc(bundle, doc_id)
    if position < 0 or position >= len(doc["ner_tags"]):
        fail(
            E_TRACE_BROKEN,
            "token position is outside the merged document",
            doc_id=doc_id,
            position=position,
            reason="position_out_of_range",
            token_count=len(doc["ner_tags"]),
        )
    trace_record = next(
        (
            item
            for item in doc["vote_records"]
            if item["position"] == position
        ),
        None,
    )
    if trace_record is None:
        fail(
            E_TRACE_BROKEN,
            "vote record is missing for the requested token position",
            doc_id=doc_id,
            position=position,
            reason="vote_record_missing",
        )
    if trace_record.get("label_set_version") != type_set.label_set_version:
        fail(
            E_TRACE_BROKEN,
            "vote record label_set_version does not match the type set",
            doc_id=doc_id,
            position=position,
            reason="trace_label_set_version_mismatch",
            expected=type_set.label_set_version,
            actual=trace_record.get("label_set_version"),
        )

    grouped = group_by_doc(records)
    source_doc = grouped.get(doc_id)
    if not source_doc:
        fail(
            E_TRACE_BROKEN,
            "source annotations are missing for the document",
            doc_id=doc_id,
            position=position,
            reason="source_annotations_missing",
        )
    source_by_annotator = {
        item.annotator_id: item for item in source_doc
    }
    trace_annotators = sorted(
        vote["annotator_id"] for vote in trace_record["raw_votes"]
    )
    source_annotators = sorted(source_by_annotator)
    if trace_annotators != source_annotators:
        fail(
            E_TRACE_BROKEN,
            "trace annotator set differs from source annotations",
            doc_id=doc_id,
            position=position,
            reason="annotator_set_mismatch",
            trace_annotators=trace_annotators,
            source_annotators=source_annotators,
        )
    if sorted(doc["annotator_ids"]) != source_annotators:
        fail(
            E_TRACE_BROKEN,
            "merged document annotator set differs from source annotations",
            doc_id=doc_id,
            position=position,
            reason="document_annotator_set_mismatch",
            document_annotators=sorted(doc["annotator_ids"]),
            source_annotators=source_annotators,
        )

    merged_aliases = _check_alias_config(type_set, config)

    trace_votes_by_annotator = {
        vote["annotator_id"]: vote for vote in trace_record["raw_votes"]
    }
    recomputed_votes: list[dict[str, Any]] = []
    for annotator in source_annotators:
        annotation = source_by_annotator[annotator]
        if position >= len(annotation.ner_tags):
            fail(
                E_TRACE_BROKEN,
                "source annotation has no tag at the traced position",
                doc_id=doc_id,
                position=position,
                reason="source_tag_missing",
                annotator_id=annotator,
            )
        raw_label = annotation.ner_tags[position]
        stored = trace_votes_by_annotator.get(annotator)
        if stored is None:
            fail(
                E_TRACE_BROKEN,
                "trace is missing one annotator's raw vote",
                doc_id=doc_id,
                position=position,
                reason="raw_vote_missing",
                annotator_id=annotator,
            )
        if stored.get("raw_label") != raw_label:
            fail(
                E_TRACE_BROKEN,
                "trace raw vote differs from source annotation",
                doc_id=doc_id,
                position=position,
                reason="raw_vote_mismatch",
                annotator_id=annotator,
                trace_label=stored.get("raw_label"),
                source_label=raw_label,
            )
        normalized = normalize_tag(
            raw_label,
            type_set=type_set,
            config=config,
            merged_aliases=merged_aliases,
            doc_id=doc_id,
            position=position,
        )
        recomputed = {
            "annotator_id": annotator,
            "annotation_version": annotation.annotation_version,
            "raw_label": raw_label,
            "normalized_label": normalized.label,
            "alias": normalized.alias_name,
        }
        if normalized.retirement_event is not None:
            recomputed["retirement_event"] = normalized.retirement_event
        recomputed_votes.append(recomputed)
        if stored.get("normalized_label") != normalized.label:
            fail(
                E_TRACE_BROKEN,
                "trace normalized label cannot be replayed",
                doc_id=doc_id,
                position=position,
                reason="normalized_vote_mismatch",
                annotator_id=annotator,
                trace_label=stored.get("normalized_label"),
                replay_label=normalized.label,
            )

    if config.merge_strategy == "majority":
        selected, selection, choices = _select_majority(recomputed_votes)
    else:
        selected, selection, choices = _select_priority(recomputed_votes, config)
    if selected != trace_record.get("selected_label"):
        fail(
            E_TRACE_BROKEN,
            "replayed label differs from the recorded merged label",
            doc_id=doc_id,
            position=position,
            reason="selected_label_mismatch",
            recorded=trace_record.get("selected_label"),
            replayed=selected,
        )
    if selected != doc["ner_tags"][position]:
        fail(
            E_TRACE_BROKEN,
            "replayed label differs from the final merged document",
            doc_id=doc_id,
            position=position,
            reason="final_tag_mismatch",
            final_tag=doc["ner_tags"][position],
            replayed=selected,
        )
    if not trace_record.get("choices_omitted", False):
        if canonical_json(trace_record.get("choices", [])) != canonical_json(choices):
            fail(
                E_TRACE_BROKEN,
                "recorded vote statistics differ from independent recomputation",
                doc_id=doc_id,
                position=position,
                reason="choice_statistics_mismatch",
                recorded=trace_record.get("choices", []),
                replayed=choices,
            )
    if canonical_json(trace_record.get("selection")) != canonical_json(selection):
        fail(
            E_TRACE_BROKEN,
            "recorded selection metadata differs from replay",
            doc_id=doc_id,
            position=position,
            reason="selection_metadata_mismatch",
            recorded=trace_record.get("selection"),
            replayed=selection,
        )
    expected_trace_id = sha256_value(
        {
            "doc_id": doc_id,
            "position": position,
            "label_set_version": type_set.label_set_version,
            "strategy": config.merge_strategy,
            "annotator_ids": source_annotators,
        }
    )
    if trace_record.get("trace_id") != expected_trace_id:
        fail(
            E_TRACE_BROKEN,
            "trace identifier cannot be replayed",
            doc_id=doc_id,
            position=position,
            reason="trace_id_mismatch",
        )
    return {
        "ok": True,
        "code": "OK",
        "doc_id": doc_id,
        "position": position,
        "label": selected,
        "label_set_version": type_set.label_set_version,
        "annotator_ids": source_annotators,
        "recomputed_choices": choices,
        "trace_id": expected_trace_id,
    }


def verify_all_traces(
    bundle: MergeBundle,
    records: list[AnnotationRecord],
    type_set: TypeSet,
    config: GovernanceConfig,
) -> dict[str, Any]:
    verified = 0
    for doc in bundle.docs:
        for position in range(len(doc["ner_tags"])):
            trace_label(
                bundle,
                records,
                type_set,
                config,
                doc["doc_id"],
                position,
            )
            verified += 1
    return {
        "ok": True,
        "code": "OK",
        "verified_positions": verified,
        "doc_count": len(bundle.docs),
        "merge_digest": bundle.digest,
    }
