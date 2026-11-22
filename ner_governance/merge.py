"""Deterministic multi-annotator adjudication and controlled-type validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .annotations import (
    group_by_doc,
    validate_document_sets,
    validate_documents,
)
from .config import GovernanceConfig, merged_alias_map
from .errors import (
    E_ALIAS_UNRESOLVED,
    E_BIO_INVALID,
    E_CONFIG_INVALID,
    E_DOC_REJECTED,
    E_RESOURCE_LIMIT,
    E_TYPE_NOT_ALLOWED,
    E_TYPE_RETIRED,
    fail,
)
from .models import (
    AnnotationRecord,
    TypeSet,
    canonical_json,
    is_valid_name,
    sha256_value,
)


@dataclass
class NormalizedTag:
    raw_label: str
    label: str
    prefix: str
    type_name: str | None
    alias_name: str | None = None
    retirement_event: dict[str, Any] | None = None


@dataclass
class MergeBundle:
    label_set_version: str
    strategy: str
    type_set_fingerprint: str
    config_digest: str
    annotations_digest: str
    docs: list[dict[str, Any]]
    business_codes: list[dict[str, Any]]
    degradations: list[dict[str, Any]]
    handling_summary: dict[str, Any]
    resident_vote_units: int
    digest: str = ""

    @property
    def code(self) -> str:
        return E_TYPE_RETIRED if self.business_codes else "OK"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "code": self.code,
            "label_set_version": self.label_set_version,
            "strategy": self.strategy,
            "type_set_fingerprint": self.type_set_fingerprint,
            "config_digest": self.config_digest,
            "annotations_digest": self.annotations_digest,
            "docs": self.docs,
            "business_codes": self.business_codes,
            "degradations": self.degradations,
            "handling_summary": self.handling_summary,
            "resident_vote_units": self.resident_vote_units,
            "digest": self.digest,
        }

    def refresh_digest(self) -> None:
        payload = self.to_dict()
        payload["digest"] = ""
        self.digest = sha256_value(payload)

    def final_annotations(self) -> dict[str, dict[str, Any]]:
        return {
            doc["doc_id"]: {
                "tokens": list(doc["tokens"]),
                "ner_tags": list(doc["ner_tags"]),
                "label_set_version": doc["label_set_version"],
            }
            for doc in self.docs
        }


def _resolve_alias_target(
    alias_name: str,
    *,
    type_set: TypeSet,
    config: GovernanceConfig,
    merged_aliases: dict[str, str],
    doc_id: str,
    position: int,
    raw_label: str,
) -> tuple[str | None, bool]:
    """Return a type name and whether the name came from an alias."""

    if alias_name in config.alias_mapping:
        return config.alias_mapping[alias_name], True
    type_alias = type_set.all_alias_map().get(alias_name)
    if type_alias is not None:
        target, enabled = type_alias
        if not enabled:
            fail(
                E_ALIAS_UNRESOLVED,
                f"disabled alias {alias_name!r} cannot be resolved",
                doc_id=doc_id,
                position=position,
                label=raw_label,
                field="alias",
                reason="disabled_alias",
                alias=alias_name,
                target=target,
            )
        return target, True
    if alias_name in merged_aliases:
        return merged_aliases[alias_name], True
    return None, False


def _retired_replacement(
    retired_type: str,
    *,
    config: GovernanceConfig,
    active_types: set[str],
    doc_id: str,
    position: int,
    raw_label: str,
) -> str:
    policy = config.retirement_policy
    if policy == "reject":
        fail(
            E_TYPE_RETIRED,
            f"retired type {retired_type!r} was explicitly rejected",
            doc_id=doc_id,
            position=position,
            label=raw_label,
            reason="retired_type_rejected",
            retired_type=retired_type,
            processing_category="retired_rejected",
        )
    if policy == "doc_reject":
        fail(
            E_DOC_REJECTED,
            f"document rejected because it contains retired type {retired_type!r}",
            doc_id=doc_id,
            position=position,
            label=raw_label,
            reason="retired_type_doc_reject",
            retired_type=retired_type,
            processing_category="doc_rejected",
        )
    replacement = config.retirement_substitutes.get(retired_type)
    if replacement is None:
        fail(
            E_TYPE_RETIRED,
            f"retired type {retired_type!r} has no configured substitute",
            doc_id=doc_id,
            position=position,
            label=raw_label,
            reason="retired_substitute_missing",
            retired_type=retired_type,
            processing_category="retired_rejected",
        )
    if replacement not in active_types:
        fail(
            E_TYPE_RETIRED,
            f"substitute type {replacement!r} is not active",
            doc_id=doc_id,
            position=position,
            label=raw_label,
            reason="retired_substitute_not_active",
            retired_type=retired_type,
            substitute_type=replacement,
            processing_category="retired_rejected",
        )
    return replacement


def normalize_tag(
    raw_label: str,
    *,
    type_set: TypeSet,
    config: GovernanceConfig,
    merged_aliases: dict[str, str],
    doc_id: str,
    position: int,
) -> NormalizedTag:
    if raw_label == "O":
        return NormalizedTag(
            raw_label=raw_label,
            label="O",
            prefix="O",
            type_name=None,
        )
    if len(raw_label) < 3 or raw_label[1] != "-" or raw_label[0] not in ("B", "I"):
        fail(
            E_BIO_INVALID,
            f"malformed BIO label {raw_label!r}",
            doc_id=doc_id,
            position=position,
            label=raw_label,
            reason="malformed_label",
        )
    prefix = raw_label[0]
    raw_type = raw_label[2:]
    if not is_valid_name(raw_type):
        fail(
            E_BIO_INVALID,
            f"invalid BIO entity type in {raw_label!r}",
            doc_id=doc_id,
            position=position,
            label=raw_label,
            reason="invalid_type_name",
        )
    active_types = set(type_set.active_type_names())
    retired_types = set(type_set.retired_type_names())
    alias_target, is_alias = _resolve_alias_target(
        raw_type,
        type_set=type_set,
        config=config,
        merged_aliases=merged_aliases,
        doc_id=doc_id,
        position=position,
        raw_label=raw_label,
    )
    resolved_type = alias_target if is_alias else raw_type
    alias_name = raw_type if is_alias else None
    if resolved_type is None:
        if raw_type in retired_types:
            resolved_type = raw_type
        else:
            fail(
                E_TYPE_NOT_ALLOWED,
                f"type {raw_type!r} is not in the controlled type set",
                doc_id=doc_id,
                position=position,
                label=raw_label,
                reason="type_not_allowed",
                type=raw_type,
            )
    if is_alias and resolved_type not in active_types and resolved_type not in retired_types:
        fail(
            E_ALIAS_UNRESOLVED,
            f"alias {alias_name!r} targets unknown type {resolved_type!r}",
            doc_id=doc_id,
            position=position,
            label=raw_label,
            field="alias",
            reason="alias_target_missing",
            alias=alias_name,
            target=resolved_type,
        )
    retirement_event: dict[str, Any] | None = None
    if resolved_type in retired_types:
        replacement = _retired_replacement(
            resolved_type,
            config=config,
            active_types=active_types,
            doc_id=doc_id,
            position=position,
            raw_label=raw_label,
        )
        retirement_event = {
            "retired_type": resolved_type,
            "replacement_type": replacement,
            "processing_category": "retired_substituted",
            "fallback_reason": "type_retired",
            "via_alias": alias_name,
            "raw_label": raw_label,
            "prefix": prefix,
        }
        resolved_type = replacement
    if resolved_type not in active_types:
        fail(
            E_TYPE_NOT_ALLOWED,
            f"normalized type {resolved_type!r} is not enabled",
            doc_id=doc_id,
            position=position,
            label=raw_label,
            reason="type_not_allowed",
            type=resolved_type,
        )
    normalized_label = f"{prefix}-{resolved_type}"
    return NormalizedTag(
        raw_label=raw_label,
        label=normalized_label,
        prefix=prefix,
        type_name=resolved_type,
        alias_name=alias_name,
        retirement_event=retirement_event,
    )


def _select_majority(
    raw_votes: list[dict[str, Any]],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    grouped: dict[str, list[str]] = {}
    for vote in raw_votes:
        grouped.setdefault(vote["normalized_label"], []).append(vote["annotator_id"])
    choices = [
        {
            "label": label,
            "count": len(annotators),
            "annotator_ids": sorted(annotators),
        }
        for label, annotators in sorted(grouped.items())
    ]
    top_count = max(choice["count"] for choice in choices)
    tied = [choice for choice in choices if choice["count"] == top_count]
    tied.sort(
        key=lambda choice: (
            choice["annotator_ids"][0],
            choice["label"],
        )
    )
    selected = tied[0]
    tie_break_used = len(tied) > 1
    selection = {
        "rule": "majority",
        "tied_labels": [choice["label"] for choice in tied],
        "tie_break_used": tie_break_used,
        "tie_break_annotator": selected["annotator_ids"][0],
    }
    choices.sort(key=lambda choice: (-choice["count"], choice["label"]))
    return selected["label"], selection, choices


def _select_priority(
    raw_votes: list[dict[str, Any]],
    config: GovernanceConfig,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    rank = {
        annotator: index
        for index, annotator in enumerate(config.annotator_priority)
    }
    ordered = sorted(
        raw_votes,
        key=lambda vote: (
            rank.get(vote["annotator_id"], len(rank)),
            vote["annotator_id"],
        ),
    )
    selected = ordered[0]
    grouped: dict[str, list[str]] = {}
    for vote in raw_votes:
        grouped.setdefault(vote["normalized_label"], []).append(vote["annotator_id"])
    choices = [
        {
            "label": label,
            "count": len(annotators),
            "annotator_ids": sorted(annotators),
        }
        for label, annotators in sorted(grouped.items())
    ]
    choices.sort(key=lambda choice: (-choice["count"], choice["label"]))
    selection = {
        "rule": "annotator_priority",
        "selected_annotator_id": selected["annotator_id"],
        "configured_rank": rank.get(selected["annotator_id"]),
        "priority_fallback_used": selected["annotator_id"] not in rank,
    }
    return selected["normalized_label"], selection, choices


def _validate_bio(
    doc_id: str,
    tags: list[str],
    *,
    active_types: set[str],
) -> None:
    for position, label in enumerate(tags):
        if label == "O":
            continue
        if len(label) < 3 or label[0] not in ("B", "I") or label[1] != "-":
            fail(
                E_BIO_INVALID,
                f"malformed final BIO label {label!r}",
                doc_id=doc_id,
                position=position,
                label=label,
                reason="malformed_final_label",
            )
        prefix = label[0]
        type_name = label[2:]
        if type_name not in active_types:
            fail(
                E_TYPE_NOT_ALLOWED,
                f"final type {type_name!r} is not enabled",
                doc_id=doc_id,
                position=position,
                label=label,
                reason="final_type_not_allowed",
                type=type_name,
            )
        if prefix == "I":
            valid = False
            if position > 0:
                previous = tags[position - 1]
                if (
                    len(previous) >= 3
                    and previous[1] == "-"
                    and previous[0] in ("B", "I")
                    and previous[2:] == type_name
                ):
                    valid = True
            if not valid:
                fail(
                    E_BIO_INVALID,
                    "I- label is not preceded by the same entity type",
                    doc_id=doc_id,
                    position=position,
                    label=label,
                    reason="i_without_b_or_i",
                )


def _check_alias_config(
    type_set: TypeSet,
    config: GovernanceConfig,
) -> dict[str, str]:
    type_names = set(type_set.type_map())
    for alias in config.alias_mapping:
        if alias in type_names:
            fail(
                E_CONFIG_INVALID,
                f"configured alias {alias!r} collides with an entity type",
                field=f"alias_mapping.{alias}",
                reason="alias_type_collision",
            )
    type_aliases = type_set.active_alias_map()
    all_type_aliases = type_set.all_alias_map()
    for alias, target in config.alias_mapping.items():
        if alias in all_type_aliases:
            existing, alias_enabled = all_type_aliases[alias]
            if not alias_enabled:
                fail(
                    E_CONFIG_INVALID,
                    f"configured alias {alias!r} is disabled in the type set",
                    field=f"alias_mapping.{alias}",
                    reason="disabled_alias_in_config",
                )
            if existing != target:
                fail(
                    E_CONFIG_INVALID,
                    f"alias {alias!r} has conflicting type-set and config targets",
                    field=f"alias_mapping.{alias}",
                    reason="conflicting_alias_target",
                    existing=existing,
                    configured=target,
                )
        if alias in type_aliases and type_aliases[alias] != target:
            fail(
                E_CONFIG_INVALID,
                f"alias {alias!r} has conflicting type-set and config targets",
                field=f"alias_mapping.{alias}",
                reason="conflicting_alias_target",
                existing=type_aliases[alias],
                configured=target,
            )
    return merged_alias_map(type_aliases, config.alias_mapping)


def merge_annotations(
    records: list[AnnotationRecord],
    type_set: TypeSet,
    config: GovernanceConfig,
) -> MergeBundle:
    """Adjudicate every token position with deterministic ordering."""

    if len(type_set.types) > config.max_types:
        fail(
            E_RESOURCE_LIMIT,
            "controlled type count exceeds max_types",
            field="max_types",
            category="type_count",
            actual=len(type_set.types),
            limit=config.max_types,
        )
    if (
        config.label_set_version is not None
        and config.label_set_version != type_set.label_set_version
    ):
        fail(
            E_CONFIG_INVALID,
            "config label_set_version does not match the type set",
            field="label_set_version",
            configured=config.label_set_version,
            type_set=type_set.label_set_version,
        )
    validate_document_sets(records)
    grouped = group_by_doc(records)
    validate_documents(grouped)
    merged_aliases = _check_alias_config(type_set, config)
    active_types = set(type_set.active_type_names())
    docs: list[dict[str, Any]] = []
    business_codes: list[dict[str, Any]] = []
    degradations: list[dict[str, Any]] = []
    category_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    resident_vote_units = 0

    for doc_id in sorted(grouped):
        doc_annotations = grouped[doc_id]
        source_tokens = list(doc_annotations[0].tokens)
        source_token_count = len(source_tokens)
        doc_degradations: list[dict[str, Any]] = []
        keep_count = source_token_count
        if source_token_count > config.max_tokens_per_doc:
            if config.resource_policy == "fail":
                fail(
                    E_RESOURCE_LIMIT,
                    "document token count exceeds max_tokens_per_doc",
                    doc_id=doc_id,
                    field="max_tokens_per_doc",
                    category="token_count",
                    actual=source_token_count,
                    limit=config.max_tokens_per_doc,
                )
            keep_count = config.max_tokens_per_doc
            degradation = {
                "category": "token_limit_truncated",
                "doc_id": doc_id,
                "actual": source_token_count,
                "limit": config.max_tokens_per_doc,
                "retained": keep_count,
            }
            degradations.append(degradation)
            doc_degradations.append(degradation)
        tokens = source_tokens[:keep_count]
        vote_records: list[dict[str, Any]] = []
        final_tags: list[str] = []
        doc_categories: set[str] = set()
        doc_reasons: set[str] = set()
        for position in range(keep_count):
            raw_votes: list[dict[str, Any]] = []
            for annotation in doc_annotations:
                raw_label = annotation.ner_tags[position]
                normalized = normalize_tag(
                    raw_label,
                    type_set=type_set,
                    config=config,
                    merged_aliases=merged_aliases,
                    doc_id=doc_id,
                    position=position,
                )
                vote = {
                    "annotator_id": annotation.annotator_id,
                    "annotation_version": annotation.annotation_version,
                    "raw_label": raw_label,
                    "normalized_label": normalized.label,
                    "alias": normalized.alias_name,
                }
                if normalized.retirement_event is not None:
                    vote["retirement_event"] = normalized.retirement_event
                raw_votes.append(vote)
            retirement_events: dict[tuple[str, str, str], dict[str, Any]] = {}
            for vote in raw_votes:
                event = vote.get("retirement_event")
                if event is None:
                    continue
                key = (
                    event["retired_type"],
                    event["replacement_type"],
                    event["prefix"],
                )
                retirement_events[key] = event
            for event in retirement_events.values():
                category = event["processing_category"]
                reason = event["fallback_reason"]
                doc_categories.add(category)
                doc_reasons.add(reason)
                category_counts[category] = category_counts.get(category, 0) + 1
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                business_codes.append(
                    {
                        "code": E_TYPE_RETIRED,
                        "severity": "warning",
                        "message": (
                            f"retired type {event['retired_type']} "
                            f"replaced by {event['replacement_type']}"
                        ),
                        "doc_id": doc_id,
                        "position": position,
                        "label": event["raw_label"],
                        "processing_category": category,
                        "fallback_reason": reason,
                        "retired_type": event["retired_type"],
                        "replacement_label": (
                            f"{event['prefix']}-{event['replacement_type']}"
                        ),
                    }
                )
            if config.merge_strategy == "majority":
                selected, selection, choices = _select_majority(raw_votes)
            else:
                selected, selection, choices = _select_priority(raw_votes, config)
            resident_vote_units += len(choices)
            record = {
                "doc_id": doc_id,
                "position": position,
                "token": tokens[position],
                "label_set_version": type_set.label_set_version,
                "strategy": config.merge_strategy,
                "raw_votes": raw_votes,
                "choices": choices,
                "choices_omitted": False,
                "selected_label": selected,
                "selection": selection,
                "processing_categories": sorted(
                    {
                        vote["retirement_event"]["processing_category"]
                        for vote in raw_votes
                        if "retirement_event" in vote
                    }
                ),
                "fallback_reasons": sorted(
                    {
                        vote["retirement_event"]["fallback_reason"]
                        for vote in raw_votes
                        if "retirement_event" in vote
                    }
                ),
                "trace_id": sha256_value(
                    {
                        "doc_id": doc_id,
                        "position": position,
                        "label_set_version": type_set.label_set_version,
                        "strategy": config.merge_strategy,
                        "annotator_ids": [
                            vote["annotator_id"] for vote in raw_votes
                        ],
                    }
                ),
            }
            vote_records.append(record)
            final_tags.append(selected)
        _validate_bio(doc_id, final_tags, active_types=active_types)
        docs.append(
            {
                "doc_id": doc_id,
                "tokens": tokens,
                "ner_tags": final_tags,
                "source_token_count": source_token_count,
                "annotator_ids": sorted(
                    annotation.annotator_id for annotation in doc_annotations
                ),
                "annotation_versions": sorted(
                    {
                        annotation.annotation_version
                        for annotation in doc_annotations
                    }
                ),
                "label_set_version": type_set.label_set_version,
                "processing_categories": sorted(doc_categories),
                "fallback_reasons": sorted(doc_reasons),
                "vote_records": vote_records,
                "degradations": doc_degradations,
            }
        )

    if resident_vote_units > config.max_resident_votes:
        if config.resource_policy == "fail":
            fail(
                E_RESOURCE_LIMIT,
                "resident vote intermediates exceed max_resident_votes",
                field="max_resident_votes",
                category="resident_votes",
                actual=resident_vote_units,
                limit=config.max_resident_votes,
            )
        degradation = {
            "category": "vote_details_omitted",
            "actual": resident_vote_units,
            "limit": config.max_resident_votes,
        }
        degradations.append(degradation)
        for doc in docs:
            for record in doc["vote_records"]:
                record["choices"] = []
                record["choices_omitted"] = True
            doc["degradations"].append(degradation)

    annotations_digest = sha256_value(
        [record.as_dict() for record in sorted(
            records,
            key=lambda item: (item.doc_id, item.annotator_id),
        )]
    )
    bundle = MergeBundle(
        label_set_version=type_set.label_set_version,
        strategy=config.merge_strategy,
        type_set_fingerprint=type_set.semantic_fingerprint(),
        config_digest=config.digest(),
        annotations_digest=annotations_digest,
        docs=docs,
        business_codes=business_codes,
        degradations=degradations,
        handling_summary={
            "category_counts": dict(sorted(category_counts.items())),
            "fallback_reason_counts": dict(sorted(reason_counts.items())),
        },
        resident_vote_units=resident_vote_units,
    )
    bundle.refresh_digest()
    return bundle


def merged_annotations_json(bundle: MergeBundle) -> str:
    """Stable serialization used for byte-for-byte determinism checks."""

    return canonical_json(bundle.final_annotations())
