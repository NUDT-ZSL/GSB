from __future__ import annotations

from copy import deepcopy
from typing import Any

from .errors import WorkbenchError


REQUIRED_KINDS = {
    "missing_field",
    "out_of_range_weight",
    "empty_subword_set",
    "precision_sensitive",
}


def _matches(candidate: dict[str, Any], target: dict[str, Any]) -> bool:
    if not isinstance(target, dict):
        return False
    if "sample_id" in target:
        return candidate.get("sample_id") == target["sample_id"]
    if "task_type" in target and candidate.get("task_type") != target["task_type"]:
        return False
    if "generation_index" in target and candidate.get("generation_index") != target["generation_index"]:
        return False
    return "task_type" in target or "generation_index" in target


def apply_injections(
    candidate: dict[str, Any], items: list[dict[str, Any]], hits: dict[str, dict[str, Any]]
) -> None:
    for item in items:
        injection_id = str(item.get("id", ""))
        if not injection_id or injection_id in hits:
            continue
        if not _matches(candidate, item.get("target", {})):
            continue
        kind = item.get("kind")
        if kind not in REQUIRED_KINDS:
            hits[injection_id] = {
                "injection_id": injection_id,
                "kind": kind,
                "sample_id": candidate.get("sample_id"),
                "stage": "generation",
                "outcome": "unsupported",
                "business_code": "E_INJECTION_UNSUPPORTED",
            }
            continue
        if kind == "missing_field":
            field_name = str(item.get("field", "reference_answer"))
            if field_name not in candidate:
                raise WorkbenchError(
                    "E_INJECTION_INVALID",
                    f"missing_field injection field {field_name} is not present",
                    "generation",
                    candidate.get("sample_id"),
                )
            del candidate[field_name]
        elif kind == "out_of_range_weight":
            dimension = str(item.get("dimension", "format_compliance"))
            value = item.get("value", 1.25)
            candidate.setdefault("scoring_overrides", {}).setdefault(
                "dimension_weights", {}
            )[dimension] = value
        elif kind == "empty_subword_set":
            candidate["injected_empty_subwords"] = True
        elif kind == "precision_sensitive":
            offsets = item.get(
                "probe_offsets",
                {
                    "instruction_clarity": 0.000123456789,
                    "answer_correctness": 0.000234567891,
                    "format_compliance": 0.000345678912,
                    "length_compliance": 0.000456789123,
                },
            )
            if not isinstance(offsets, dict):
                raise WorkbenchError(
                    "E_INJECTION_INVALID",
                    "precision_sensitive probe_offsets must be an object",
                    "generation",
                    candidate.get("sample_id"),
                )
            candidate["precision_probe_offsets"] = deepcopy(offsets)
        hits[injection_id] = {
            "injection_id": injection_id,
            "kind": kind,
            "sample_id": candidate.get("sample_id"),
            "stage": "generation",
            "outcome": "applied",
            "business_code": {
                "missing_field": "E_MISSING_FIELD",
                "out_of_range_weight": "E_OUT_OF_RANGE_WEIGHT",
                "empty_subword_set": "E_EMPTY_SUBWORD_SET",
                "precision_sensitive": "E_PRECISION_SENSITIVE",
            }[kind],
        }


def missing_hits(items: list[dict[str, Any]], hits: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in items:
        injection_id = str(item.get("id", ""))
        if injection_id and injection_id not in hits:
            result.append(
                {
                    "injection_id": injection_id,
                    "kind": item.get("kind"),
                    "sample_id": None,
                    "stage": "matching",
                    "outcome": "not_found",
                    "business_code": "E_INJECTION_NOT_FOUND",
                }
            )
    return result

