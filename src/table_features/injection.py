"""Input-driven fault injection for the four feature stages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .canonical import read_json, sha256_obj
from .errors import (
    E_CONFIG_INVALID,
    E_ENCODE_VALUE_RANGE,
    E_SLOT_DUP,
    E_SLOT_REF_INVALID,
    E_STANDARDIZE_VALUE_INVALID,
    ErrorRecord,
)


STAGES = ("encode", "standardize", "cross", "select")
DEFAULT_CODES = {
    "category_value_out_of_range": E_ENCODE_VALUE_RANGE,
    "target_folds_non_positive": E_CONFIG_INVALID,
    "non_finite_numeric": E_STANDARDIZE_VALUE_INVALID,
    "numeric_value_out_of_range": E_STANDARDIZE_VALUE_INVALID,
    "missing_slot_column": E_SLOT_REF_INVALID,
    "duplicate_slot": E_SLOT_DUP,
    "threshold_out_of_bounds": E_CONFIG_INVALID,
    "bins_invalid": E_CONFIG_INVALID,
    "unclassified_exception": "E_INJECTED_UNCLASSIFIED",
}
MESSAGES = {
    "category_value_out_of_range": "injected category value is outside the accepted domain",
    "target_folds_non_positive": "target-encoding fold count must be positive",
    "non_finite_numeric": "numeric value is not finite",
    "numeric_value_out_of_range": "numeric value is outside the configured domain",
    "missing_slot_column": "cross slot references a missing or non-categorical column",
    "duplicate_slot": "duplicate cross-slot combination",
    "threshold_out_of_bounds": "selection threshold is outside its allowed range",
    "bins_invalid": "selection bin count is invalid",
    "unclassified_exception": "injected exception was not claimed by a stage handler",
}


@dataclass(frozen=True)
class InjectionAction:
    stage: str
    kind: str
    feature: str | None
    row_id: str | None
    code: str
    details: dict[str, Any]
    raw: dict[str, Any]

    def to_error(self) -> ErrorRecord:
        return ErrorRecord(
            stage=self.stage,
            code=self.code,
            message=MESSAGES[self.kind],
            feature=self.feature,
            row_id=self.row_id,
            details={"injection_kind": self.kind, **self.details},
        )


class InjectedUnclassified(RuntimeError):
    """An injected exception intentionally left unclaimed by stage handlers."""

    def __init__(self, action: InjectionAction) -> None:
        super().__init__(MESSAGES["unclassified_exception"])
        self.action = action


@dataclass(frozen=True)
class InjectionConfig:
    raw: dict[str, Any]
    actions: tuple[InjectionAction, ...]

    @property
    def digest(self) -> str:
        return sha256_obj(self.raw)

    def for_stage(
        self,
        stage: str,
        *,
        kinds: Iterable[str] | None = None,
        feature: str | None = None,
    ) -> tuple[InjectionAction, ...]:
        allowed_kinds = set(kinds) if kinds is not None else None
        result: list[InjectionAction] = []
        for action in self.actions:
            if action.stage != stage:
                continue
            if allowed_kinds is not None and action.kind not in allowed_kinds:
                continue
            if feature is not None and action.feature != feature:
                continue
            result.append(action)
        return tuple(result)

    def has_stage(self, stage: str) -> bool:
        return any(action.stage == stage for action in self.actions)

    def unclassified_actions(self) -> tuple[InjectionAction, ...]:
        return tuple(action for action in self.actions if action.kind == "unclassified_exception")

    def raise_if_unclassified(self, stage: str) -> None:
        for action in self.for_stage(stage, kinds={"unclassified_exception"}):
            raise InjectedUnclassified(action)


def _action_from_raw(stage: str, item: Any, index: int) -> InjectionAction:
    if not isinstance(item, dict):
        raise ValueError(f"{stage}[{index}] must be an object")
    kind = item.get("kind")
    if kind not in DEFAULT_CODES:
        raise ValueError(f"{stage}[{index}].kind is unsupported: {kind!r}")
    if stage == "encode" and kind not in {
        "category_value_out_of_range",
        "target_folds_non_positive",
        "unclassified_exception",
    }:
        raise ValueError(f"injection kind {kind!r} is not valid for stage encode")
    if stage == "standardize" and kind not in {
        "non_finite_numeric",
        "numeric_value_out_of_range",
        "unclassified_exception",
    }:
        raise ValueError(f"injection kind {kind!r} is not valid for stage standardize")
    if stage == "cross" and kind not in {
        "missing_slot_column",
        "duplicate_slot",
        "unclassified_exception",
    }:
        raise ValueError(f"injection kind {kind!r} is not valid for stage cross")
    if stage == "select" and kind not in {
        "threshold_out_of_bounds",
        "bins_invalid",
        "unclassified_exception",
    }:
        raise ValueError(f"injection kind {kind!r} is not valid for stage select")
    code = item.get("code", DEFAULT_CODES[kind])
    if not isinstance(code, str) or not code:
        raise ValueError(f"{stage}[{index}].code must be a non-empty string")
    feature = item.get("feature")
    row_id = item.get("row_id")
    if feature is not None and not isinstance(feature, str):
        raise ValueError(f"{stage}[{index}].feature must be a string or null")
    if row_id is not None and not isinstance(row_id, str):
        raise ValueError(f"{stage}[{index}].row_id must be a string or null")
    if kind == "missing_slot_column":
        if feature is None and isinstance(item.get("slot_id"), str):
            feature = item["slot_id"]
    if kind in {
        "category_value_out_of_range",
        "target_folds_non_positive",
        "non_finite_numeric",
        "numeric_value_out_of_range",
        "missing_slot_column",
        "duplicate_slot",
    } and not feature:
        raise ValueError(f"{stage}[{index}] requires a feature or slot_id")
    if kind in {"category_value_out_of_range", "non_finite_numeric", "numeric_value_out_of_range"}:
        if not row_id:
            raise ValueError(f"{stage}[{index}] requires a row_id for row-level injection")
    if kind == "missing_slot_column" and not isinstance(item.get("column"), str):
        raise ValueError(f"{stage}[{index}].column is required for missing_slot_column")
    details = {
        key: value
        for key, value in item.items()
        if key not in {"kind", "code", "feature", "row_id"}
    }
    return InjectionAction(
        stage=stage,
        kind=kind,
        feature=feature,
        row_id=row_id,
        code=code,
        details=details,
        raw=dict(item),
    )


def parse_injection_config(raw: dict[str, Any] | None) -> InjectionConfig:
    document = {} if raw is None else raw
    if not isinstance(document, dict):
        raise ValueError("injection config must be a JSON object")
    if document.get("enabled", True) is False:
        document = {"enabled": False}
    actions: list[InjectionAction] = []
    for stage in STAGES:
        items = document.get(stage, [])
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ValueError(f"injection config {stage} must be a list")
        for index, item in enumerate(items):
            actions.append(_action_from_raw(stage, item, index))
    return InjectionConfig(raw=dict(document), actions=tuple(actions))


def load_injection_config(path: str | Path | None) -> InjectionConfig:
    if path is None:
        return parse_injection_config(None)
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ValueError("injection config must be a JSON object")
    return parse_injection_config(raw)

