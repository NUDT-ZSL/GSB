"""Input loading and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import ROUNDING_MODES, sha256_value
from .errors import E_FORMAT_CORRUPT, WorkbenchError


def load_requests(path: str | Path) -> list[dict[str, Any]]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkbenchError(
            E_FORMAT_CORRUPT, "request manifest cannot be loaded", error=str(exc)
        ) from exc
    requests: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "request manifest JSONL is corrupt",
                line=line_no,
                error=str(exc),
            ) from exc
        if not isinstance(request, dict):
            raise WorkbenchError(
                E_FORMAT_CORRUPT, "request line must be an object", line=line_no
            )
        request_id = request.get("request_id")
        turn_no = request.get("turn_no")
        query_text = request.get("query_text")
        tool_calls = request.get("tool_calls")
        if not isinstance(request_id, str) or not request_id:
            raise WorkbenchError(
                E_FORMAT_CORRUPT, "request_id is missing", line=line_no
            )
        if not isinstance(turn_no, int) or isinstance(turn_no, bool) or turn_no < 1:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "turn_no must be a positive integer",
                line=line_no,
                request_id=request_id,
            )
        if not isinstance(query_text, str) or not query_text:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "query_text is missing",
                line=line_no,
                request_id=request_id,
            )
        if not isinstance(tool_calls, list):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "tool_calls must be a list",
                line=line_no,
                request_id=request_id,
            )
        normalized_calls = []
        for index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    "tool call must be an object",
                    request_id=request_id,
                    call_index=index,
                )
            for field in (
                "tool_id",
                "arg_digest",
                "result_ref",
                "result_format_version",
            ):
                if not isinstance(call.get(field), str) or not call[field]:
                    raise WorkbenchError(
                        E_FORMAT_CORRUPT,
                        f"tool call field is missing: {field}",
                        request_id=request_id,
                        call_index=index,
                    )
            normalized_calls.append(
                {
                    "tool_id": call["tool_id"],
                    "arg_digest": call["arg_digest"],
                    "result_ref": call["result_ref"],
                    "result_format_version": call["result_format_version"],
                }
            )
        required_lanes = request.get("required_lanes", 1)
        if (
            not isinstance(required_lanes, int)
            or isinstance(required_lanes, bool)
            or required_lanes < 1
        ):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "required_lanes must be a positive integer",
                request_id=request_id,
                turn_no=turn_no,
            )
        unit_id = f"{request_id}@{turn_no}"
        if unit_id in seen_units:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "duplicate request_id and turn_no",
                unit_id=unit_id,
            )
        seen_units.add(unit_id)
        normalized = {
            "request_id": request_id,
            "turn_no": turn_no,
            "query_text": query_text,
            "tool_calls": normalized_calls,
            "required_lanes": required_lanes,
            "unit_id": unit_id,
        }
        if "seed_offset" in request:
            normalized["seed_offset"] = request["seed_offset"]
        requests.append(normalized)
    if not requests:
        raise WorkbenchError(E_FORMAT_CORRUPT, "request manifest is empty")
    turn_keys: set[tuple[str, int]] = set()
    for request in requests:
        key = (request["request_id"], request["turn_no"])
        if key in turn_keys:
            raise WorkbenchError(
                E_FORMAT_CORRUPT, "duplicate request turn", unit_id=request["unit_id"]
            )
        turn_keys.add(key)
    return requests


def load_schedule(path: str | Path) -> dict[str, Any]:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError(
            E_FORMAT_CORRUPT, "schedule config cannot be loaded", error=str(exc)
        ) from exc
    if not isinstance(config, dict):
        raise WorkbenchError(E_FORMAT_CORRUPT, "schedule config must be an object")
    precision = config.get("precision")
    retrieval = config.get("retrieval")
    scheduling = config.get("scheduling")
    if not isinstance(precision, dict):
        raise WorkbenchError(E_FORMAT_CORRUPT, "precision config is missing")
    if precision.get("mode") != "float64":
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "only the float64 precision tier is supported",
            mode=precision.get("mode"),
        )
    precision.setdefault("score_decimals", 10)
    precision.setdefault("rounding_mode", "half_even")
    precision.setdefault("subnormal_protection", True)
    precision.setdefault("block_size", 3)
    rounding_mode = precision.get("rounding_mode", "half_even")
    if rounding_mode not in ROUNDING_MODES:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "precision rounding_mode is unsupported",
            rounding_mode=rounding_mode,
        )
    for field, minimum in (("score_decimals", 0), ("block_size", 1)):
        value = precision.get(field)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < minimum
        ):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"precision {field} is out of range",
                field=field,
            )
    if not isinstance(precision.get("subnormal_protection", True), bool):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "subnormal_protection must be a boolean",
        )
    if not isinstance(retrieval, dict) or not isinstance(scheduling, dict):
        raise WorkbenchError(
            E_FORMAT_CORRUPT, "retrieval or scheduling config is missing"
        )
    retrieval.setdefault("top_k", 8)
    retrieval.setdefault("candidate_limit", retrieval["top_k"])
    retrieval.setdefault("prefix_expansion", 3)
    retrieval.setdefault("vector_dim", 24)
    retrieval.setdefault("vector_lsh_bits", 8)
    retrieval.setdefault("vector_probe_buckets", 2)
    weights = retrieval.get("weights")
    if not isinstance(weights, dict) or any(
        route not in weights for route in ("inverted", "prefix", "vector")
    ):
        raise WorkbenchError(
            E_FORMAT_CORRUPT, "retrieval route weights are missing"
        )
    for route in ("inverted", "prefix", "vector"):
        weight = weights[route]
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or weight < 0
        ):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "retrieval route weight must be non-negative",
                route=route,
            )
    for field in ("top_k", "candidate_limit", "vector_dim"):
        value = retrieval.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"retrieval {field} must be a positive integer",
                field=field,
            )
    for field in ("prefix_expansion", "vector_probe_buckets"):
        value = retrieval.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"retrieval {field} must be a non-negative integer",
                field=field,
            )
    for field in ("vector_lsh_bits",):
        value = retrieval.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"retrieval {field} must be a positive integer",
                field=field,
            )
    concurrency = scheduling.get("concurrency_limit")
    if not isinstance(concurrency, int) or concurrency < 1:
        raise WorkbenchError(
            E_FORMAT_CORRUPT, "concurrency_limit must be a positive integer"
        )
    for field in ("time_budget_ms", "memory_budget_bytes"):
        value = scheduling.get(field)
        if not isinstance(value, (int, float)) or value <= 0:
            raise WorkbenchError(
                E_FORMAT_CORRUPT, f"{field} must be positive", field=field
            )
    precedence = scheduling.get(
        "budget_precedence", ["concurrency", "time", "memory"]
    )
    if not isinstance(precedence, list) or set(precedence) != {
        "concurrency",
        "time",
        "memory",
    }:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "budget_precedence must contain concurrency, time, and memory",
        )
    arbitration = config.get("arbitration", {})
    priority = arbitration.get("priority_order", ["quality", "memory", "latency"])
    if not isinstance(priority, list) or set(priority) != {
        "quality",
        "memory",
        "latency",
    }:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "priority_order must contain quality, memory, and latency",
        )
    return config


def schedule_hash(config: dict[str, Any]) -> str:
    return sha256_value(config)


def request_hash(request: dict[str, Any]) -> str:
    return sha256_value(
        {
            "request_id": request["request_id"],
            "turn_no": request["turn_no"],
            "query_text": request["query_text"],
            "tool_calls": request["tool_calls"],
            "required_lanes": request.get("required_lanes", 1),
        }
    )

