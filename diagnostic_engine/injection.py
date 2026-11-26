from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import DiagnosticIssue


STAGE_FEATURE = "feature_standardization"
STAGE_SOLVE = "path_solve"
STAGE_BLOCK = "block_reduce"
STAGE_VALIDATION = "result_validation"

SUPPORTED_INJECTIONS = {
    (STAGE_FEATURE, "CATEGORY_OUT_OF_RANGE"),
    (STAGE_SOLVE, "REG_STRENGTH_NONPOSITIVE"),
    (STAGE_BLOCK, "BLOCK_SIZE_ZERO"),
    (STAGE_VALIDATION, "TOLERANCE_NONPOSITIVE"),
}


@dataclass
class InjectionController:
    specs: list[dict[str, Any]] = field(default_factory=list)
    issues: list[DiagnosticIssue] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)

    @classmethod
    def from_specs(
        cls, specs: Iterable[dict[str, Any]] | None
    ) -> "InjectionController":
        controller = cls()
        for spec in specs or ():
            normalized = dict(spec)
            key = (normalized.get("stage"), normalized.get("code"))
            if key not in SUPPORTED_INJECTIONS:
                raise ValueError(f"unsupported injection {key}")
            if bool(normalized.get("enabled", True)):
                controller.specs.append(normalized)
        return controller

    def find(self, stage: str, code: str) -> dict[str, Any] | None:
        for spec in self.specs:
            if spec.get("stage") == stage and spec.get("code") == code:
                return spec
        return None

    def mutate_rows(
        self, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        spec = self.find(STAGE_FEATURE, "CATEGORY_OUT_OF_RANGE")
        if spec is None:
            return rows
        mutated = copy.deepcopy(rows)
        row_index = int(spec.get("row_index", 0))
        column = str(spec.get("column", "region"))
        value = spec.get("value", "__OUT_OF_VOCAB__")
        if not 0 <= row_index < len(mutated):
            raise ValueError("category injection row_index is out of range")
        if column not in mutated[row_index]:
            raise ValueError(f"category injection column {column!r} is absent")
        mutated[row_index] = dict(mutated[row_index])
        mutated[row_index][column] = value
        self.applied.append("feature_standardization:CATEGORY_OUT_OF_RANGE")
        return mutated

    def record_unknown_category(
        self, *, row_index: int, column: str, value: Any
    ) -> DiagnosticIssue:
        issue = DiagnosticIssue(
            stage=STAGE_FEATURE,
            code="E_CATEGORY_OUT_OF_RANGE",
            message="category value is outside the configured vocabulary",
            severity="warning",
            action="zero_fill_unknown_category",
            degrade_category="unknown_category_zero_fill",
            details={
                "row_index": row_index,
                "column": column,
                "value": str(value),
                "policy": "one_hot_zero_vector",
            },
        )
        self.issues.append(issue)
        return issue

    def inject_strength(
        self, strengths: list[float]
    ) -> tuple[list[float], DiagnosticIssue | None]:
        spec = self.find(STAGE_SOLVE, "REG_STRENGTH_NONPOSITIVE")
        if spec is None:
            return strengths, None
        index = int(spec.get("index", 0))
        if not 0 <= index < len(strengths):
            raise ValueError("regularization injection index is out of range")
        fallback = float(spec.get("fallback", 1e-8))
        injected = list(strengths)
        injected[index] = float(spec.get("value", 0.0))
        self.applied.append("path_solve:REG_STRENGTH_NONPOSITIVE")
        issue = DiagnosticIssue(
            stage=STAGE_SOLVE,
            code="E_REG_STRENGTH_NONPOSITIVE",
            message="regularization strength is not positive",
            severity="warning",
            action="clamp_to_positive_floor",
            degrade_category="regularization_strength_clamped",
            details={
                "path_index": index,
                "injected_value": injected[index],
                "fallback": fallback,
            },
        )
        self.issues.append(issue)
        injected[index] = fallback
        return injected, issue

    def inject_block_size(self, block_size: int | None) -> tuple[int | None, bool]:
        spec = self.find(STAGE_BLOCK, "BLOCK_SIZE_ZERO")
        if spec is None:
            return block_size, False
        self.applied.append("block_reduce:BLOCK_SIZE_ZERO")
        injected_value = int(spec.get("value", 0))
        return injected_value, True

    def record_block_fallback(
        self, *, injected_value: int, fallback_size: int
    ) -> DiagnosticIssue:
        issue = DiagnosticIssue(
            stage=STAGE_BLOCK,
            code="E_BLOCK_SIZE_ZERO",
            message="block size must be positive; deterministic fallback applied",
            severity="warning",
            action="replace_with_budget_block",
            degrade_category="auto_block_plan",
            details={
                "injected_block_size": injected_value,
                "fallback_block_size": fallback_size,
            },
        )
        self.issues.append(issue)
        return issue

    def inject_tolerance(self, tolerance: float) -> tuple[float, bool]:
        spec = self.find(STAGE_VALIDATION, "TOLERANCE_NONPOSITIVE")
        if spec is None:
            return tolerance, False
        self.applied.append("result_validation:TOLERANCE_NONPOSITIVE")
        return float(spec.get("value", 0.0)), True

    def record_tolerance_fallback(
        self, *, injected_value: float, fallback: float
    ) -> DiagnosticIssue:
        issue = DiagnosticIssue(
            stage=STAGE_VALIDATION,
            code="E_TOLERANCE_NONPOSITIVE",
            message="validation tolerance must be positive",
            severity="warning",
            action="use_default_validation_tolerance",
            degrade_category="default_validation_tolerance",
            details={
                "injected_tolerance": injected_value,
                "fallback_tolerance": fallback,
            },
        )
        self.issues.append(issue)
        return issue

    def issue_dicts(self) -> list[dict[str, Any]]:
        return [issue.to_dict() for issue in self.issues]


def parse_injection_arguments(values: Iterable[str]) -> list[dict[str, Any]]:
    """Parse CLI values such as feature_standardization:row=3,column=region."""

    specs: list[dict[str, Any]] = []
    code_map = {
        "feature_standardization": "CATEGORY_OUT_OF_RANGE",
        "path_solve": "REG_STRENGTH_NONPOSITIVE",
        "block_reduce": "BLOCK_SIZE_ZERO",
        "result_validation": "TOLERANCE_NONPOSITIVE",
    }
    for raw in values:
        parts = [part for part in raw.split(":") if part]
        stage = parts[0]
        if stage not in code_map:
            raise ValueError(f"unknown injection stage {stage!r}")
        spec: dict[str, Any] = {"stage": stage, "code": code_map[stage]}
        if len(parts) > 1:
            for item in parts[1].split(","):
                if "=" not in item:
                    continue
                key, value = item.split("=", 1)
                if value.lower() in {"true", "false"}:
                    parsed: Any = value.lower() == "true"
                else:
                    try:
                        parsed = int(value)
                    except ValueError:
                        try:
                            parsed = float(value)
                        except ValueError:
                            parsed = value
                spec[key] = parsed
        specs.append(spec)
    return specs

