"""Stage 1: deterministic JSONL ingestion and field validation."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from ..errors import E_BAD_FIELD, E_DUP_SAMPLE
from ..utils import parse_annotation_revision, sha256_json


REQUIRED_FIELDS = (
    "sample_id",
    "instruction",
    "input",
    "output",
    "source_tag",
    "quality_flag",
    "lang",
    "annotation_revision",
)


def _read_raw_rows(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row: dict[str, Any] = {
                "__line_no": line_no,
                "__raw_ok": True,
                "__raw_error": None,
            }
            try:
                parsed = json.loads(line)
                if not isinstance(parsed, dict):
                    raise ValueError("JSONL row must be a JSON object")
                row.update(parsed)
                row["__raw_checksum"] = sha256_json(parsed)
            except (json.JSONDecodeError, ValueError) as exc:
                row["__raw_ok"] = False
                row["__raw_error"] = str(exc)
            rows.append(row)
    return rows


def _check_string_field(
    row: dict[str, Any],
    field: str,
    limits: dict[str, Any],
) -> tuple[str | None, str | None]:
    value = row.get(field)
    if not isinstance(value, str):
        return None, f"{field} must be a string"
    normalized = " ".join(value.splitlines()).strip()
    limit = limits.get(field, {})
    min_chars = int(limit.get("min_chars", 0))
    max_chars = int(limit.get("max_chars", 10**9))
    if len(normalized) < min_chars:
        return None, (
            f"{field} length {len(normalized)} is below minimum {min_chars}"
        )
    if len(normalized) > max_chars:
        return None, (
            f"{field} length {len(normalized)} exceeds maximum {max_chars}"
        )
    return normalized, None


def _row_sort_key(row: dict[str, Any]) -> tuple[str, int]:
    sample_id = row.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id:
        return ("\uffff", int(row["__line_no"]))
    return (sample_id, int(row["__line_no"]))


def run_ingest(
    raw_path: str | Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    rows = sorted(_read_raw_rows(raw_path), key=_row_sort_key)
    field_limits = config["field_limits"]
    languages = set(config["languages"])
    source_tags = set(config["source_tags"])
    quality_flags = set(config["quality_flags"])
    id_counts = Counter(
        row["sample_id"]
        for row in rows
        if row.get("__raw_ok") and isinstance(row.get("sample_id"), str)
    )
    duplicate_ids = {sid for sid, count in id_counts.items() if count > 1}

    samples: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    completed_ids: list[str] = []
    for row in rows:
        line_no = int(row["__line_no"])
        raw_id = row.get("sample_id")
        display_id = (
            raw_id
            if isinstance(raw_id, str) and raw_id
            else f"__line_{line_no:06d}"
        )
        completed_ids.append(display_id)
        if not row.get("__raw_ok"):
            statuses.append(
                {
                    "sample_id": display_id,
                    "line_no": line_no,
                    "status": "error",
                    "code": E_BAD_FIELD,
                    "errors": [row.get("__raw_error")],
                }
            )
            continue
        if display_id in duplicate_ids:
            statuses.append(
                {
                    "sample_id": display_id,
                    "line_no": line_no,
                    "status": "error",
                    "code": E_DUP_SAMPLE,
                    "errors": [
                        f"sample_id appears {id_counts[display_id]} times; "
                        "all occurrences are rejected"
                    ],
                }
            )
            continue

        errors: list[str] = []
        normalized: dict[str, Any] = {}
        for field in REQUIRED_FIELDS:
            if field not in row:
                errors.append(f"missing required field: {field}")
                continue
            if field == "annotation_revision":
                continue
            value, error = _check_string_field(row, field, field_limits)
            if error:
                errors.append(error)
            else:
                normalized[field] = value
        if "lang" in normalized and normalized["lang"] not in languages:
            errors.append(
                f"lang {normalized['lang']!r} is outside controlled set "
                f"{sorted(languages)}"
            )
        if (
            "source_tag" in normalized
            and normalized["source_tag"] not in source_tags
        ):
            errors.append(
                f"source_tag {normalized['source_tag']!r} is outside controlled set"
            )
        if (
            "quality_flag" in normalized
            and normalized["quality_flag"] not in quality_flags
        ):
            errors.append(
                f"quality_flag {normalized['quality_flag']!r} is outside controlled set"
            )
        revision_raw = row.get("annotation_revision")
        if "annotation_revision" not in row:
            errors.append("missing required field: annotation_revision")
        else:
            revision_limit = field_limits.get("annotation_revision", {})
            if isinstance(revision_raw, str):
                max_chars = int(revision_limit.get("max_chars", 10**9))
                if len(revision_raw) > max_chars:
                    errors.append(
                        "annotation_revision exceeds maximum character length"
                    )
            try:
                normalized["annotation_revision"] = parse_annotation_revision(
                    revision_raw
                )
                normalized["annotation_revision_raw"] = revision_raw
            except ValueError as exc:
                errors.append(f"annotation_revision: {exc}")

        if errors:
            statuses.append(
                {
                    "sample_id": display_id,
                    "line_no": line_no,
                    "status": "error",
                    "code": E_BAD_FIELD,
                    "errors": errors,
                }
            )
            continue
        normalized["__line_no"] = line_no
        normalized["__raw_checksum"] = row["__raw_checksum"]
        samples.append(normalized)
        statuses.append(
            {
                "sample_id": display_id,
                "line_no": line_no,
                "status": "ok",
                "code": None,
                "errors": [],
            }
        )

    samples.sort(key=lambda item: item["sample_id"])
    statuses.sort(key=lambda item: (item["sample_id"], item["line_no"]))
    unique_completed = sorted(set(completed_ids))
    valid_ids = [sample["sample_id"] for sample in samples]
    return {
        "stage": "ingest",
        "samples": samples,
        "statuses": statuses,
        "summary": {
            "total_rows": len(rows),
            "valid_rows": len(samples),
            "invalid_rows": len(rows) - len(samples),
            "duplicate_ids": sorted(duplicate_ids),
            "error_counts": dict(
                sorted(Counter(item["code"] for item in statuses if item["code"]).items())
            ),
        },
        "completed_sample_ids": unique_completed,
        "valid_sample_ids": valid_ids,
        "expected_sample_ids": unique_completed,
    }

