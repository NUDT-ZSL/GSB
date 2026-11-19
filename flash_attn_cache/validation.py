from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

from .errors import ErrorCode, Issue
from .models import (
    MASK_MODES,
    SUPPORTED_DTYPES,
    AttentionCase,
    QKVArrays,
)


def load_cases_jsonl(path: str | Path) -> list[AttentionCase]:
    cases: list[AttentionCase] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(payload, Mapping):
                raise ValueError(f"JSONL line {line_number} must be an object")
            cases.append(AttentionCase.from_dict(payload))
    return cases


def validate_case(
    case: AttentionCase,
    arrays: QKVArrays | None = None,
    *,
    require_arrays: bool = True,
) -> list[Issue]:
    issues: list[Issue] = []

    def add(code: ErrorCode, message: str, field: str) -> None:
        issues.append(
            Issue(code, message, case_id=case.case_id or None, field=field)
        )

    if not case.case_id:
        add(ErrorCode.INVALID_INPUT, "case_id must be non-empty", "case_id")
    if case.seq_len <= 0:
        add(ErrorCode.INVALID_SEQ_LEN, "seq_len must be positive", "seq_len")
    if case.head_dim <= 0:
        add(ErrorCode.INVALID_HEAD_DIM, "head_dim must be positive", "head_dim")
    if case.block_size <= 0:
        add(
            ErrorCode.INVALID_BLOCK_SIZE,
            "block_size must be positive",
            "block_size",
        )
    if case.n_heads <= 0:
        add(ErrorCode.INVALID_HEAD_COUNT, "n_heads must be positive", "n_heads")
    if case.dtype not in SUPPORTED_DTYPES:
        add(
            ErrorCode.UNSUPPORTED_DTYPE,
            f"dtype '{case.dtype}' is not supported",
            "dtype",
        )
    if case.mask_mode is not None and case.mask_mode not in MASK_MODES:
        add(
            ErrorCode.CAUSAL_MASK_CONFLICT,
            f"mask_mode '{case.mask_mode}' is not compatible with causal semantics",
            "mask_mode",
        )
    elif case.mask_mode == "causal" and not case.causal:
        add(
            ErrorCode.CAUSAL_MASK_CONFLICT,
            "mask_mode='causal' conflicts with causal=false",
            "mask_mode",
        )
    elif case.mask_mode == "none" and case.causal:
        add(
            ErrorCode.CAUSAL_MASK_CONFLICT,
            "mask_mode='none' conflicts with causal=true",
            "mask_mode",
        )
    if case.scale is not None and (
        not np.isfinite(case.scale) or case.scale <= 0
    ):
        add(ErrorCode.INVALID_CONFIG, "scale must be finite and positive", "scale")

    revision = case.kv_revision
    if isinstance(revision, tuple):
        if case.num_kv_blocks and len(revision) != case.num_kv_blocks:
            add(
                ErrorCode.INVALID_REVISION,
                "per-block kv_revision length must equal the number of K/V blocks",
                "kv_revision",
            )
        if any(item < 0 for item in revision):
            add(
                ErrorCode.INVALID_REVISION,
                "kv_revision values must be non-negative",
                "kv_revision",
            )
    elif revision < 0:
        add(
            ErrorCode.INVALID_REVISION,
            "kv_revision must be non-negative",
            "kv_revision",
        )

    if arrays is None:
        if require_arrays:
            add(ErrorCode.INVALID_INPUT, "Q/K/V arrays are required", "arrays")
        return issues

    expected_shape = (case.n_heads, case.seq_len, case.head_dim)
    for field_name in ("q", "k", "v"):
        array = getattr(arrays, field_name)
        if not isinstance(array, np.ndarray):
            add(
                ErrorCode.INVALID_INPUT,
                f"{field_name} must be a NumPy array",
                field_name,
            )
            continue
        if array.shape != expected_shape:
            add(
                ErrorCode.SEQ_LEN_MISMATCH,
                f"{field_name} shape {array.shape} does not match expected "
                f"{expected_shape}",
                field_name,
            )
        if array.dtype.name not in SUPPORTED_DTYPES:
            add(
                ErrorCode.UNSUPPORTED_DTYPE,
                f"{field_name} dtype {array.dtype} is not supported",
                field_name,
            )
        elif array.dtype.name != case.dtype:
            add(
                ErrorCode.DTYPE_MISMATCH,
                f"{field_name} dtype {array.dtype.name} does not match case dtype "
                f"{case.dtype}",
                field_name,
            )
        if array.size and not np.isfinite(array).all():
            add(
                ErrorCode.NONFINITE_INPUT,
                f"{field_name} contains NaN or infinity",
                field_name,
            )
    return issues


def validate_cases(
    cases: Sequence[AttentionCase],
    arrays: Sequence[QKVArrays] | None = None,
) -> list[Issue]:
    issues: list[Issue] = []
    seen: dict[str, int] = {}
    for index, case in enumerate(cases):
        if case.case_id in seen:
            issues.append(
                Issue(
                    ErrorCode.DUPLICATE_CASE_ID,
                    f"duplicate case_id '{case.case_id}' at indexes "
                    f"{seen[case.case_id]} and {index}",
                    case_id=case.case_id,
                    field="case_id",
                )
            )
        else:
            seen[case.case_id] = index
        bundle = arrays[index] if arrays is not None else None
        issues.extend(validate_case(case, bundle))
    return issues
