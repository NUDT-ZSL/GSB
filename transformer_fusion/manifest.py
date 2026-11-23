"""Task manifest loading, parsing and explicit fusion feasibility rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .errors import (
    Diagnostic,
    E_AXIS_INVALID,
    E_BLOCK_SIZE_INVALID,
    E_DTYPE_UNSUPPORTED,
    E_EPS_INVALID,
    E_FUSION_INFEASIBLE,
    E_MANIFEST_INVALID,
    E_NO_ADJACENT_PAIR,
    E_NOT_ELEMENTWISE,
    E_OP_UNKNOWN,
    E_REDUCTION_DEPENDENCY,
)
from .model import ParsedCase, SUPPORTED_DTYPES, TaskCase
from .ops import (
    ACTIVATIONS,
    is_activation_op,
    is_elementwise_activation,
    is_known_op,
    is_reduction_op,
    is_transpose_op,
)


class ManifestFileError(ValueError):
    def __init__(self, case_id: str, message: str) -> None:
        super().__init__(message)
        self.case_id = case_id


def _coerce_int_tuple(value: Any, field: str, case_id: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple, np.ndarray)):
        raise ManifestFileError(case_id, f"{field} must be a sequence")
    try:
        result = tuple(int(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise ManifestFileError(case_id, f"{field} must contain integers") from exc
    return result


def task_from_mapping(value: Mapping[str, Any]) -> TaskCase:
    if "case_id" not in value:
        raise ManifestFileError("*", "missing case_id")
    case_id = str(value["case_id"])
    missing = [
        key
        for key in (
            "op_chain",
            "input_shape",
            "axis_order",
            "activation_kind",
            "block_size",
            "eps",
            "dtype",
            "seed",
            "policy",
        )
        if key not in value
    ]
    if missing:
        raise ManifestFileError(case_id, f"missing required fields: {missing}")
    op_chain_raw = value["op_chain"]
    if not isinstance(op_chain_raw, (list, tuple)):
        raise ManifestFileError(case_id, "op_chain must be a sequence")
    op_chain = tuple(str(op) for op in op_chain_raw)
    if not op_chain:
        raise ManifestFileError(case_id, "op_chain must not be empty")
    try:
        input_shape = _coerce_int_tuple(value["input_shape"], "input_shape", case_id)
        axis_order = _coerce_int_tuple(value["axis_order"], "axis_order", case_id)
        block_size = int(value["block_size"])
        eps = float(value["eps"])
        seed = int(value["seed"])
    except (TypeError, ValueError) as exc:
        raise ManifestFileError(case_id, f"invalid numeric field: {exc}") from exc
    tolerance = value.get("tolerance")
    if tolerance is not None:
        if not isinstance(tolerance, Mapping):
            raise ManifestFileError(case_id, "tolerance must be an object")
        tolerance = {str(k): float(v) for k, v in tolerance.items()}
    metadata = value.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise ManifestFileError(case_id, "metadata must be an object")
    return TaskCase(
        case_id=case_id,
        op_chain=op_chain,
        input_shape=input_shape,
        axis_order=axis_order,
        activation_kind=str(value["activation_kind"]),
        block_size=block_size,
        eps=eps,
        dtype=str(value["dtype"]),
        seed=seed,
        policy=value["policy"],
        input_file=str(value["input_file"]) if value.get("input_file") is not None else None,
        tolerance=tolerance,
        metadata=dict(metadata),
    )


def load_tasks(source: str | Path | Sequence[TaskCase | Mapping[str, Any]] | None) -> list[TaskCase]:
    if source is None:
        source = "data/manifest.jsonl"
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.suffix.lower() == ".json":
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, Mapping):
                payload = payload.get("cases", [])
            source = payload
        else:
            tasks: list[TaskCase] = []
            with open(path, "r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise ManifestFileError("*", f"line {line_number}: invalid JSON") from exc
                    if not isinstance(payload, Mapping):
                        raise ManifestFileError("*", f"line {line_number}: object expected")
                    tasks.append(task_from_mapping(payload))
            return tasks
    assert not isinstance(source, (str, Path))
    tasks = []
    for item in source:
        tasks.append(item if isinstance(item, TaskCase) else task_from_mapping(item))
    return tasks


def parse_case(task: TaskCase) -> ParsedCase:
    diagnostics: list[Diagnostic] = []
    if len(task.input_shape) == 0 or any(int(dim) <= 0 for dim in task.input_shape):
        diagnostics.append(
            Diagnostic(
                code=E_MANIFEST_INVALID,
                case_id=task.case_id,
                stage="parse",
                message="input_shape must contain positive dimensions",
                details={"input_shape": list(task.input_shape)},
            )
        )
    if not task.activation_kind or task.activation_kind not in ACTIVATIONS:
        diagnostics.append(
            Diagnostic(
                code=E_OP_UNKNOWN,
                case_id=task.case_id,
                stage="parse",
                message=f"unknown activation_kind: {task.activation_kind}",
                details={"activation_kind": task.activation_kind},
            )
        )
    for index, op_name in enumerate(task.op_chain):
        if not is_known_op(op_name):
            diagnostics.append(
                Diagnostic(
                    code=E_OP_UNKNOWN,
                    case_id=task.case_id,
                    stage="parse",
                    message=f"unknown operator at op_chain[{index}]: {op_name}",
                    details={"op_chain": list(task.op_chain), "index": index, "op": op_name},
                )
            )
    if not diagnostics:
        # Structural feasibility checks live here so injection of a duplicate
        # axis is attributed to the feasibility stage rather than parsing.
        diagnostics.extend(validate_feasibility_inputs(task))
    return ParsedCase(
        task=task,
        ops=task.op_chain,
        valid=not diagnostics,
        diagnostics=tuple(diagnostics),
    )


def validate_feasibility_inputs(task: TaskCase) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    ndim = len(task.input_shape)
    axis_order = tuple(task.axis_order)
    valid_axis = (
        len(axis_order) == ndim
        and len(set(axis_order)) == ndim
        and all(0 <= int(axis) < ndim for axis in axis_order)
    )
    if not valid_axis:
        diagnostics.append(
            Diagnostic(
                code=E_AXIS_INVALID,
                case_id=task.case_id,
                stage="feasibility",
                message="axis_order must be a permutation of input dimensions",
                details={
                    "axis_order": list(axis_order),
                    "input_shape": list(task.input_shape),
                    "expected_rank": ndim,
                },
            )
        )
    if int(task.block_size) <= 0:
        diagnostics.append(
            Diagnostic(
                code=E_BLOCK_SIZE_INVALID,
                case_id=task.case_id,
                stage="feasibility",
                message="block_size must be positive",
                details={"block_size": task.block_size},
            )
        )
    if not np.isfinite(task.eps) or float(task.eps) < 0.0:
        diagnostics.append(
            Diagnostic(
                code=E_EPS_INVALID,
                case_id=task.case_id,
                stage="feasibility",
                message="eps must be finite and non-negative",
                details={"eps": task.eps},
            )
        )
    if task.dtype not in SUPPORTED_DTYPES:
        diagnostics.append(
            Diagnostic(
                code=E_DTYPE_UNSUPPORTED,
                case_id=task.case_id,
                stage="feasibility",
                message=f"dtype must be one of {SUPPORTED_DTYPES}",
                details={"dtype": task.dtype},
            )
        )
    return diagnostics


def find_fusion_pair(task: TaskCase) -> tuple[MutablePair | None, list[Diagnostic]]:
    """Return the first adjacent activation/transpose candidate.

    The parser separates "pair exists but is unsafe" from "no adjacent pair".
    """

    diagnostics = validate_feasibility_inputs(task)
    if diagnostics:
        return None, diagnostics

    for index in range(len(task.op_chain) - 1):
        left = task.op_chain[index]
        right = task.op_chain[index + 1]
        if not is_activation_op(left) or not is_transpose_op(right):
            continue
        checks: dict[str, Any] = {
            "adjacent": right == task.op_chain[index + 1],
            "activation_elementwise": is_elementwise_activation(left),
            "transpose_pure_axis_permutation": is_transpose_op(right),
            "prefix_shape_preserving": not any(
                is_transpose_op(op) for op in task.op_chain[:index]
            ),
            "reduction_between_operators": False,
            "axis_is_permutation": True,
            "activation_kind_matches_field": left == task.activation_kind,
            "block_size_positive": task.block_size > 0,
            "eps_finite_nonnegative": bool(np.isfinite(task.eps) and task.eps >= 0.0),
            "dtype_supported": task.dtype in SUPPORTED_DTYPES,
        }
        reasons: list[str] = []
        reason_code: str | None = None
        if not checks["activation_elementwise"]:
            reason_code = E_NOT_ELEMENTWISE
            reasons.append(f"{left} is not an elementwise activation")
        if is_reduction_op(left) or is_reduction_op(right):
            reason_code = E_REDUCTION_DEPENDENCY
            reasons.append("a reduction dependency exists inside the candidate pair")
        if not checks["prefix_shape_preserving"]:
            reason_code = E_FUSION_INFEASIBLE
            reasons.append("a preceding transpose changes the axis mapping domain")
        if not checks["activation_kind_matches_field"]:
            reasons.append(
                f"declared activation_kind={task.activation_kind!r} differs from pair op={left!r}"
            )
        feasible = reason_code is None
        return (
            MutablePair(
                activation_index=index,
                transpose_index=index + 1,
                activation_op=left,
                transpose_op=right,
                feasible=feasible,
                reason_code=reason_code,
                reasons=tuple(reasons),
                checks=checks,
            ),
            [],
        )

    # Distinguish a reduction sitting between an activation and a transpose
    # from a chain that simply has no transpose at all.
    first_activation = next((i for i, op in enumerate(task.op_chain) if is_activation_op(op)), None)
    if first_activation is not None:
        later_transpose = next(
            (
                i
                for i in range(first_activation + 1, len(task.op_chain))
                if is_transpose_op(task.op_chain[i])
            ),
            None,
        )
        if later_transpose is not None:
            between = task.op_chain[first_activation + 1 : later_transpose]
            if any(is_reduction_op(op) for op in between):
                pair = MutablePair(
                    activation_index=first_activation,
                    transpose_index=later_transpose,
                    activation_op=task.op_chain[first_activation],
                    transpose_op=task.op_chain[later_transpose],
                    feasible=False,
                    reason_code=E_REDUCTION_DEPENDENCY,
                    reasons=("a reduction operator lies between activation and transpose",),
                    checks={
                        "adjacent": False,
                        "between_ops": list(between),
                        "reduction_between_operators": True,
                    },
                )
                return pair, []
    return None, []


class MutablePair:
    """Small mutable builder; converted to immutable FusionPair by caller."""

    def __init__(
        self,
        *,
        activation_index: int,
        transpose_index: int,
        activation_op: str,
        transpose_op: str,
        feasible: bool,
        reason_code: str | None,
        reasons: tuple[str, ...],
        checks: Mapping[str, Any],
    ) -> None:
        self.activation_index = activation_index
        self.transpose_index = transpose_index
        self.activation_op = activation_op
        self.transpose_op = transpose_op
        self.feasible = feasible
        self.reason_code = reason_code
        self.reasons = reasons
        self.checks = dict(checks)

    def freeze(self):
        from .model import FusionPair

        return FusionPair(
            activation_index=self.activation_index,
            transpose_index=self.transpose_index,
            activation_op=self.activation_op,
            transpose_op=self.transpose_op,
            feasible=self.feasible,
            reason_code=self.reason_code,
            reasons=self.reasons,
            checks=self.checks,
        )
