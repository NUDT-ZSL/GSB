"""Longest-first deterministic glossary matching."""

from __future__ import annotations

import hashlib
from typing import Any

from .util import canonical_json


def _candidate_id(
    doc_id: str,
    start: int,
    end: int,
    entry: dict[str, Any],
    glossary_version: str,
) -> str:
    material = (
        f"{doc_id}|{start}|{end}|{entry['entry_no']}|{entry['term_id']}|"
        f"{entry['rule_version']}|{glossary_version}"
    )
    return "cand-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def extract_document(
    doc: dict[str, Any],
    glossary: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    entries = glossary["entries"]
    by_first: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_first.setdefault(entry["term"][0], []).append(entry)
    for values in by_first.values():
        values.sort(key=lambda entry: (-len(entry["term"]), entry["entry_no"]))

    text = doc["text"]
    candidates: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(text):
        selected = None
        for entry in by_first.get(text[cursor], []):
            term = entry["term"]
            if text.startswith(term, cursor):
                selected = entry
                break
        if selected is None:
            cursor += 1
            continue
        end = cursor + len(selected["term"])
        candidates.append(
            {
                "candidate_id": _candidate_id(
                    doc["doc_id"],
                    cursor,
                    end,
                    selected,
                    glossary["glossary_version"],
                ),
                "doc_id": doc["doc_id"],
                "para_no": doc["para_no"],
                "term_id": selected["term_id"],
                "entry_no": selected["entry_no"],
                "term": selected["term"],
                "surface": text[cursor:end],
                "start": cursor,
                "end": end,
                "length_tier": selected["length_tier"],
                "match_rule_version": selected["rule_version"],
                "glossary_version": glossary["glossary_version"],
            }
        )
        cursor = end
    candidates.sort(key=lambda c: (c["start"], c["end"], c["entry_no"]))
    return candidates


def extract_documents(
    documents: list[dict[str, Any]],
    glossary: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    result = []
    for doc in documents:
        candidates = extract_document(doc, glossary, config)
        result.append(
            {
                "doc_id": doc["doc_id"],
                "candidates": candidates,
                "candidate_checksum": hashlib.sha256(
                    canonical_json(candidates).encode("utf-8")
                ).hexdigest(),
            }
        )
    return result

