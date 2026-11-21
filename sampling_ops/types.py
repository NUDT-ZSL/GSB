"""Task manifest parsing and common value validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from .errors import fail


DTYPE_ALIASES = {
    "float16": np.dtype("float16"),
    "fp16": np.dtype("float16"),
    "float32": np.dtype("float32"),
    "fp32": np.dtype("float32"),
    "float64": np.dtype("float64"),
    "fp64": np.dtype("float64"),
    "double": np.dtype("float64"),
    "float": np.dtype("float64"),
    "half": np.dtype("float16"),
}

TIE_POLICY_ALIASES = {
    "index_asc": "score_desc_index_asc",
    "index_ascending": "score_desc_index_asc",
    "stable_index": "score_desc_index_asc",
    "score_desc_index_asc": "score_desc_index_asc",
    "score_desc_then_index_asc": "score_desc_index_asc",
}

ROUTE_ALIASES = {
    "auto": "auto",
    "adaptive": "auto",
    "decision": "auto",
    "full": "full_sort",
    "sort": "full_sort",
    "full_sort": "full_sort",
    "heap": "heap_select",
    "heap_select": "heap_select",
    "chunk": "chunked_two_stage",
    "chunked": "chunked_two_stage",
    "chunked_two_stage": "chunked_two_stage",
}

ROUTES = ("full_sort", "heap_select", "chunked_two_stage")


def resolve_dtype(value: Any, *, case_id: str | None = None) -> np.dtype:
    if isinstance(value, np.dtype):
        value = value.name
    elif isinstance(value, type) and issubclass(value, np.floating):
        value = np.dtype(value).name
    if isinstance(value, str) and value.strip().lower() in {"bf16", "bfloat16"}:
        fail(
            "E_DTYPE_UNSUPPORTED",
            "bfloat16 has no native NumPy storage; use float16, float32, or float64",
            field="dtype",
            case_id=case_id,
            details={"actual": value, "supported": sorted(DTYPE_ALIASES)},
        )
    if not isinstance(value, str):
        fail(
            "E_DTYPE_UNSUPPORTED",
            "dtype must be one of float16, float32, float64 (aliases fp16/fp32/fp64)",
            field="dtype",
            case_id=case_id,
            details={"actual": value},
        )
    resolved = DTYPE_ALIASES.get(value.strip().lower())
    if resolved is None:
        fail(
            "E_DTYPE_UNSUPPORTED",
            f"unsupported dtype {value!r}",
            field="dtype",
            case_id=case_id,
            details={"actual": value, "supported": sorted(DTYPE_ALIASES)},
        )
    return resolved


def normalize_tie_policy(value: Any, *, case_id: str | None = None) -> str:
    if value is None:
        return "score_desc_index_asc"
    if isinstance(value, str):
        resolved = TIE_POLICY_ALIASES.get(value.strip().lower())
        if resolved is not None:
            return resolved
    fail(
        "E_TIE_POLICY_UNSUPPORTED",
        "tie_policy must declare score-descending order with index-ascending ties",
        field="tie_policy",
        case_id=case_id,
        details={"actual": value, "supported": sorted(TIE_POLICY_ALIASES)},
    )
    raise AssertionError("unreachable")


def normalize_route(value: Any, *, field_name: str = "route", case_id: str | None = None) -> str:
    if isinstance(value, str):
        resolved = ROUTE_ALIASES.get(value.strip().lower())
        if resolved is not None:
            return resolved
    fail(
        "E_K_MODE_INVALID",
        "route/k_mode must be auto, full_sort, heap_select, or chunked_two_stage",
        field=field_name,
        case_id=case_id,
        details={"actual": value, "supported": sorted(ROUTE_ALIASES)},
    )
    raise AssertionError("unreachable")


def positive_int(value: Any, *, field_name: str, code: str, case_id: str | None) -> int:
    if isinstance(value, bool):
        fail(code, f"{field_name} must be a positive integer", field=field_name, case_id=case_id)
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError):
        fail(code, f"{field_name} must be a positive integer", field=field_name, case_id=case_id)
    if isinstance(value, float) and not value.is_integer():
        fail(code, f"{field_name} must be a positive integer", field=field_name, case_id=case_id)
    if converted <= 0:
        fail(code, f"{field_name} must be a positive integer", field=field_name, case_id=case_id)
    return converted


def finite_float(
    value: Any,
    *,
    field_name: str,
    code: str,
    case_id: str | None,
) -> float:
    if isinstance(value, bool):
        fail(code, f"{field_name} must be a finite number", field=field_name, case_id=case_id)
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        fail(code, f"{field_name} must be a finite number", field=field_name, case_id=case_id)
    if not np.isfinite(converted):
        fail(code, f"{field_name} must be finite", field=field_name, case_id=case_id)
    return converted


@dataclass
class TaskSpec:
    """A parsed task manifest.

    Value validation is intentionally deferred when a failure belongs to a
    specific pipeline stage.  The raw manifest remains available in ``raw``.
    """

    raw: dict[str, Any]
    case_id: str
    seed_present: bool
    seed: Any
    dtype: Any
    vocab_size: Any
    batch_size: Any
    k_mode: Any
    k_value: Any
    temperature: Any
    top_p: Any
    group_size: Any
    tie_policy: Any
    resource_profile: Any
    injections: list[dict[str, Any]] = field(default_factory=list)
    logits_quantum: float | None = None
    tolerance: float = 1e-12

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any], *, fallback_case_id: str) -> "TaskSpec":
        raw = dict(row)
        case_id_value = raw.get("case_id")
        case_id = str(case_id_value) if case_id_value not in (None, "") else fallback_case_id
        quantum = raw.get("logits_quantum")
        generation = raw.get("generation")
        if quantum is None and isinstance(generation, Mapping):
            quantum = generation.get("logits_quantum")
        tolerance = raw.get("tolerance", 1e-12)
        injections = raw.get("injections", [])
        if not isinstance(injections, list):
            injections = [injections]
        return cls(
            raw=raw,
            case_id=case_id,
            seed_present="seed" in raw,
            seed=raw.get("seed"),
            dtype=raw.get("dtype"),
            vocab_size=raw.get("vocab_size"),
            batch_size=raw.get("batch_size"),
            k_mode=raw.get("k_mode", "auto"),
            k_value=raw.get("k_value"),
            temperature=raw.get("temperature"),
            top_p=raw.get("top_p"),
            group_size=raw.get("group_size", 1),
            tie_policy=raw.get("tie_policy", "index_asc"),
            resource_profile=raw.get("resource_profile", {}),
            injections=[dict(item) for item in injections if isinstance(item, Mapping)],
            logits_quantum=quantum,
            tolerance=tolerance,
        )

    def public_input(self) -> dict[str, Any]:
        fields = (
            "case_id",
            "vocab_size",
            "batch_size",
            "seed",
            "k_mode",
            "k_value",
            "temperature",
            "top_p",
            "group_size",
            "dtype",
            "tie_policy",
            "resource_profile",
        )
        result = {name: self.raw.get(name) for name in fields}
        if "logits_quantum" in self.raw:
            result["logits_quantum"] = self.raw["logits_quantum"]
        return result

