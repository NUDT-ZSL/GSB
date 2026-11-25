"""Deterministic JSONL parsing and input validation."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .codes import EngineError, ErrorCode
from .hashing import canonical_hash, sha256_file


def _reject_json_constant(value: str) -> None:
    if value == "NaN":
        raise EngineError(ErrorCode.E_NAN_VALUE, "JSON NaN literal is not allowed")
    raise EngineError(
        ErrorCode.E_NON_FINITE_VALUE, f"non-finite JSON constant is not allowed: {value}"
    )


def parse_jsonl_file(path: Path) -> Tuple[List[Dict[str, Any]], str]:
    path = Path(path)
    records: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line, parse_constant=_reject_json_constant)
                except EngineError as error:
                    error.details["line_number"] = line_number
                    raise
                except json.JSONDecodeError as error:
                    raise EngineError(
                        ErrorCode.E_JSON_PARSE,
                        f"invalid JSON on line {line_number}: {error.msg}",
                        details={"line_number": line_number, "position": error.pos},
                    )
                if not isinstance(record, dict):
                    raise EngineError(
                        ErrorCode.E_INVALID_INPUT,
                        f"line {line_number} must contain a JSON object",
                        details={"line_number": line_number},
                    )
                records.append(record)
    except OSError as error:
        raise EngineError(
            ErrorCode.E_INVALID_INPUT, f"cannot read sample file: {error}", {"path": str(path)}
        )
    return records, sha256_file(path)


def _row_id_sort_key(row_id: Any, row_id_type: str):
    if row_id_type == "integer":
        return (0, int(row_id))
    return (1, str(row_id))


def detect_row_id_type(row_ids: List[Any]) -> str:
    if all(isinstance(value, int) and not isinstance(value, bool) for value in row_ids):
        return "integer"
    if all(isinstance(value, str) for value in row_ids):
        return "string"
    raise EngineError(
        ErrorCode.E_TYPE_ERROR,
        "row_id values must be all integers or all strings",
        {"row_ids": row_ids[:20]},
    )


def find_duplicate_row_ids(records: List[Mapping[str, Any]]) -> List[Any]:
    counts: Dict[Any, int] = {}
    for record in records:
        if "row_id" not in record:
            continue
        row_id = record["row_id"]
        key = canonical_hash(row_id)
        counts[key] = counts.get(key, 0) + 1
    duplicates: List[Any] = []
    seen_keys = set()
    for record in records:
        row_id = record.get("row_id")
        key = canonical_hash(row_id)
        if counts.get(key, 0) > 1 and key not in seen_keys:
            duplicates.append(row_id)
            seen_keys.add(key)
    return duplicates


def validate_row(
    row: Mapping[str, Any], config: Mapping[str, Any], *, row_index: Optional[int] = None
) -> Dict[str, Any]:
    if not isinstance(row, Mapping):
        raise EngineError(
            ErrorCode.E_INVALID_INPUT,
            "sample row must be an object",
            details={"row_index": row_index},
        )
    if "row_id" not in row:
        raise EngineError(
            ErrorCode.E_MISSING_FIELD, "row_id is required", details={"row_index": row_index}
        )
    row_id = row["row_id"]
    if isinstance(row_id, bool) or not isinstance(row_id, (int, str)):
        raise EngineError(
            ErrorCode.E_TYPE_ERROR,
            "row_id must be an integer or string",
            row_id=row_id,
            details={"row_index": row_index},
        )

    label_column = config["label_column"]
    required = list(config["numeric_columns"]) + list(config["categorical_columns"]) + [
        label_column
    ]
    for column in required:
        if column not in row:
            raise EngineError(
                ErrorCode.E_MISSING_FIELD,
                f"missing required column: {column}",
                row_id=row_id,
                details={"column": column},
            )

    for column in config["numeric_columns"]:
        value = row[column]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EngineError(
                ErrorCode.E_TYPE_ERROR,
                f"numeric column {column!r} must contain a number",
                row_id=row_id,
                details={"column": column, "value": value},
            )
        if isinstance(value, float) and math.isnan(value):
            raise EngineError(
                ErrorCode.E_NAN_VALUE,
                f"numeric column {column!r} contains NaN",
                row_id=row_id,
                details={"column": column},
            )
        if not math.isfinite(float(value)):
            raise EngineError(
                ErrorCode.E_NON_FINITE_VALUE,
                f"numeric column {column!r} contains a non-finite value",
                row_id=row_id,
                details={"column": column, "value": value},
            )

    for column in config["categorical_columns"]:
        value = row[column]
        if not isinstance(value, str):
            raise EngineError(
                ErrorCode.E_TYPE_ERROR,
                f"categorical column {column!r} must be a string",
                row_id=row_id,
                details={"column": column, "value": value},
            )
        allowed = set(config["vocabulary"][column])
        if value not in allowed:
            raise EngineError(
                ErrorCode.E_CATEGORY_VIOLATION,
                f"category {value!r} is outside controlled vocabulary for {column!r}",
                row_id=row_id,
                details={
                    "column": column,
                    "value": value,
                    "allowed": config["vocabulary"][column],
                },
            )

    label = row[label_column]
    if isinstance(label, bool) or not isinstance(label, (int, float)):
        raise EngineError(
            ErrorCode.E_TYPE_ERROR,
            "label must be numeric",
            row_id=row_id,
            details={"column": label_column, "value": label},
        )
    if not math.isfinite(float(label)):
        raise EngineError(
            ErrorCode.E_NON_FINITE_VALUE,
            "label must be finite",
            row_id=row_id,
            details={"value": label},
        )
    return dict(row)


def validate_samples(
    records: List[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    raw_input_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    if not records:
        raise EngineError(ErrorCode.E_INVALID_INPUT, "sample JSONL contains no rows")

    duplicates = find_duplicate_row_ids(records)
    if duplicates:
        raise EngineError(
            ErrorCode.E_DUP_INPUT,
            "duplicate row_id values are not allowed",
            details={"duplicate_row_ids": duplicates},
        )

    row_ids = [record.get("row_id") for record in records]
    row_id_type = detect_row_id_type(row_ids)
    ordered = sorted(
        (dict(record) for record in records),
        key=lambda row: _row_id_sort_key(row["row_id"], row_id_type),
    )

    validated_rows: List[Dict[str, Any]] = []
    for index, row in enumerate(ordered):
        validate_row(row, config, row_index=index)
        validated_rows.append(dict(row))

    sample_content_hash = canonical_hash({"rows": validated_rows})
    summary = {
        "row_count": len(validated_rows),
        "row_id_type": row_id_type,
        "first_row_id": validated_rows[0]["row_id"],
        "last_row_id": validated_rows[-1]["row_id"],
        "numeric_columns": list(config["numeric_columns"]),
        "categorical_columns": list(config["categorical_columns"]),
        "sample_content_hash": sample_content_hash,
        "raw_input_sha256": raw_input_sha256,
    }
    return {
        "rows": validated_rows,
        "summary": summary,
        "sample_content_hash": sample_content_hash,
        "raw_input_sha256": raw_input_sha256,
    }


def load_and_validate(path: Path, config: Mapping[str, Any]) -> Dict[str, Any]:
    records, raw_hash = parse_jsonl_file(path)
    return validate_samples(records, config, raw_input_sha256=raw_hash)
