"""Disambiguated-term index aggregation."""

from __future__ import annotations

from typing import Any

from .util import checksum_object, rounded


def _summary(values: list[float], digits: int) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "sum": 0.0,
            "mean": None,
        }
    return {
        "count": len(values),
        "min": rounded(min(values), digits),
        "max": rounded(max(values), digits),
        "sum": rounded(sum(values), digits),
        "mean": rounded(sum(values) / len(values), digits),
    }


def build_index(
    disambiguated_documents: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    selected_path: str,
    rule_version: str,
) -> dict[str, Any]:
    digits = int(config["scoring"]["round_digits"])
    occurrences: list[dict[str, Any]] = []
    for doc in disambiguated_documents:
        for result in doc["results"]:
            occurrences.append(result)
    occurrences.sort(
        key=lambda item: (
            item["term"],
            item["sense_id"],
            item["doc_id"],
            item["start"],
            item["candidate_id"],
        )
    )

    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for occurrence in occurrences:
        grouped.setdefault(
            (occurrence["term"], occurrence["sense_id"]), []
        ).append(occurrence)

    entries = []
    for (term, sense_id), rows in sorted(grouped.items()):
        doc_counts: dict[str, int] = {}
        for row in rows:
            doc_counts[row["doc_id"]] = doc_counts.get(row["doc_id"], 0) + 1
        confidences = [float(row["candidate_confidence"]) for row in rows]
        disambiguation_scores = [
            float(row["disambiguation_score"]) for row in rows
        ]
        entries.append(
            {
                "term": term,
                "sense_id": sense_id,
                "definition": rows[0]["definition"],
                "first_doc_id": min(row["doc_id"] for row in rows),
                "count": len(rows),
                "doc_occurrence_counts": {
                    doc_id: doc_counts[doc_id] for doc_id in sorted(doc_counts)
                },
                "confidence_summary": _summary(confidences, digits),
                "disambiguation_score_summary": _summary(
                    disambiguation_scores, digits
                ),
                "rule_version": rule_version,
            }
        )
    index = {
        "index_version": "term-index-v1",
        "glossary_version": config.get("glossary_version"),
        "rule_version": rule_version,
        "selected_path": selected_path,
        "entry_count": len(entries),
        "occurrence_count": len(occurrences),
        "entries": entries,
    }
    index["checksum"] = checksum_object(index)
    return index


def build_index_from_ledger_rows(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    selected_path: str,
    rule_version: str,
    glossary_version: str | None,
) -> dict[str, Any]:
    """Recompute the same index shape from per-document ledger rows."""

    digits = int(config["scoring"]["round_digits"])
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["term"], row["sense_id"]), []).append(row)
    entries = []
    for (term, sense_id), values in sorted(grouped.items()):
        doc_counts: dict[str, int] = {}
        for row in values:
            doc_counts[row["doc_id"]] = doc_counts.get(row["doc_id"], 0) + 1
        confidences = [float(row["candidate_confidence"]) for row in values]
        disambiguation_scores = [
            float(row["disambiguation_score"]) for row in values
        ]
        entries.append(
            {
                "term": term,
                "sense_id": sense_id,
                "definition": values[0]["definition"],
                "first_doc_id": min(row["doc_id"] for row in values),
                "count": len(values),
                "doc_occurrence_counts": {
                    doc_id: doc_counts[doc_id] for doc_id in sorted(doc_counts)
                },
                "confidence_summary": _summary(confidences, digits),
                "disambiguation_score_summary": _summary(
                    disambiguation_scores, digits
                ),
                "rule_version": rule_version,
            }
        )
    index = {
        "index_version": "term-index-v1",
        "glossary_version": glossary_version,
        "rule_version": rule_version,
        "selected_path": selected_path,
        "entry_count": len(entries),
        "occurrence_count": len(rows),
        "entries": entries,
    }
    index["checksum"] = checksum_object(index)
    return index

