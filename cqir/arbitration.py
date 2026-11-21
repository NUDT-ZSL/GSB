from __future__ import annotations

from typing import Any, Iterable

from .config import PipelineConfig
from .errors import fail
from .migration import migrate_answer
from .models import Document, QueryTurn
from .util import canonical_json, sha256_text


def _citation_differences(
    left: dict[str, Any],
    right: dict[str, Any],
    left_name: str,
    right_name: str,
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    left_citations = left["citations"]
    right_citations = right["citations"]
    if len(left_citations) != len(right_citations):
        differences.append(
            {
                "category": "citation_count",
                "left": left_name,
                "right": right_name,
                "left_count": len(left_citations),
                "right_count": len(right_citations),
            }
        )
    for index, (a, b) in enumerate(zip(left_citations, right_citations)):
        for field in (
            "doc_id",
            "start",
            "end",
            "score",
            "confidence_low",
            "confidence_high",
            "paragraph_role",
            "order_weight",
            "revision",
            "context_refs",
        ):
            if a.get(field) != b.get(field):
                differences.append(
                    {
                        "category": "citation_field",
                        "field": field,
                        "citation_index": index,
                        "doc_id": a.get("doc_id") or b.get("doc_id"),
                        "start": a.get("start"),
                        "end": a.get("end"),
                        "left": left_name,
                        "right": right_name,
                        "left_value": a.get(field),
                        "right_value": b.get(field),
                    }
                )
    return differences


def _view_differences(
    reference: dict[str, Any],
    reference_name: str,
    candidate: dict[str, Any],
    candidate_name: str,
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for field in ("conv_id", "turn_no", "answer_text", "doc_versions", "context_chain"):
        if reference.get(field) != candidate.get(field):
            differences.append(
                {
                    "category": "answer_field",
                    "field": field,
                    "left": reference_name,
                    "right": candidate_name,
                    "left_value": reference.get(field),
                    "right_value": candidate.get(field),
                }
            )
    if reference.get("snippets") != candidate.get("snippets"):
        left_snippets = reference.get("snippets", [])
        right_snippets = candidate.get("snippets", [])
        if len(left_snippets) != len(right_snippets):
            differences.append(
                {
                    "category": "snippet_count",
                    "left": reference_name,
                    "right": candidate_name,
                    "left_count": len(left_snippets),
                    "right_count": len(right_snippets),
                }
            )
        for index, (a, b) in enumerate(zip(left_snippets, right_snippets)):
            if a != b:
                differences.append(
                    {
                        "category": "snippet",
                        "snippet_index": index,
                        "left": reference_name,
                        "right": candidate_name,
                        "left_value": a,
                        "right_value": b,
                    }
                )
    differences.extend(
        _citation_differences(reference, candidate, reference_name, candidate_name)
    )
    return differences


def arbitrate(
    texts: dict[str, str],
    documents: dict[str, Document],
    queries: Iterable[QueryTurn] | None = None,
    config: PipelineConfig | None = None,
) -> dict[str, Any]:
    config = config or PipelineConfig()
    if not texts:
        fail("E_INPUT", "at least one answer text is required for arbitration")
    migrated: dict[str, Any] = {}
    for name in ("v1", "v2", "v3"):
        if name not in texts:
            continue
        migrated[name] = migrate_answer(
            texts[name],
            3,
            documents,
            queries,
            config,
        )
    reference_name = "v3" if "v3" in migrated else ("v2" if "v2" in migrated else "v1")
    reference = migrated[reference_name]
    reference_view = reference["package"].semantic_dict()
    reference_hash = sha256_text(canonical_json(reference_view))
    differences: list[dict[str, Any]] = []
    for name, result in migrated.items():
        if name == reference_name:
            continue
        view = result["package"].semantic_dict()
        differences.extend(_view_differences(reference_view, reference_name, view, name))
    if differences:
        categories = sorted({item["category"] for item in differences})
        fail(
            "E_SEMANTIC_INCOMPLETE",
            "formats cannot be arbitrated to identical answer semantics",
            {
                "categories": categories,
                "unrecoverable": differences,
                "reference_format": reference_name,
                "reference_hash": reference_hash,
                "migration_fills": {
                    name: result["fills"] for name, result in migrated.items()
                },
                "migration_drops": {
                    name: result["drops"] for name, result in migrated.items()
                },
            },
        )
    return {
        "ok": True,
        "reference_format": reference_name,
        "canonical_hash": reference_hash,
        "formats": sorted(migrated),
        "migration_fills": {
            name: result["fills"] for name, result in migrated.items()
        },
        "migration_drops": {
            name: result["drops"] for name, result in migrated.items()
        },
        "semantic_view": reference_view,
    }
