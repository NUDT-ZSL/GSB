"""Public integration API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .engine import execute_task
from .spectrum import adjudicate_deviations


@dataclass
class ExecutionResult:
    """Integration result containing both a JSON-safe report and arrays."""

    report: dict[str, Any]
    output: np.ndarray | None
    reference_output: np.ndarray | None
    output_source: str

    @property
    def job_id(self) -> str:
        return str(self.report.get("job_id", "<unknown>"))

    @property
    def status(self) -> str:
        return str(self.report.get("status", "error"))

    @property
    def primary_code(self) -> str | None:
        value = self.report.get("primary_code")
        return str(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        return self.report

    def __getitem__(self, key: str) -> Any:
        return self.report[key]


def _as_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(task, Mapping):
        raise TypeError("task must be a mapping")
    return dict(task)


def inject_diagnose(
    task: Mapping[str, Any],
    *,
    input_array: np.ndarray | None = None,
    weight_array: np.ndarray | None = None,
) -> ExecutionResult:
    """Run the generic four-stage injection/diagnostic pipeline."""

    report, output, reference, source = execute_task(
        task, input_array=input_array, weight_array=weight_array
    )
    return ExecutionResult(
        report=report,
        output=output,
        reference_output=reference,
        output_source=source,
    )


def conv2d(
    task: Mapping[str, Any],
    *,
    input_array: np.ndarray | None = None,
    weight_array: np.ndarray | None = None,
) -> ExecutionResult:
    """Run a conv2d task through the diagnostic pipeline."""

    effective = _as_task(task)
    effective["op_kind"] = "conv2d"
    effective.pop("interp_mode", None)
    return inject_diagnose(
        effective, input_array=input_array, weight_array=weight_array
    )


def interpolate(
    task: Mapping[str, Any],
    *,
    mode: str | None = None,
    input_array: np.ndarray | None = None,
) -> ExecutionResult:
    """Run a nearest/bilinear/bicubic interpolation task."""

    effective = _as_task(task)
    if mode is not None:
        effective["interp_mode"] = mode
    if effective.get("interp_mode") is None and effective.get("op_kind") in {
        "nearest",
        "bilinear",
        "bicubic",
    }:
        effective["interp_mode"] = effective["op_kind"]
    effective["op_kind"] = "interpolate"
    return inject_diagnose(effective, input_array=input_array)


def run_task(
    task: Mapping[str, Any],
    *,
    input_array: np.ndarray | None = None,
    weight_array: np.ndarray | None = None,
) -> ExecutionResult:
    return inject_diagnose(
        task, input_array=input_array, weight_array=weight_array
    )


__all__ = [
    "ExecutionResult",
    "adjudicate_deviations",
    "conv2d",
    "inject_diagnose",
    "interpolate",
    "run_task",
]
