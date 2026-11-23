"""Validation and deterministic window assignment for the sampling stream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import (
    canonical_json,
    digest,
    require_int,
    require_list,
    require_mapping,
)
from .errors import EngineError
from .sql import normalize_sql


def window_bounds(logical_clock_ms: int, window_ms: int) -> tuple[int, int, str]:
    start = (logical_clock_ms // window_ms) * window_ms
    end = start + window_ms
    return start, end, f"w{start:012d}-{end:012d}"


def load_json_file(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise EngineError(
            "ERR_INPUT_NOT_FOUND", "input file does not exist", {"path": str(path)}
        ) from exc
    except json.JSONDecodeError as exc:
        raise EngineError(
            "ERR_INVALID_JSON",
            "input file is not valid JSON",
            {
                "path": str(path),
                "line": exc.lineno,
                "column": exc.colno,
                "message": exc.msg,
            },
        ) from exc
    except OSError as exc:
        raise EngineError(
            "ERR_INPUT_IO", "cannot read input file", {"path": str(path), "error": str(exc)}
        ) from exc


def _field(document: dict[str, Any], *names: str) -> tuple[str, Any]:
    for name in names:
        if name in document:
            return name, document[name]
    raise EngineError(
        "ERR_SAMPLE_SCHEMA",
        f"sample is missing required field {names[0]}",
        {"fields": list(names)},
    )


def validate_samples_document(
    document: Any,
    *,
    source: str = "<memory>",
    window_ms: int = 1000,
) -> list[dict[str, Any]]:
    """Validate the whole stream before any state is opened or mutated."""

    if isinstance(document, list):
        raw_samples = document
    elif isinstance(document, dict):
        if "samples" not in document:
            raise EngineError(
                "ERR_SAMPLE_SCHEMA",
                "sample stream must contain a samples array",
                {"source": source},
            )
        raw_samples = require_list(document["samples"], "ERR_SAMPLE_SCHEMA", "samples")
    else:
        raise EngineError(
            "ERR_SAMPLE_SCHEMA",
            "sample stream must be an object or array",
            {"source": source},
        )

    if not isinstance(window_ms, int) or isinstance(window_ms, bool) or window_ms <= 0:
        raise EngineError(
            "ERR_WINDOW_CONFIG", "window_ms must be a positive integer"
        )

    samples: list[dict[str, Any]] = []
    seen_sequences: set[int] = set()
    previous_sequence: int | None = None
    for index, value in enumerate(raw_samples):
        raw = require_mapping(value, "ERR_SAMPLE_SCHEMA", f"samples[{index}]")
        seq_name, seq_value = _field(raw, "seq", "sample_seq")
        seq = require_int(
            seq_value,
            "ERR_SAMPLE_SEQ",
            f"{source}:samples[{index}].{seq_name}",
            minimum=0,
        )
        if seq in seen_sequences:
            raise EngineError(
                "ERR_SAMPLE_SEQ",
                "sample sequence numbers must be unique",
                {"source": source, "seq": seq},
            )
        if previous_sequence is not None and seq <= previous_sequence:
            raise EngineError(
                "ERR_SAMPLE_SEQ",
                "sample sequence numbers must be strictly increasing",
                {"source": source, "previous": previous_sequence, "seq": seq},
            )
        seen_sequences.add(seq)
        previous_sequence = seq

        clock_name, clock_value = _field(raw, "logical_clock_ms", "clock_ms")
        clock = require_int(
            clock_value,
            "ERR_SAMPLE_CLOCK",
            f"{source}:samples[{index}].{clock_name}",
            minimum=0,
        )
        sql_name, sql_value = _field(raw, "sql", "sql_text")
        if not isinstance(sql_value, str) or not sql_value.strip():
            raise EngineError(
                "ERR_SQL_PARSE",
                "SQL text must be a non-empty string",
                {"source": source, "seq": seq, "path": sql_name},
            )
        try:
            normalized = normalize_sql(sql_value)
        except EngineError as exc:
            raise EngineError(
                exc.code,
                exc.message,
                {"source": source, "seq": seq, **exc.details},
            ) from exc
        if normalize_sql(sql_value)["canonical"] != normalized["canonical"]:
            raise EngineError(
                "ERR_NORMALIZATION",
                "SQL normalization is not deterministic",
                {"source": source, "seq": seq},
            )

        duration_name, duration_value = _field(raw, "duration_ms")
        duration = require_int(
            duration_value,
            "ERR_SAMPLE_METRIC",
            f"{source}:samples[{index}].{duration_name}",
            minimum=0,
        )
        scan_name, scan_value = _field(raw, "scanned_rows", "scan_rows")
        scanned = require_int(
            scan_value,
            "ERR_SAMPLE_METRIC",
            f"{source}:samples[{index}].{scan_name}",
            minimum=0,
        )
        return_name, return_value = _field(raw, "returned_rows", "rows_returned")
        returned = require_int(
            return_value,
            "ERR_SAMPLE_METRIC",
            f"{source}:samples[{index}].{return_name}",
            minimum=0,
        )

        index_hit = raw.get("index_hit", raw.get("index"))
        if index_hit is not None and not isinstance(index_hit, str):
            raise EngineError(
                "ERR_SAMPLE_SCHEMA",
                "index_hit must be a string or null",
                {"source": source, "seq": seq},
            )

        start, end, window_key = window_bounds(clock, window_ms)
        content = {
            "seq": seq,
            "logical_clock_ms": clock,
            "sql": sql_value,
            "duration_ms": duration,
            "scanned_rows": scanned,
            "returned_rows": returned,
            "index_hit": index_hit,
        }
        samples.append(
            {
                **content,
                "template_id": normalized["template_id"],
                "structure": normalized["structure"],
                "canonical": normalized["canonical"],
                "window_key": window_key,
                "window_start_ms": start,
                "window_end_ms": end,
                "content_digest": digest(content),
            }
        )
    return samples


def load_samples_file(
    path: str | Path, *, window_ms: int = 1000
) -> list[dict[str, Any]]:
    return validate_samples_document(
        load_json_file(path), source=str(path), window_ms=window_ms
    )


def validate_injected_clock(
    clock_ms: int | None, samples: list[dict[str, Any]]
) -> int | None:
    if clock_ms is None:
        return None
    if not isinstance(clock_ms, int) or isinstance(clock_ms, bool) or clock_ms < 0:
        raise EngineError(
            "ERR_SAMPLE_CLOCK", "injected logical clock must be a non-negative integer"
        )
    if samples and clock_ms < max(sample["logical_clock_ms"] for sample in samples):
        raise EngineError(
            "ERR_CLOCK_ORDER",
            "injected logical clock cannot precede an incoming sample clock",
            {
                "injected_clock_ms": clock_ms,
                "maximum_sample_clock_ms": max(
                    sample["logical_clock_ms"] for sample in samples
                ),
            },
        )
    return clock_ms


def sample_stream_digest(samples: list[dict[str, Any]]) -> str:
    return digest(
        [
            {
                "seq": sample["seq"],
                "logical_clock_ms": sample["logical_clock_ms"],
                "sql": sample["sql"],
                "duration_ms": sample["duration_ms"],
                "scanned_rows": sample["scanned_rows"],
                "returned_rows": sample["returned_rows"],
                "index_hit": sample["index_hit"],
            }
            for sample in samples
        ]
    )
