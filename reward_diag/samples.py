from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .io_utils import canonical_json, sha256_obj, sorted_failures


REQUIRED_FIELDS = (
    "pair_id",
    "prompt",
    "chosen",
    "rejected",
    "annotator_id",
    "timestamp",
)


def read_jsonl_entries(path: str | Path, batch_id: str = "BASE") -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            entries.append(
                {
                    "source": f"base:line:{line_number}",
                    "batch_id": batch_id,
                    "raw": line.rstrip("\n"),
                }
            )
    return entries


def records_to_entries(
    records: Iterable[dict[str, Any]], batch_id: str, field: str
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        entries.append(
            {
                "source": f"batch:{batch_id}:{field}:{index}",
                "batch_id": batch_id,
                "raw": canonical_json(record),
            }
        )
    return entries


def _pair_id_from_raw(raw: str) -> str | None:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(value, dict) and isinstance(value.get("pair_id"), str):
        return value["pair_id"]
    return None


def _failure(
    entry: dict[str, Any],
    pair_id: str | None,
    code: str,
    reason: str,
    exception_type: str | None = None,
) -> dict[str, Any]:
    return {
        "pair_id": pair_id,
        "batch_id": entry.get("batch_id", "BASE"),
        "stage": "load",
        "business_code": code,
        "exception_type": exception_type,
        "reason": reason,
        "source": entry.get("source"),
    }


def _parse_timestamp(value: str) -> str:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return parsed.isoformat()


def load_records(raw_entries: list[dict[str, Any]]) -> dict[str, Any]:
    valid: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    record_fingerprints: dict[str, str] = {}

    for entry in raw_entries:
        pair_id: str | None = None
        try:
            value = json.loads(entry.get("raw", ""))
        except (json.JSONDecodeError, TypeError) as exc:
            failures.append(
                _failure(entry, None, "E_SAMPLE_INVALID", "invalid_json", type(exc).__name__)
            )
            continue

        if not isinstance(value, dict):
            failures.append(
                _failure(entry, None, "E_SAMPLE_INVALID", "record_not_object")
            )
            continue

        candidate_id = value.get("pair_id")
        if isinstance(candidate_id, str):
            pair_id = candidate_id

        missing = [field for field in REQUIRED_FIELDS if field not in value]
        if missing:
            failures.append(
                _failure(
                    entry,
                    pair_id,
                    "E_SAMPLE_INVALID",
                    "missing_fields:" + ",".join(missing),
                )
            )
            continue

        if not isinstance(candidate_id, str) or not candidate_id.strip():
            failures.append(
                _failure(entry, None, "E_SAMPLE_INVALID", "invalid_pair_id")
            )
            continue

        text_fields = ("prompt", "chosen", "rejected", "annotator_id", "timestamp")
        invalid_types = [field for field in text_fields if not isinstance(value[field], str)]
        if invalid_types:
            failures.append(
                _failure(
                    entry,
                    pair_id,
                    "E_SAMPLE_INVALID",
                    "invalid_field_types:" + ",".join(invalid_types),
                )
            )
            continue

        if not value["annotator_id"].strip():
            failures.append(
                _failure(entry, pair_id, "E_SAMPLE_INVALID", "empty_annotator_id")
            )
            continue

        try:
            normalized_timestamp = _parse_timestamp(value["timestamp"])
        except (ValueError, TypeError) as exc:
            failures.append(
                _failure(
                    entry,
                    pair_id,
                    "E_SAMPLE_INVALID",
                    "invalid_timestamp",
                    type(exc).__name__,
                )
            )
            continue

        empty_text = [
            field for field in ("prompt", "chosen", "rejected") if not value[field].strip()
        ]
        if empty_text:
            failures.append(
                _failure(
                    entry,
                    pair_id,
                    "E_EMPTY_TEXT",
                    "empty_text:" + ",".join(empty_text),
                )
            )
            continue

        if pair_id in valid:
            failures.append(
                _failure(entry, pair_id, "E_SAMPLE_INVALID", "duplicate_pair_id")
            )
            continue

        record = dict(value)
        record["timestamp"] = normalized_timestamp
        valid[pair_id] = record
        record_fingerprints[pair_id] = sha256_obj(record)

    return {
        "valid": valid,
        "record_fingerprints": record_fingerprints,
        "failures": sorted_failures(failures),
    }


def data_fingerprint(raw_entries: list[dict[str, Any]], loaded: dict[str, Any]) -> str:
    valid_records = [
        loaded["valid"][pair_id] for pair_id in sorted(loaded["valid"])
    ]
    valid_ids = set(loaded["valid"])
    invalid_entries: list[dict[str, Any]] = []
    for entry in raw_entries:
        pair_id = _pair_id_from_raw(entry.get("raw", ""))
        if pair_id not in valid_ids:
            invalid_entries.append(
                {
                    "source": entry.get("source"),
                    "batch_id": entry.get("batch_id"),
                    "raw": entry.get("raw"),
                }
            )
    invalid_entries.sort(
        key=lambda item: (
            str(item.get("pair_id") or ""),
            str(item.get("source") or ""),
            str(item.get("raw") or ""),
        )
    )
    return sha256_obj({"valid": valid_records, "invalid": invalid_entries})


def pair_id_from_entry(entry: dict[str, Any]) -> str | None:
    return _pair_id_from_raw(entry.get("raw", ""))


def corpus_fingerprint(raw_entries: list[dict[str, Any]]) -> str:
    return data_fingerprint(raw_entries, load_records(raw_entries))


def record_fingerprint(record: dict[str, Any]) -> str:
    return sha256_obj(record)
