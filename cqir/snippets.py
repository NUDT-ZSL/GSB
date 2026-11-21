from __future__ import annotations

from typing import Any

from .config import PipelineConfig
from .errors import fail
from .models import Document, Snippet
from .text import tokenize
from .util import q6


def extract_snippets(
    document: Document,
    normalized: dict[str, Any],
    config: PipelineConfig,
) -> list[dict[str, Any]]:
    query_items = {
        str(item["term"]): float(item["weight"])
        for item in normalized["tokens"]
    }
    total_query_weight = sum(query_items.values())
    candidates: list[dict[str, Any]] = []
    for start, end, role in document.paragraph_spans:
        snippet_text = document.text[start:end]
        snippet_terms = sorted(set(tokenize(snippet_text)))
        matched = [term for term in snippet_terms if term in query_items]
        if not matched:
            continue
        confidence = q6(
            sum(query_items[term] for term in matched) / total_query_weight
            if total_query_weight
            else 0.0
        )
        coverage = q6(len(matched) / len(query_items) if query_items else 0.0)
        final_score = q6(
            config.confidence_weight * confidence
            + config.coverage_weight * coverage
        )
        detail = {
            "doc_id": document.doc_id,
            "start": start,
            "end": end,
            "paragraph_role": role,
            "matched_terms": matched,
            "weights": {
                "confidence": q6(config.confidence_weight),
                "coverage": q6(config.coverage_weight),
            },
            "confidence": confidence,
            "coverage": coverage,
            "final_score": final_score,
        }
        candidates.append(
            Snippet(
                doc_id=document.doc_id,
                start=start,
                end=end,
                text=snippet_text,
                score_detail=detail,
            ).to_dict()
        )
    return sorted(
        candidates,
        key=lambda item: (
            -float(item["score_detail"]["final_score"]),
            item["doc_id"],
            item["start"],
            item["end"],
        ),
    )


def recompute_detail_score(detail: dict[str, Any]) -> float:
    weights = detail["weights"]
    return q6(
        float(weights["confidence"]) * float(detail["confidence"])
        + float(weights["coverage"]) * float(detail["coverage"])
    )


def validate_score_detail(detail: dict[str, Any]) -> None:
    recomputed = recompute_detail_score(detail)
    if recomputed != q6(float(detail["final_score"])):
        fail(
            "E_INCONSISTENT",
            "snippet score detail does not recompute to final score",
            {
                "doc_id": detail.get("doc_id"),
                "start": detail.get("start"),
                "end": detail.get("end"),
                "final_score": detail.get("final_score"),
                "recomputed": recomputed,
            },
        )


def select_snippets(
    recall_result: dict[str, Any],
    snippet_candidates: dict[str, list[dict[str, Any]]],
    documents: dict[str, Document],
    config: PipelineConfig,
) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for doc_id in recall_result["top_doc_ids"]:
        for candidate in snippet_candidates.get(doc_id, []):
            validate_score_detail(candidate["score_detail"])
            flattened.append(candidate)
    flattened.sort(
        key=lambda item: (
            -float(item["score_detail"]["final_score"]),
            item["doc_id"],
            item["start"],
            item["end"],
        )
    )
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for candidate in flattened:
        key = (candidate["doc_id"], candidate["start"], candidate["end"])
        if key in seen:
            fail(
                "E_CITE_DUP",
                "duplicate citation for the same document span",
                {"doc_id": key[0], "start": key[1], "end": key[2]},
            )
        document = documents.get(candidate["doc_id"])
        if document is None or candidate["start"] < 0 or candidate["end"] > len(document.text):
            fail(
                "E_SPAN_OUT_OF_RANGE",
                "selected snippet span is outside the document",
                {
                    "doc_id": candidate["doc_id"],
                    "start": candidate["start"],
                    "end": candidate["end"],
                    "doc_codepoint_len": len(document.text) if document else None,
                },
            )
        seen.add(key)
        selected.append(candidate)
        if len(selected) >= config.snippet_top_k:
            break
    return selected
