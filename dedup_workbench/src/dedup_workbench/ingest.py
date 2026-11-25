"""Deterministic JSONL shard ingestion and input validation."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .canonical import hash_json, sha256_text
from .config import parse_config
from .errors import (
    E_CONTENT_HASH_FORMAT,
    E_CONTENT_HASH_MISMATCH,
    E_DUPLICATE_DOC_ID,
    E_EMPTY_TEXT,
    E_INPUT_RECORD,
    E_JSONL_PARSE,
    E_LANG_NOT_ALLOWED,
    E_RESOURCE_LIMIT,
    WorkbenchError,
)


WHITESPACE_RE = re.compile(r"\s+", flags=re.UNICODE)
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

SOURCE_QUALITY = {
    "official": 0.98,
    "reference": 0.93,
    "book": 0.88,
    "wiki": 0.82,
    "article": 0.74,
    "web": 0.58,
    "crawl": 0.36,
    "synthetic": 0.65,
}

REQUIRED_FIELDS = ("shard_id", "doc_id", "text", "lang", "source", "token_estimate", "content_hash")


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return WHITESPACE_RE.sub(" ", normalized).strip()


def content_hash_for(text: str) -> str:
    return sha256_text(normalize_text(text))


def _jsonl_files(source: str | Path) -> list[Path]:
    path = Path(source)
    if path.is_dir():
        return sorted(path.glob("*.jsonl"), key=lambda item: item.name)
    return [path]


def _quality_score(record: dict[str, Any]) -> float:
    if "quality_score" in record:
        value = record["quality_score"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0.0 <= float(value) <= 1.0:
            raise WorkbenchError(
                E_INPUT_RECORD,
                "quality_score must be a number in [0, 1]",
                field="quality_score",
                details={"doc_id": record.get("doc_id")},
            )
        return round(float(value), 6)
    return SOURCE_QUALITY.get(str(record.get("source")), 0.50)


def _validate_record(record: Any, *, source_file: str, line_number: int, allowed_langs: Iterable[str]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise WorkbenchError(
            E_INPUT_RECORD,
            "Each JSONL line must be an object",
            field="$",
            details={"source_file": source_file, "line": line_number},
        )

    for field in REQUIRED_FIELDS:
        if field not in record:
            raise WorkbenchError(
                E_INPUT_RECORD,
                f"Missing required field: {field}",
                field=field,
                details={"source_file": source_file, "line": line_number},
            )

    for field in ("shard_id", "doc_id", "text", "lang", "source", "content_hash"):
        if not isinstance(record[field], str):
            raise WorkbenchError(
                E_INPUT_RECORD,
                f"Field {field} must be a string",
                field=field,
                details={"source_file": source_file, "line": line_number, "actual_type": type(record[field]).__name__},
            )

    shard_id = record["shard_id"].strip()
    doc_id = record["doc_id"].strip()
    lang = record["lang"].strip()
    source = record["source"].strip()
    if not shard_id or not doc_id or not lang or not source:
        raise WorkbenchError(
            E_INPUT_RECORD,
            "Identity and controlled string fields must be non-empty after trimming",
            field="shard_id/doc_id/lang/source",
            details={"source_file": source_file, "line": line_number},
        )

    token_estimate = record["token_estimate"]
    if not isinstance(token_estimate, int) or isinstance(token_estimate, bool) or token_estimate < 0:
        raise WorkbenchError(
            E_INPUT_RECORD,
            "token_estimate must be a non-negative integer",
            field="token_estimate",
            details={"doc_id": doc_id, "line": line_number},
        )

    provided_hash = record["content_hash"].strip().lower()
    if not HEX64_RE.match(provided_hash):
        raise WorkbenchError(
            E_CONTENT_HASH_FORMAT,
            "content_hash must be a lowercase SHA-256 hexadecimal string",
            field="content_hash",
            details={"doc_id": doc_id, "line": line_number},
        )

    normalized_text = normalize_text(record["text"])
    if not normalized_text:
        raise WorkbenchError(
            E_EMPTY_TEXT,
            "Normalized text must not be empty",
            field="text",
            details={"doc_id": doc_id, "shard_id": shard_id, "line": line_number},
        )

    expected_hash = sha256_text(normalized_text)
    if provided_hash != expected_hash:
        raise WorkbenchError(
            E_CONTENT_HASH_MISMATCH,
            "content_hash does not match SHA-256 of NFKC-normalized text",
            field="content_hash",
            details={
                "doc_id": doc_id,
                "shard_id": shard_id,
                "line": line_number,
                "expected": expected_hash,
                "provided": provided_hash,
            },
        )

    allowed = tuple(allowed_langs)
    if lang not in allowed:
        raise WorkbenchError(
            E_LANG_NOT_ALLOWED,
            "lang is outside the controlled language set",
            field="lang",
            details={"doc_id": doc_id, "provided": lang, "allowed": list(allowed)},
        )

    quality_score = _quality_score(record)
    metadata = {
        key: value
        for key, value in record.items()
        if key not in {"text", "content_hash"}
    }
    metadata.update({"shard_id": shard_id, "doc_id": doc_id, "lang": lang, "source": source})
    input_hash = hash_json(
        {
            "content_hash": provided_hash,
            "metadata": metadata,
            "normalized_length": len(normalized_text),
        }
    )

    return {
        "shard_id": shard_id,
        "doc_id": doc_id,
        "lang": lang,
        "source": source,
        "token_estimate": int(token_estimate),
        "quality_score": quality_score,
        "content_hash": provided_hash,
        "text_hash": expected_hash,
        "input_hash": input_hash,
        "normalized_text": normalized_text,
        "normalized_length": len(normalized_text),
        "source_file": source_file,
        "source_line": line_number,
    }


def load_documents(source: str | Path, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if config is None:
        config = parse_config(
            {
                "chunk_signature_length": 32,
                "similarity_threshold": 0.8,
                "candidate_window": 8,
                "validation_mode": "full",
            }
        )

    files = _jsonl_files(source)
    if not files:
        raise WorkbenchError(E_JSONL_PARSE, "No JSONL input files found", field="source", details={"source": str(source)})

    documents: list[dict[str, Any]] = []
    seen_doc_ids: dict[str, dict[str, Any]] = {}

    for path in files:
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError as exc:
            raise WorkbenchError(E_JSONL_PARSE, f"Cannot open JSONL file: {exc}", field="source", details={"path": str(path)}) from exc

        with handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise WorkbenchError(
                        E_JSONL_PARSE,
                        f"Invalid JSON on line {line_number}: {exc.msg}",
                        field=f"{path.name}:{line_number}",
                        details={"path": str(path), "line": line_number},
                    ) from exc

                document = _validate_record(
                    record,
                    source_file=path.name,
                    line_number=line_number,
                    allowed_langs=config["allowed_langs"],
                )
                if document["doc_id"] in seen_doc_ids:
                    previous = seen_doc_ids[document["doc_id"]]
                    raise WorkbenchError(
                        E_DUPLICATE_DOC_ID,
                        "Duplicate doc_id values are not allowed",
                        field="doc_id",
                        details={
                            "doc_id": document["doc_id"],
                            "occurrences": [
                                {"source_file": previous["source_file"], "line": previous["source_line"]},
                                {"source_file": document["source_file"], "line": document["source_line"]},
                            ],
                        },
                    )
                seen_doc_ids[document["doc_id"]] = document
                documents.append(document)

    shard_ids = {doc["shard_id"] for doc in documents}
    max_shards = int(config["limits"]["max_shards"])
    if len(shard_ids) > max_shards:
        raise WorkbenchError(
            E_RESOURCE_LIMIT,
            "Number of shards exceeds max_shards",
            field="limits.max_shards",
            details={"actual_shards": len(shard_ids), "limit": max_shards, "shard_ids": sorted(shard_ids)},
        )

    documents.sort(key=lambda doc: (doc["shard_id"], doc["doc_id"]))
    return documents
