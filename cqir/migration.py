from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from .config import PipelineConfig
from .errors import fail
from .formats import parse_answer, serialize
from .models import AnswerPackage, Citation, Document, QueryTurn
from .text import context_chain
from .util import q6


MIGRATION_POLICY: dict[str, Any] = {
    "policy_version": "1.0",
    "v1_to_v2_fill": {
        "confidence_low": "max(0, score - config.confidence_half_width)",
        "confidence_high": "min(1, score + config.confidence_half_width)",
        "paragraph_role": "documents[doc_id].role_for_span(start, end)",
        "order_weight": "1 / (zero_based_citation_index + 1)",
    },
    "to_v3_fill": {
        "context_chain": "query_log context for conv_id and turn_no",
        "context_refs": "copy of the derived context_chain",
        "revision": "constant 1",
    },
    "downgrade_drop": {
        "v1": [
            "confidence_low",
            "confidence_high",
            "paragraph_role",
            "order_weight",
            "context_chain",
            "context_refs",
            "revision",
        ],
        "v2": ["context_chain", "context_refs", "revision"],
    },
}


def _record(
    records: list[dict[str, Any]],
    action: str,
    field: str,
    citation: Citation,
    index: int,
    value: Any,
    source: str,
) -> None:
    records.append(
        {
            "action": action,
            "field": field,
            "citation_index": index,
            "doc_id": citation.doc_id,
            "start": citation.start,
            "end": citation.end,
            "value": value,
            "source": source,
        }
    )


def _context_for(
    package: AnswerPackage,
    queries: Iterable[QueryTurn] | None,
    config: PipelineConfig,
) -> tuple[tuple[str, ...], str]:
    if package.context_chain:
        return package.context_chain, "source_package.context_chain"
    if queries is not None:
        derived = tuple(
            context_chain(
                queries,
                package.conv_id,
                package.turn_no,
                config,
            )
        )
        if derived:
            return derived, "query_log.context_chain"
    fallback = (f"{package.conv_id}:{package.turn_no}",)
    return fallback, "fallback.conv_id_turn_no"


def _upgrade_citation(
    package: AnswerPackage,
    citation: Citation,
    index: int,
    documents: dict[str, Document],
    target_version: int,
    derived_context: tuple[str, ...],
    context_source: str,
    config: PipelineConfig,
    fills: list[dict[str, Any]],
) -> Citation:
    document = documents.get(citation.doc_id)
    if document is None:
        fail(
            "E_SPAN_OUT_OF_RANGE",
            "migration cannot resolve the citation document",
            {"doc_id": citation.doc_id, "citation_index": index},
        )
    confidence_low = citation.confidence_low
    confidence_high = citation.confidence_high
    role = citation.paragraph_role
    order_weight = citation.order_weight
    if target_version >= 2:
        if confidence_low is None:
            confidence_low = q6(
                max(0.0, citation.score - config.confidence_half_width)
            )
            _record(
                fills,
                "fill",
                "confidence_low",
                citation,
                index,
                confidence_low,
                MIGRATION_POLICY["v1_to_v2_fill"]["confidence_low"],
            )
        if confidence_high is None:
            confidence_high = q6(
                min(1.0, citation.score + config.confidence_half_width)
            )
            _record(
                fills,
                "fill",
                "confidence_high",
                citation,
                index,
                confidence_high,
                MIGRATION_POLICY["v1_to_v2_fill"]["confidence_high"],
            )
        if role is None:
            role = document.role_for_span(citation.start, citation.end)
            _record(
                fills,
                "fill",
                "paragraph_role",
                citation,
                index,
                role,
                MIGRATION_POLICY["v1_to_v2_fill"]["paragraph_role"],
            )
        if order_weight is None:
            order_weight = q6(1.0 / (index + 1))
            _record(
                fills,
                "fill",
                "order_weight",
                citation,
                index,
                order_weight,
                MIGRATION_POLICY["v1_to_v2_fill"]["order_weight"],
            )
    revision = citation.revision
    context_refs = citation.context_refs
    if target_version >= 3:
        if revision is None:
            revision = 1
            _record(
                fills,
                "fill",
                "revision",
                citation,
                index,
                revision,
                MIGRATION_POLICY["to_v3_fill"]["revision"],
            )
        if context_refs is None:
            context_refs = derived_context
            _record(
                fills,
                "fill",
                "context_refs",
                citation,
                index,
                list(context_refs),
                context_source,
            )
    return replace(
        citation,
        confidence_low=confidence_low,
        confidence_high=confidence_high,
        paragraph_role=role,
        order_weight=order_weight,
        revision=revision,
        context_refs=context_refs,
    )


def _downgrade_citation(
    citation: Citation,
    index: int,
    target_version: int,
    drops: list[dict[str, Any]],
) -> Citation:
    values: dict[str, Any] = {
        "confidence_low": citation.confidence_low,
        "confidence_high": citation.confidence_high,
        "paragraph_role": citation.paragraph_role,
        "order_weight": citation.order_weight,
        "context_refs": citation.context_refs,
        "revision": citation.revision,
    }
    if target_version == 1:
        for field in (
            "confidence_low",
            "confidence_high",
            "paragraph_role",
            "order_weight",
            "context_refs",
            "revision",
        ):
            if values[field] is not None:
                _record(
                    drops,
                    "drop",
                    field,
                    citation,
                    index,
                    values[field],
                    "v1_cannot_express",
                )
        return Citation(
            doc_id=citation.doc_id,
            start=citation.start,
            end=citation.end,
            score=citation.score,
        )
    for field in ("context_refs", "revision"):
        if values[field] is not None:
            _record(
                drops,
                "drop",
                field,
                citation,
                index,
                values[field],
                "v2_cannot_express",
            )
    return replace(
        citation,
        revision=None,
        context_refs=None,
    )


def migrate_answer(
    text: str,
    target_version: int,
    documents: dict[str, Document],
    queries: Iterable[QueryTurn] | None = None,
    config: PipelineConfig | None = None,
) -> dict[str, Any]:
    config = config or PipelineConfig()
    parsed = parse_answer(text, documents)
    source_version = parsed.format_version
    package = parsed.package
    source_keys = [
        (snippet.doc_id, snippet.start, snippet.end)
        for snippet in package.snippets
    ]
    fills: list[dict[str, Any]] = []
    drops: list[dict[str, Any]] = []
    derived_context, context_source = _context_for(package, queries, config)
    if target_version >= 3 and not package.context_chain:
        fills.append(
            {
                "action": "fill_global",
                "field": "context_chain",
                "citation_index": None,
                "doc_id": None,
                "start": None,
                "end": None,
                "value": list(derived_context),
                "source": context_source,
            }
        )
    if target_version < 3 and package.context_chain:
        for index, citation in enumerate(package.citations):
            if citation.context_refs is None:
                citation = replace(citation, context_refs=package.context_chain)
            _record(
                drops,
                "drop_global",
                "context_chain",
                citation,
                index,
                list(package.context_chain),
                "target_format_cannot_express",
            )
    if target_version == 3:
        citations = tuple(
            _upgrade_citation(
                package,
                citation,
                index,
                documents,
                target_version,
                derived_context,
                context_source,
                config,
                fills,
            )
            for index, citation in enumerate(package.citations)
        )
        target_package = replace(
            package,
            context_chain=derived_context,
            citations=citations,
            format_version=3,
        )
    elif target_version == 2:
        citations = tuple(
            _upgrade_citation(
                package,
                _downgrade_citation(citation, index, 2, drops),
                index,
                documents,
                2,
                derived_context,
                context_source,
                config,
                fills,
            )
            for index, citation in enumerate(package.citations)
        )
        target_package = replace(
            package,
            context_chain=(),
            citations=citations,
            format_version=2,
        )
    elif target_version == 1:
        citations = tuple(
            _downgrade_citation(citation, index, 1, drops)
            for index, citation in enumerate(package.citations)
        )
        target_package = replace(
            package,
            context_chain=(),
            citations=citations,
            format_version=1,
        )
    else:
        fail(
            "E_FORMAT_VERSION_UNSUPPORTED",
            "migration target version is not supported",
            {"target_version": target_version, "supported": [1, 2, 3]},
        )
    target_text = serialize(target_package, target_version)
    reparsed = parse_answer(target_text, documents, expected_format=target_version)
    target_keys = [
        (snippet.doc_id, snippet.start, snippet.end)
        for snippet in reparsed.package.snippets
    ]
    if target_keys != source_keys:
        for index, (before, after) in enumerate(zip(source_keys, target_keys)):
            if before != after:
                fail(
                    "E_SPAN_DRIFT",
                    "migration changed a citation codepoint span",
                    {
                        "citation_index": index,
                        "before": list(before),
                        "after": list(after),
                        "source_version": source_version,
                        "target_version": target_version,
                    },
                )
        fail(
            "E_SPAN_DRIFT",
            "migration changed the number of citation spans",
            {
                "before_count": len(source_keys),
                "after_count": len(target_keys),
                "source_version": source_version,
                "target_version": target_version,
            },
        )
    return {
        "source_version": source_version,
        "target_version": target_version,
        "target_text": target_text,
        "package": reparsed.package,
        "fills": fills,
        "drops": drops,
        "span_check": {
            "ok": True,
            "source_keys": [list(key) for key in source_keys],
            "target_keys": [list(key) for key in target_keys],
        },
        "policy": MIGRATION_POLICY,
    }
