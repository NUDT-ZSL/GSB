"""SQLite ledger materialization and logical row export."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .features import document_feature_counts
from .util import canonical_json, checksum_object


SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY,
    shard_id TEXT NOT NULL,
    para_no INTEGER NOT NULL,
    char_len INTEGER NOT NULL,
    text_checksum TEXT NOT NULL,
    selected_path TEXT NOT NULL
);
CREATE TABLE candidates (
    doc_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    term_id TEXT NOT NULL,
    entry_no INTEGER NOT NULL,
    term TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    length_tier TEXT NOT NULL,
    match_rule_version TEXT NOT NULL,
    glossary_version TEXT NOT NULL,
    confidence REAL NOT NULL,
    score_breakdown_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    PRIMARY KEY (doc_id, candidate_id)
);
CREATE TABLE disambiguations (
    doc_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    term TEXT NOT NULL,
    term_id TEXT NOT NULL,
    entry_no INTEGER NOT NULL,
    sense_id INTEGER NOT NULL,
    definition TEXT NOT NULL,
    disambiguation_score REAL NOT NULL,
    candidate_confidence REAL NOT NULL,
    selected_rule_version TEXT NOT NULL,
    path TEXT NOT NULL,
    score_breakdown_json TEXT NOT NULL,
    candidate_score_breakdown_json TEXT NOT NULL,
    PRIMARY KEY (doc_id, candidate_id),
    FOREIGN KEY (doc_id, candidate_id)
        REFERENCES candidates(doc_id, candidate_id)
);
CREATE TABLE tradeoffs (
    doc_id TEXT PRIMARY KEY,
    selected_path TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    context_feature_count INTEGER NOT NULL,
    exact_comparison_estimate INTEGER NOT NULL,
    approximate_comparison_estimate INTEGER NOT NULL,
    admitted INTEGER NOT NULL,
    reason_code TEXT,
    degradation_category TEXT,
    trigger_budget TEXT,
    observations_json TEXT NOT NULL
);
"""


def write_ledger(
    path_text: str | Path,
    documents: list[dict[str, Any]],
    scored_documents: list[dict[str, Any]],
    disambiguated_documents: list[dict[str, Any]],
    features: dict[str, Any],
    path_decision: dict[str, Any],
    budget_plan: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".sqlite3", dir=str(path.parent)
    )
    os.close(fd)
    try:
        connection = sqlite3.connect(temp_name)
        try:
            connection.executescript(SCHEMA)
            feature_counts = document_feature_counts(documents, features)
            budget_by_doc = {
                item["doc_id"]: item for item in budget_plan["decisions"]
            }
            scored_by_doc = {
                item["doc_id"]: item["candidates"] for item in scored_documents
            }
            disambiguation_by_doc = {
                item["doc_id"]: item for item in disambiguated_documents
            }
            meta = {
                "schema_version": "ledger-v1",
                "selected_path": path_decision["selected_path"],
                "glossary_version": config.get("glossary_version"),
                "rule_version": config.get("rule_version"),
                "doc_count": len(documents),
            }
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in sorted(meta.items())],
            )

            doc_rows = []
            candidate_rows = []
            disambiguation_rows = []
            tradeoff_rows = []
            for doc in sorted(documents, key=lambda item: item["doc_id"]):
                doc_id = doc["doc_id"]
                doc_rows.append(
                    (
                        doc_id,
                        doc["shard_id"],
                        doc["para_no"],
                        doc["char_len"],
                        doc["text_checksum"],
                        path_decision["selected_path"],
                    )
                )
                for candidate in scored_by_doc.get(doc_id, []):
                    candidate_rows.append(
                        (
                            doc_id,
                            candidate["candidate_id"],
                            candidate["term_id"],
                            candidate["entry_no"],
                            candidate["term"],
                            candidate["start"],
                            candidate["end"],
                            candidate["length_tier"],
                            candidate["match_rule_version"],
                            candidate["glossary_version"],
                            float(candidate["confidence"]),
                            canonical_json(candidate["score_breakdown"]),
                            canonical_json(candidate["context"]),
                        )
                    )
                doc_disambiguation = disambiguation_by_doc[doc_id]
                for result in doc_disambiguation["results"]:
                    disambiguation_rows.append(
                        (
                            doc_id,
                            result["candidate_id"],
                            result["term"],
                            result["term_id"],
                            result["entry_no"],
                            result["sense_id"],
                            result["definition"],
                            float(result["disambiguation_score"]),
                            float(result["candidate_confidence"]),
                            result["selected_rule_version"],
                            result["path"],
                            canonical_json(result["score_breakdown"]),
                            canonical_json(result["candidate_score_breakdown"]),
                        )
                    )
                feature_count = feature_counts.get(doc_id, 0)
                candidate_count = len(scored_by_doc.get(doc_id, []))
                budget_decision = budget_by_doc[doc_id]
                degradation = budget_plan.get("degradation") or {}
                tradeoff_rows.append(
                    (
                        doc_id,
                        path_decision["selected_path"],
                        candidate_count,
                        feature_count,
                        candidate_count * max(1, feature_count),
                        min(
                            candidate_count,
                            path_decision["approximate_limits"][
                                "max_candidates_per_doc"
                            ],
                        )
                        * min(
                            max(1, feature_count),
                            path_decision["approximate_limits"][
                                "max_features_per_candidate"
                            ],
                        ),
                        1 if budget_decision["admitted"] else 0,
                        budget_decision["reason_code"],
                        degradation.get("category"),
                        degradation.get("trigger_budget"),
                        canonical_json(
                            {
                                "adaptive": path_decision["observations"],
                                "budget": budget_decision["observations"],
                                "reason_codes": path_decision["reason_codes"],
                            }
                        ),
                    )
                )
            connection.executemany(
                "INSERT INTO documents(doc_id, shard_id, para_no, char_len, text_checksum, selected_path) VALUES (?, ?, ?, ?, ?, ?)",
                doc_rows,
            )
            connection.executemany(
                "INSERT INTO candidates(doc_id, candidate_id, term_id, entry_no, term, start, end, length_tier, match_rule_version, glossary_version, confidence, score_breakdown_json, context_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                candidate_rows,
            )
            connection.executemany(
                "INSERT INTO disambiguations(doc_id, candidate_id, term, term_id, entry_no, sense_id, definition, disambiguation_score, candidate_confidence, selected_rule_version, path, score_breakdown_json, candidate_score_breakdown_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                disambiguation_rows,
            )
            connection.executemany(
                "INSERT INTO tradeoffs(doc_id, selected_path, candidate_count, context_feature_count, exact_comparison_estimate, approximate_comparison_estimate, admitted, reason_code, degradation_category, trigger_budget, observations_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tradeoff_rows,
            )
            connection.commit()
        finally:
            connection.close()
        os.replace(temp_name, path)
        return ledger_checksum(path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def ledger_checksum(path_text: str | Path) -> dict[str, Any]:
    path = Path(path_text)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        payload: dict[str, Any] = {}
        for table, order_by in (
            ("meta", "key"),
            ("documents", "doc_id"),
            ("candidates", "doc_id, candidate_id"),
            ("disambiguations", "doc_id, candidate_id"),
            ("tradeoffs", "doc_id"),
        ):
            rows = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY {order_by}"
                )
            ]
            payload[table] = rows
        return {
            "checksum": checksum_object(payload),
            "row_counts": {
                table: len(rows) for table, rows in payload.items()
            },
        }
    finally:
        connection.close()

