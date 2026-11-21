"""Manifest- or CLI-driven exception injection without editing source code."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from .errors import BusinessError, fail


STAGES = ("candidate_filter", "temperature_scale", "top_p_filter", "sample")

STAGE_FIELDS = {
    "candidate_filter": {
        "dtype",
        "vocab_size",
        "batch_size",
        "k_value",
        "tie_policy",
        "k_mode",
        "resource_profile",
        "logits",
    },
    "temperature_scale": {"temperature"},
    "top_p_filter": {"top_p"},
    "sample": {"seed", "group_size"},
}

TYPE_DEFAULTS = {
    "non_finite_logits": ("candidate_filter", "logits", "nan"),
    "temperature_zero": ("temperature_scale", "temperature", 0.0),
    "temperature_negative": ("temperature_scale", "temperature", -1.0),
    "top_p_out_of_range": ("top_p_filter", "top_p", 1.5),
    "top_p_zero": ("top_p_filter", "top_p", 0.0),
    "seed_missing": ("sample", "seed", None),
}


def _stable_nonfinite_position(case_id: str, injection_id: str, batch: int, vocab: int) -> tuple[int, int]:
    digest = hashlib.sha256(f"{case_id}:{injection_id}".encode("utf-8")).digest()
    row = int.from_bytes(digest[:4], "big") % max(batch, 1)
    position = int.from_bytes(digest[4:8], "big") % max(vocab, 1)
    return row, position


@dataclass
class InjectionController:
    case_id: str
    configured: list[dict[str, Any]]
    records: list[dict[str, Any]] = field(default_factory=list)
    _stage_applied: dict[str, list[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(self.configured):
            item = dict(raw)
            injection_type = item.get("type")
            stage = item.get("stage")
            field_name = item.get("field")
            value = item.get("value")
            if injection_type in TYPE_DEFAULTS:
                default_stage, default_field, default_value = TYPE_DEFAULTS[injection_type]
                stage = stage or default_stage
                field_name = field_name or default_field
                if "value" not in item:
                    value = default_value
            injection_id = str(item.get("injection_id") or f"inj-{index + 1}")
            normalized_item = {
                "injection_id": injection_id,
                "case_id": self.case_id,
                "stage": stage,
                "type": injection_type,
                "field": field_name,
                "value": value,
            }
            normalized.append(normalized_item)
            self.records.append(
                {
                    "injection_id": injection_id,
                    "case_id": self.case_id,
                    "stage": stage,
                    "type": injection_type,
                    "field": field_name,
                    "configured_value": _json_safe(value),
                    "applied": False,
                    "reached": False,
                    "status": "configured",
                    "business_code": None,
                }
            )
        self.configured = normalized

    def _record(self, injection_id: str) -> dict[str, Any]:
        for record in self.records:
            if record["injection_id"] == injection_id:
                return record
        raise KeyError(injection_id)

    def apply(self, stage: str, params: dict[str, Any], logits: np.ndarray | None) -> tuple[np.ndarray | None, bool]:
        """Apply all configured injections for one stage.

        ``params`` is a caller-owned shallow copy.  Logits are copied before
        mutation so the generated sample source remains untouched.
        """

        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage}")
        applied = False
        if logits is not None:
            logits = np.array(logits, copy=True)
        for injection in self.configured:
            if injection["stage"] != stage:
                continue
            record = self._record(injection["injection_id"])
            record["reached"] = True
            field_name = injection.get("field")
            if stage not in STAGE_FIELDS or field_name not in STAGE_FIELDS[stage]:
                record["status"] = "invalid"
                record["business_code"] = "E_INJECTION_INVALID"
                fail(
                    "E_INJECTION_INVALID",
                    f"injection field {field_name!r} is not valid at stage {stage}",
                    field=str(field_name),
                    case_id=self.case_id,
                    stage=stage,
                )
            applied = True
            value = injection.get("value")
            if field_name == "logits":
                if logits is None:
                    record["status"] = "pending_logits"
                    ids = self._stage_applied.setdefault(stage, [])
                    if injection["injection_id"] not in ids:
                        ids.append(injection["injection_id"])
                    continue
                row, position = _stable_nonfinite_position(
                    self.case_id,
                    injection["injection_id"],
                    int(logits.shape[0]),
                    int(logits.shape[1]),
                )
                token = str(value).lower()
                if token in ("inf", "+inf", "posinf", "positive_infinity"):
                    replacement = np.inf
                elif token in ("-inf", "neginf", "negative_infinity"):
                    replacement = -np.inf
                else:
                    replacement = np.nan
                logits[row, position] = replacement
                record["applied_position"] = {"row": row, "vocabulary_index": position}
                record["applied_value"] = str(logits[row, position])
            else:
                params[field_name] = value
                record["applied_value"] = _json_safe(value)
            record["applied"] = True
            record["status"] = "applied"
            self._stage_applied.setdefault(stage, []).append(injection["injection_id"])
        return logits, applied

    def apply_logits(self, logits: np.ndarray) -> np.ndarray:
        """Apply deferred candidate-stage non-finite logits injections."""

        output = np.array(logits, copy=True)
        stage = "candidate_filter"
        for injection in self.configured:
            if injection["stage"] != stage or injection.get("field") != "logits":
                continue
            record = self._record(injection["injection_id"])
            if record["status"] not in ("pending_logits", "configured", "applied"):
                continue
            row, position = _stable_nonfinite_position(
                self.case_id,
                injection["injection_id"],
                int(output.shape[0]),
                int(output.shape[1]),
            )
            token = str(injection.get("value")).lower()
            if token in ("inf", "+inf", "posinf", "positive_infinity"):
                replacement = np.inf
            elif token in ("-inf", "neginf", "negative_infinity"):
                replacement = -np.inf
            else:
                replacement = np.nan
            output[row, position] = replacement
            record["reached"] = True
            record["applied"] = True
            record["status"] = "applied"
            record["applied_position"] = {"row": row, "vocabulary_index": position}
            record["applied_value"] = str(output[row, position])
            ids = self._stage_applied.setdefault(stage, [])
            if injection["injection_id"] not in ids:
                ids.append(injection["injection_id"])
        return output

    def stage_injected(self, stage: str) -> bool:
        return bool(self._stage_applied.get(stage))

    def stage_records(self, stage: str) -> list[dict[str, Any]]:
        ids = set(self._stage_applied.get(stage, []))
        return [record for record in self.records if record["injection_id"] in ids]

    def mark_error(self, stage: str, business_code: str, field_name: str | None) -> None:
        for record in self.records:
            if record["stage"] == stage and record["reached"]:
                record["business_code"] = business_code
                record["error_field"] = field_name
                record["status"] = "injected_error"

    def finalize(self, reached_stages: set[str]) -> list[dict[str, Any]]:
        for record in self.records:
            if record["stage"] not in reached_stages and record["status"] == "configured":
                record["status"] = "not_reached"
        return [dict(record) for record in self.records]


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, float) and not np.isfinite(value):
            return str(value)
        return value
    if isinstance(value, np.generic):
        return value.item()
    return copy.deepcopy(value)

