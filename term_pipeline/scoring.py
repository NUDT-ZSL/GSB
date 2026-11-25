"""Candidate confidence scoring with fully auditable contributions."""

from __future__ import annotations

from typing import Any

from .features import observations_in_window
from .glossary import glossary_index
from .util import overlap_chars, rounded


def score_document(
    doc: dict[str, Any],
    candidates: list[dict[str, Any]],
    glossary: dict[str, Any],
    features: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    scoring = config["scoring"]
    digits = scoring["round_digits"]
    window_radius = int(scoring["context_window_chars"])
    _, entries_by_key = glossary_index(glossary)
    scored: list[dict[str, Any]] = []

    for candidate in candidates:
        entry = entries_by_key[(candidate["term_id"], candidate["entry_no"])]
        window_start = max(0, candidate["start"] - window_radius)
        window_end = min(doc["char_len"], candidate["end"] + window_radius)
        window_len = max(1, window_end - window_start)
        matched = observations_in_window(
            features,
            doc["doc_id"],
            doc["para_no"],
            window_start,
            window_end,
        )
        feature_details = []
        weighted_overlap = 0.0
        for observation in matched:
            overlap = overlap_chars(
                observation["start"],
                observation["end"],
                window_start,
                window_end,
            )
            contribution = overlap * observation["weight"]
            weighted_overlap += contribution
            feature_details.append(
                {
                    "feature_id": observation["feature_id"],
                    "start": observation["start"],
                    "end": observation["end"],
                    "overlap_chars": overlap,
                    "weight": observation["weight"],
                    "weighted_overlap": rounded(contribution, digits),
                }
            )
        coverage = min(1.0, weighted_overlap / window_len)
        length_contribution = scoring["length_weights"][candidate["length_tier"]]
        source_contribution = (
            entry["source_weight"] * scoring["source_weight_scale"]
        )
        coverage_contribution = coverage * scoring["coverage_weight"]
        components = [
            {
                "name": "length_tier",
                "raw": candidate["length_tier"],
                "weight": float(length_contribution),
                "contribution": rounded(length_contribution, digits),
            },
            {
                "name": "dictionary_source",
                "raw": {
                    "source": entry["source"],
                    "source_weight": entry["source_weight"],
                },
                "weight": scoring["source_weight_scale"],
                "contribution": rounded(source_contribution, digits),
            },
            {
                "name": "context_coverage",
                "raw": rounded(coverage, digits),
                "weight": scoring["coverage_weight"],
                "contribution": rounded(coverage_contribution, digits),
            },
        ]
        score = rounded(sum(item["contribution"] for item in components), digits)
        scored_candidate = dict(candidate)
        scored_candidate["confidence"] = score
        scored_candidate["score_breakdown"] = {
            "formula": "sum(components)",
            "round_digits": digits,
            "components": components,
            "score": score,
        }
        scored_candidate["context"] = {
            "window_start": window_start,
            "window_end": window_end,
            "window_chars": window_len,
            "coverage": rounded(coverage, digits),
            "feature_ids": sorted(
                {item["feature_id"] for item in feature_details}
            ),
            "observations": feature_details,
        }
        scored.append(scored_candidate)
    scored.sort(key=lambda item: (item["start"], item["end"], item["entry_no"]))
    return scored


def score_documents(
    documents: list[dict[str, Any]],
    extracted: list[dict[str, Any]],
    glossary: dict[str, Any],
    features: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    docs_by_id = {doc["doc_id"]: doc for doc in documents}
    extracted_by_id = {item["doc_id"]: item for item in extracted}
    result = []
    for doc in documents:
        candidates = extracted_by_id[doc["doc_id"]]["candidates"]
        scored = score_document(
            docs_by_id[doc["doc_id"]],
            candidates,
            glossary,
            features,
            config,
        )
        result.append(
            {
                "doc_id": doc["doc_id"],
                "candidates": scored,
            }
        )
    return result


def recompute_score(score_breakdown: dict[str, Any]) -> float:
    digits = int(score_breakdown["round_digits"])
    return rounded(
        sum(float(item["contribution"]) for item in score_breakdown["components"]),
        digits,
    )

