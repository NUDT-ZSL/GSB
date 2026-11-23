"""Deterministic local sample task list."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import DomainError, E_INVALID_CASE
from .models import CaseSpec


def build_sample_cases() -> list[CaseSpec]:
    rows: list[dict[str, Any]] = [
        {
            "case_id": "case-rope-pi",
            "seq_len": 12,
            "head_dim": 8,
            "num_heads": 2,
            "base_theta": 10000.0,
            "variant_pair": ["rope", "position_interpolation"],
            "scale_ratio": 2.0,
            "freq_block": [1.0, 0.75, 0.5, 0.25],
            "block_size": 4,
            "causal": True,
            "seed": 101,
        },
        {
            "case_id": "case-rope-ntk",
            "seq_len": 16,
            "head_dim": 8,
            "num_heads": 2,
            "base_theta": 10000.0,
            "variant_pair": ["rope", "ntk_aware"],
            "scale_ratio": 2.0,
            "freq_block": [1.0, 0.75, 0.5, 0.25],
            "block_size": 5,
            "causal": True,
            "seed": 102,
        },
        {
            "case_id": "case-rope-block",
            "seq_len": 14,
            "head_dim": 8,
            "num_heads": 2,
            "base_theta": 10000.0,
            "variant_pair": ["rope", "block_frequency"],
            "scale_ratio": 2.0,
            "freq_block": [1.0, 0.75, 0.5, 0.25],
            "block_size": 4,
            "causal": False,
            "seed": 103,
        },
        {
            "case_id": "case-rope-pi-noncausal",
            "seq_len": 10,
            "head_dim": 12,
            "num_heads": 3,
            "base_theta": 10000.0,
            "variant_pair": ["rope", "position_interpolation"],
            "scale_ratio": 1.5,
            "freq_block": [1.0, 0.8, 0.6, 0.4, 0.2, 0.1],
            "block_size": 3,
            "causal": False,
            "seed": 104,
        },
        {
            "case_id": "case-rope-ntk-wide",
            "seq_len": 18,
            "head_dim": 16,
            "num_heads": 2,
            "base_theta": 10000.0,
            "variant_pair": ["rope", "ntk_aware"],
            "scale_ratio": 4.0,
            "freq_block": [1.0, 0.8, 0.6, 0.4, 0.2, 0.1, 0.05, 0.025],
            "block_size": 6,
            "causal": True,
            "seed": 105,
        },
        {
            "case_id": "case-rope-block-wide",
            "seq_len": 15,
            "head_dim": 16,
            "num_heads": 2,
            "base_theta": 10000.0,
            "variant_pair": ["rope", "block_frequency"],
            "scale_ratio": 3.0,
            "freq_block": [1.0, 0.8, 0.6, 0.4, 0.2, 0.1, 0.05, 0.025],
            "block_size": 5,
            "causal": True,
            "seed": 106,
        },
    ]
    return [CaseSpec.from_dict(row) for row in rows]


def write_cases_jsonl(path: str | Path, cases: Iterable[CaseSpec]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(case.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        for case in cases
    )
    path.write_text(text, encoding="utf-8")


def load_cases_jsonl(path: str | Path) -> list[CaseSpec]:
    path = Path(path)
    cases: list[CaseSpec] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DomainError(
                E_INVALID_CASE,
                f"invalid JSONL at line {line_number}: {exc}",
                {"line": line_number},
            ) from exc
        case = CaseSpec.from_dict(value)
        if case.case_id in seen:
            raise DomainError(
                E_INVALID_CASE,
                "duplicate case_id in task list",
                {"case_id": case.case_id, "line": line_number},
            )
        seen.add(case.case_id)
        cases.append(case)
    if not cases:
        raise DomainError(E_INVALID_CASE, "task list is empty", {})
    return cases
