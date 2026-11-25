"""Input, weight, revision, and resource validation."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import (
    E_DUPLICATE_CASE_ID,
    E_EXPERT_COUNT_MISMATCH,
    E_EXPERT_DIMENSION_MISMATCH,
    E_GATE_DIMENSION_MISMATCH,
    E_INVALID_BLOCK_SIZE,
    E_INVALID_INPUT,
    E_RESOURCE_LIMIT,
    E_REVISION_REGRESSION,
    E_TOKEN_DIMENSION_MISMATCH,
    MoeError,
)
from .models import CaseSpec, ExpertBank, ResourceLimits


_REQUIRED_FIELDS = (
    "case_id",
    "token_len",
    "hidden_dim",
    "num_experts",
    "top_k",
    "capacity_factor",
    "router_revision",
    "block_size",
    "dtype",
    "seed",
)


def _business_error(
    code: str,
    message: str,
    *,
    case_id: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> MoeError:
    return MoeError(
        code=code,
        message=message,
        case_id=case_id,
        field=field,
        details=details or {},
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_case(case: CaseSpec) -> CaseSpec:
    if not isinstance(case.case_id, str) or not case.case_id.strip():
        raise _business_error(
            E_INVALID_INPUT,
            "case_id must be a non-empty string",
            field="case_id",
        )
    for field in ("token_len", "hidden_dim", "num_experts"):
        value = getattr(case, field)
        if not _is_int(value) or value <= 0:
            raise _business_error(
                E_INVALID_INPUT,
                f"{field} must be a positive integer",
                case_id=case.case_id,
                field=field,
                details={"value": value},
            )
    if not _is_int(case.top_k) or not (1 <= case.top_k <= case.num_experts):
        raise _business_error(
            E_INVALID_INPUT,
            "top_k must be between 1 and num_experts",
            case_id=case.case_id,
            field="top_k",
            details={"top_k": case.top_k, "num_experts": case.num_experts},
        )
    if (
        not isinstance(case.capacity_factor, (int, float))
        or isinstance(case.capacity_factor, bool)
        or not math.isfinite(float(case.capacity_factor))
        or float(case.capacity_factor) <= 0.0
    ):
        raise _business_error(
            E_INVALID_INPUT,
            "capacity_factor must be a positive finite number",
            case_id=case.case_id,
            field="capacity_factor",
            details={"value": case.capacity_factor},
        )
    if not _is_int(case.router_revision) or case.router_revision < 0:
        raise _business_error(
            E_INVALID_INPUT,
            "router_revision must be a non-negative integer",
            case_id=case.case_id,
            field="router_revision",
            details={"value": case.router_revision},
        )
    if not _is_int(case.block_size) or case.block_size <= 0:
        raise _business_error(
            E_INVALID_BLOCK_SIZE,
            "block_size must be a positive integer",
            case_id=case.case_id,
            field="block_size",
            details={"value": case.block_size},
        )
    if case.dtype not in {"float32", "float64"}:
        raise _business_error(
            E_INVALID_INPUT,
            "dtype must be float32 or float64",
            case_id=case.case_id,
            field="dtype",
            details={"value": case.dtype},
        )
    if not _is_int(case.seed) or case.seed < 0:
        raise _business_error(
            E_INVALID_INPUT,
            "seed must be a non-negative integer",
            case_id=case.case_id,
            field="seed",
            details={"value": case.seed},
        )
    return case


def parse_case_dict(payload: dict[str, Any], *, line_number: int | None = None) -> CaseSpec:
    missing = [field for field in _REQUIRED_FIELDS if field not in payload]
    if missing:
        raise _business_error(
            E_INVALID_INPUT,
            "case record is missing required fields",
            field=missing[0],
            details={"missing_fields": missing, "line_number": line_number},
        )
    try:
        case = CaseSpec.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise _business_error(
            E_INVALID_INPUT,
            f"case record cannot be parsed: {exc}",
            field="record",
            details={"line_number": line_number},
        ) from exc
    return validate_case(case)


def load_cases(path: str | Path) -> list[CaseSpec]:
    cases: list[CaseSpec] = []
    seen: dict[str, int] = {}
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _business_error(
                    E_INVALID_INPUT,
                    f"invalid JSONL at line {line_number}: {exc.msg}",
                    field="record",
                    details={"line_number": line_number, "column": exc.colno},
                ) from exc
            if not isinstance(payload, dict):
                raise _business_error(
                    E_INVALID_INPUT,
                    "each case record must be a JSON object",
                    field="record",
                    details={"line_number": line_number},
                )
            case = parse_case_dict(payload, line_number=line_number)
            if case.case_id in seen:
                raise _business_error(
                    E_DUPLICATE_CASE_ID,
                    "duplicate case_id in case manifest",
                    case_id=case.case_id,
                    field="case_id",
                    details={
                        "first_line": seen[case.case_id],
                        "duplicate_line": line_number,
                    },
                )
            seen[case.case_id] = line_number
            cases.append(case)
    return cases


def write_cases(path: str | Path, cases: Iterable[CaseSpec]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(json.dumps(case.to_dict(), ensure_ascii=True, separators=(",", ":")))
            handle.write("\n")


def validate_tokens_and_gate(
    case: CaseSpec, tokens: np.ndarray, gate_weights: np.ndarray
) -> None:
    tokens = np.asarray(tokens)
    gate_weights = np.asarray(gate_weights)
    expected_tokens = (case.token_len, case.hidden_dim)
    if tokens.shape != expected_tokens:
        raise _business_error(
            E_TOKEN_DIMENSION_MISMATCH,
            "token tensor shape does not match token_len and hidden_dim",
            case_id=case.case_id,
            field="token_len",
            details={
                "expected_shape": list(expected_tokens),
                "actual_shape": list(tokens.shape),
            },
        )
    expected_gate = (case.hidden_dim, case.num_experts)
    if gate_weights.shape != expected_gate:
        raise _business_error(
            E_GATE_DIMENSION_MISMATCH,
            "gate weight shape must be (hidden_dim, num_experts)",
            case_id=case.case_id,
            field="gate_weights",
            details={
                "expected_shape": list(expected_gate),
                "actual_shape": list(gate_weights.shape),
            },
        )


def validate_expert_bank(case: CaseSpec, expert_bank: ExpertBank) -> None:
    if (
        len(expert_bank.weights) != case.num_experts
        or len(expert_bank.revisions) != case.num_experts
    ):
        raise _business_error(
            E_EXPERT_COUNT_MISMATCH,
            "expert weight/revision count does not match num_experts",
            case_id=case.case_id,
            field="num_experts",
            details={
                "num_experts": case.num_experts,
                "expert_weights": len(expert_bank.weights),
                "expert_revisions": len(expert_bank.revisions),
            },
        )
    expected = (case.hidden_dim, case.hidden_dim)
    for expert_id, weight in enumerate(expert_bank.weights):
        array = np.asarray(weight)
        if array.shape != expected:
            raise _business_error(
                E_EXPERT_DIMENSION_MISMATCH,
                "expert weight matrix shape must be (hidden_dim, hidden_dim)",
                case_id=case.case_id,
                field=f"expert_weights[{expert_id}]",
                details={
                    "expected_shape": list(expected),
                    "actual_shape": list(array.shape),
                },
            )


def validate_revision(previous_revision: int | None, current_revision: int) -> None:
    if previous_revision is not None and current_revision < previous_revision:
        raise MoeError(
            code=E_REVISION_REGRESSION,
            message="router_revision may not move backwards",
            field="router_revision",
            details={
                "previous_revision": previous_revision,
                "current_revision": current_revision,
            },
        )


def estimate_block_resident_bytes(
    case: CaseSpec, block_token_count: int
) -> int:
    bytes_per_float = 8
    output = block_token_count * case.hidden_dim * bytes_per_float
    gate = block_token_count * case.num_experts * bytes_per_float * 2
    selected = block_token_count * case.top_k * bytes_per_float * 3
    assignments = block_token_count * case.top_k
    contributions = block_token_count * case.top_k * case.hidden_dim * bytes_per_float
    return int(output + gate + selected + assignments + contributions)


def enforce_memory_limit(
    current_bytes: int, block_token_count: int, case: CaseSpec, limits: ResourceLimits
) -> int:
    estimate = estimate_block_resident_bytes(case, block_token_count)
    projected = current_bytes + estimate
    if projected > limits.max_case_resident_bytes:
        raise MoeError(
            code=E_RESOURCE_LIMIT,
            message="single-case resident block output memory limit exceeded",
            case_id=case.case_id,
            field="max_case_resident_bytes",
            details={
                "current_resident_bytes": current_bytes,
                "block_estimate_bytes": estimate,
                "projected_resident_bytes": projected,
                "limit_bytes": limits.max_case_resident_bytes,
            },
        )
    return projected
