from __future__ import annotations

from typing import Any

from .errors import (
    BusinessError,
    E_BAD_SAMPLE,
    E_SAMPLE_NOT_MONOTONIC,
)
from .normalize import normalize_sql
from .util import canonical_json, is_int, read_json_file, sha256_json


SAMPLE_REQUIRED = {
    "sample_seq",
    "collected_at_ms",
    "sql",
    "template_id",
    "duration_ms",
    "rows_scanned",
    "rows_returned",
    "index_hit",
    "session_id",
}


def _bad(seq: Any, field: str, message: str, value: Any = None) -> BusinessError:
    details = {"sample_seq": seq, "field": field, "message": message}
    if value is not None:
        details["value"] = value
    return BusinessError(E_BAD_SAMPLE, message, details)


def validate_sample_document(document: Any, *, path: str | None = None) -> tuple[list[dict[str, Any]], str]:
    if isinstance(document, list):
        raw_samples = document
    elif isinstance(document, dict) and "samples" in document:
        raw_samples = document["samples"]
    else:
        raise BusinessError(
            E_BAD_SAMPLE,
            "sample document must be an array or an object with a samples array",
            {"path": path},
        )
    if not isinstance(raw_samples, list):
        raise BusinessError(E_BAD_SAMPLE, "samples must be an array", {"path": path})
    validated: list[dict[str, Any]] = []
    previous_seq = 0
    for index, sample in enumerate(raw_samples):
        if not isinstance(sample, dict):
            raise BusinessError(E_BAD_SAMPLE, "each sample must be an object", {"path": path, "index": index})
        missing = sorted(SAMPLE_REQUIRED - set(sample))
        if missing:
            raise BusinessError(
                E_BAD_SAMPLE,
                "sample is missing required fields",
                {"path": path, "index": index, "missing": missing, "sample_seq": sample.get("sample_seq")},
            )
        seq = sample["sample_seq"]
        if not is_int(seq) or seq <= 0:
            raise _bad(seq, "sample_seq", "sample_seq must be a positive integer", seq)
        if seq <= previous_seq:
            raise BusinessError(
                E_SAMPLE_NOT_MONOTONIC,
                "sample_seq must be strictly increasing",
                {"path": path, "index": index, "previous": previous_seq, "sample_seq": seq},
            )
        previous_seq = seq
        collected_at_ms = sample["collected_at_ms"]
        if not is_int(collected_at_ms) or collected_at_ms < 0:
            raise _bad(seq, "collected_at_ms", "collected_at_ms must be a non-negative integer", collected_at_ms)
        sql = sample["sql"]
        if not isinstance(sql, str):
            raise _bad(seq, "sql", "sql must be a string", sql)
        try:
            normalized = normalize_sql(sql)
        except BusinessError as exc:
            raise BusinessError(
                exc.code,
                exc.message,
                {
                    **(exc.details or {}),
                    "path": path,
                    "index": index,
                    "sample_seq": seq,
                    "field": "sql",
                },
            ) from exc
        template_id = sample["template_id"]
        if not isinstance(template_id, str):
            raise _bad(seq, "template_id", "template_id must be a string", template_id)
        if template_id != normalized.template_id:
            raise BusinessError(
                E_BAD_SAMPLE,
                "template_id does not match deterministic normalization",
                {
                    "sample_seq": seq,
                    "field": "template_id",
                    "declared": template_id,
                    "actual": normalized.template_id,
                    "template_sql": normalized.template_sql,
                },
            )
        for field in ("duration_ms", "rows_scanned", "rows_returned"):
            value = sample[field]
            if not is_int(value) or value < 0:
                raise _bad(seq, field, f"{field} must be a non-negative integer", value)
        if sample["rows_returned"] > sample["rows_scanned"]:
            raise _bad(
                seq,
                "rows_returned",
                "rows_returned cannot exceed rows_scanned",
                sample["rows_returned"],
            )
        index_hit = sample["index_hit"]
        if index_hit is not None and not isinstance(index_hit, str):
            raise _bad(seq, "index_hit", "index_hit must be a string or null", index_hit)
        session_id = sample["session_id"]
        if not isinstance(session_id, str) or not session_id:
            raise _bad(seq, "session_id", "session_id must be a non-empty string", session_id)
        validated.append(
            {
                "sample_seq": seq,
                "collected_at_ms": collected_at_ms,
                "sql": sql,
                "template_id": normalized.template_id,
                "template_sql": normalized.template_sql,
                "structure": normalized.structure,
                "duration_ms": sample["duration_ms"],
                "rows_scanned": sample["rows_scanned"],
                "rows_returned": sample["rows_returned"],
                "index_hit": index_hit,
                "session_id": session_id,
            }
        )
    digest = sha256_json(validated)
    for sample in validated:
        sample["sample_digest"] = sha256_json(
            {key: value for key, value in sample.items() if key != "sample_digest"}
        )
    return validated, digest


def load_and_validate_samples(path: str) -> tuple[list[dict[str, Any]], str]:
    document = read_json_file(path, E_BAD_SAMPLE)
    return validate_sample_document(document, path=path)


def sample_storage_digest(sample: dict[str, Any], window_start_ms: int) -> str:
    return sha256_json(
        {
            "sample_seq": sample["sample_seq"],
            "collected_at_ms": sample["collected_at_ms"],
            "sql": sample["sql"],
            "template_id": sample["template_id"],
            "duration_ms": sample["duration_ms"],
            "rows_scanned": sample["rows_scanned"],
            "rows_returned": sample["rows_returned"],
            "index_hit": sample["index_hit"],
            "session_id": sample["session_id"],
            "window_start_ms": window_start_ms,
        }
    )


def canonical_sample(sample: dict[str, Any]) -> str:
    return canonical_json(sample)
