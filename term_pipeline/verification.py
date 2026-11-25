"""Cross-stage and ledger/index consistency checks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import E_INDEX_INCONSISTENT, PipelineError
from .indexer import build_index_from_ledger_rows
from .util import rounded


def _fail(deviation: dict[str, Any]) -> None:
    raise PipelineError(
        E_INDEX_INCONSISTENT,
        "index and ledger are inconsistent",
        {"first_deviation": deviation},
    )


def _ledger_rows(
    db_path: str | Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    path = Path(db_path)
    if not path.exists():
        _fail({"type": "ledger_missing", "path": path.name})
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        payload = {
            "documents": [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM documents ORDER BY doc_id"
                )
            ],
            "candidates": [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM candidates ORDER BY doc_id, candidate_id"
                )
            ],
            "disambiguations": [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM disambiguations ORDER BY doc_id, candidate_id"
                )
            ],
            "tradeoffs": [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM tradeoffs ORDER BY doc_id"
                )
            ],
        }
        meta = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute("SELECT key, value FROM meta")
        }
        return payload, meta
    except sqlite3.DatabaseError as exc:
        _fail({"type": "ledger_unreadable", "path": path.name, "reason": str(exc)})
    finally:
        connection.close()


def _recompute_candidate_score(score_breakdown: dict[str, Any]) -> float:
    digits = int(score_breakdown["round_digits"])
    return rounded(
        sum(float(item["contribution"]) for item in score_breakdown["components"]),
        digits,
    )


def _recompute_disambiguation_score(score_breakdown: dict[str, Any]) -> float:
    if score_breakdown["formula"] == "monosemous":
        return float(score_breakdown["selected_score"])
    selected_sense = next(
        item
        for item in score_breakdown["sense_scores"]
        if item["sense_id"] == score_breakdown["selected_sense_id"]
    )
    return rounded(
        float(selected_sense["prior"])
        + sum(
            float(item["contribution"])
            for item in selected_sense["rule_contributions"]
        ),
        12,
    )


def verify_artifacts(
    index: dict[str, Any],
    db_path: str | Path,
    documents: list[dict[str, Any]],
    scored_documents: list[dict[str, Any]],
    disambiguated_documents: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Verify candidate coverage, disambiguation coverage, and all index stats."""

    ledger, meta = _ledger_rows(db_path)
    docs_by_id = {doc["doc_id"]: doc for doc in documents}
    scored_by_doc = {
        item["doc_id"]: item["candidates"] for item in scored_documents
    }
    disambiguated_by_doc = {
        item["doc_id"]: item for item in disambiguated_documents
    }

    ledger_doc_ids = {row["doc_id"] for row in ledger["documents"]}
    expected_doc_ids = set(docs_by_id)
    if ledger_doc_ids != expected_doc_ids:
        _fail(
            {
                "type": "document_set_mismatch",
                "missing_in_ledger": sorted(expected_doc_ids - ledger_doc_ids),
                "extra_in_ledger": sorted(ledger_doc_ids - expected_doc_ids),
            }
        )

    ledger_candidates_by_doc: dict[str, list[dict[str, Any]]] = {}
    for row in ledger["candidates"]:
        ledger_candidates_by_doc.setdefault(row["doc_id"], []).append(row)
    ledger_disambiguation_by_doc: dict[str, list[dict[str, Any]]] = {}
    for row in ledger["disambiguations"]:
        ledger_disambiguation_by_doc.setdefault(row["doc_id"], []).append(row)
    tradeoff_doc_ids = {row["doc_id"] for row in ledger["tradeoffs"]}

    for doc_id in sorted(expected_doc_ids):
        expected = sorted(
            scored_by_doc.get(doc_id, []), key=lambda item: item["candidate_id"]
        )
        actual = sorted(
            ledger_candidates_by_doc.get(doc_id, []),
            key=lambda item: item["candidate_id"],
        )
        expected_ids = [item["candidate_id"] for item in expected]
        actual_ids = [item["candidate_id"] for item in actual]
        if expected_ids != actual_ids:
            _fail(
                {
                    "type": "candidate_set_mismatch",
                    "doc_id": doc_id,
                    "missing_in_ledger": sorted(set(expected_ids) - set(actual_ids)),
                    "extra_in_ledger": sorted(set(actual_ids) - set(expected_ids)),
                }
            )
        expected_by_id = {item["candidate_id"]: item for item in expected}
        for row in actual:
            candidate = expected_by_id[row["candidate_id"]]
            for field in (
                "term_id",
                "entry_no",
                "term",
                "start",
                "end",
                "length_tier",
                "match_rule_version",
                "glossary_version",
            ):
                if row[field] != candidate[field]:
                    _fail(
                        {
                            "type": "candidate_field_mismatch",
                            "doc_id": doc_id,
                            "candidate_id": row["candidate_id"],
                            "field": field,
                            "ledger": row[field],
                            "expected": candidate[field],
                        }
                    )
            score_breakdown = json.loads(row["score_breakdown_json"])
            recomputed = _recompute_candidate_score(score_breakdown)
            if abs(recomputed - float(row["confidence"])) > 1e-12:
                _fail(
                    {
                        "type": "candidate_score_not_recomputable",
                        "doc_id": doc_id,
                        "candidate_id": row["candidate_id"],
                        "recorded": row["confidence"],
                        "recomputed": recomputed,
                    }
                )
        if doc_id not in tradeoff_doc_ids:
            _fail({"type": "tradeoff_missing", "doc_id": doc_id})

        expected_disambiguation_ids = sorted(
            result["candidate_id"]
            for result in disambiguated_by_doc[doc_id]["results"]
        )
        actual_disambiguation_ids = sorted(
            row["candidate_id"]
            for row in ledger_disambiguation_by_doc.get(doc_id, [])
        )
        if expected_disambiguation_ids != actual_disambiguation_ids:
            _fail(
                {
                    "type": "disambiguation_set_mismatch",
                    "doc_id": doc_id,
                    "missing_in_ledger": sorted(
                        set(expected_disambiguation_ids)
                        - set(actual_disambiguation_ids)
                    ),
                    "extra_in_ledger": sorted(
                        set(actual_disambiguation_ids)
                        - set(expected_disambiguation_ids)
                    ),
                }
            )

    ledger_keys = {
        (row["term"], row["sense_id"]) for row in ledger["disambiguations"]
    }
    for entry in sorted(
        index.get("entries", []),
        key=lambda item: (item["term"], item["sense_id"]),
    ):
        key = (entry["term"], entry["sense_id"])
        if key not in ledger_keys:
            _fail(
                {
                    "type": "index_term_without_disambiguation",
                    "term": entry["term"],
                    "sense_id": entry["sense_id"],
                }
            )

    for row in sorted(
        ledger["disambiguations"],
        key=lambda item: (item["doc_id"], item["candidate_id"]),
    ):
        breakdown = json.loads(row["score_breakdown_json"])
        recomputed = _recompute_disambiguation_score(breakdown)
        if abs(recomputed - float(row["disambiguation_score"])) > 1e-12:
            _fail(
                {
                    "type": "disambiguation_score_not_recomputable",
                    "doc_id": row["doc_id"],
                    "candidate_id": row["candidate_id"],
                    "recorded": row["disambiguation_score"],
                    "recomputed": recomputed,
                }
            )

    recomputed_index = build_index_from_ledger_rows(
        ledger["disambiguations"],
        config,
        selected_path=meta.get("selected_path", index.get("selected_path")),
        rule_version=meta.get("rule_version", index.get("rule_version")),
        glossary_version=meta.get(
            "glossary_version", index.get("glossary_version")
        ),
    )
    if recomputed_index["entries"] != index.get("entries", []):
        expected_by_key = {
            (entry["term"], entry["sense_id"]): entry
            for entry in recomputed_index["entries"]
        }
        actual_by_key = {
            (entry["term"], entry["sense_id"]): entry
            for entry in index.get("entries", [])
        }
        for key in sorted(set(expected_by_key) | set(actual_by_key)):
            expected = expected_by_key.get(key)
            actual = actual_by_key.get(key)
            if expected != actual:
                _fail(
                    {
                        "type": "index_statistic_mismatch",
                        "term": key[0],
                        "sense_id": key[1],
                        "recomputed": expected,
                        "index": actual,
                    }
                )
    if recomputed_index["entry_count"] != index.get("entry_count") or (
        recomputed_index["occurrence_count"] != index.get("occurrence_count")
    ):
        _fail(
            {
                "type": "index_totals_mismatch",
                "recomputed": {
                    "entry_count": recomputed_index["entry_count"],
                    "occurrence_count": recomputed_index["occurrence_count"],
                },
                "index": {
                    "entry_count": index.get("entry_count"),
                    "occurrence_count": index.get("occurrence_count"),
                },
            }
        )

    return {
        "ok": True,
        "document_count": len(expected_doc_ids),
        "candidate_count": len(ledger["candidates"]),
        "disambiguation_count": len(ledger["disambiguations"]),
        "tradeoff_count": len(ledger["tradeoffs"]),
        "index_entry_count": index.get("entry_count"),
        "index_occurrence_count": index.get("occurrence_count"),
    }

