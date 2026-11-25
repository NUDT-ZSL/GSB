from __future__ import annotations

import json
from typing import Any

import numpy as np

from .errors import WorkbenchError
from .numeric import PrecisionContext


REQUIRED_FIELDS = [
    "edit_id", "image_seed", "image_shape", "mask", "prompt",
    "style_strength", "iterations", "block_size", "dtype_profile", "revision",
]


def load_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkbenchError("E_SCHEMA_INVALID", f"invalid JSON on line {line_number}") from exc
            if not isinstance(row, dict):
                raise WorkbenchError("E_SCHEMA_INVALID", f"line {line_number} is not an object")
            rows.append(row)
    return rows


def load_params(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        params = json.load(handle)
    if not isinstance(params, dict):
        raise WorkbenchError("E_SCHEMA_INVALID", "parameter file must contain a JSON object")
    return params


def validate_task(task: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
    edit_id = str(task.get("edit_id", "<unknown>"))
    missing = [field for field in REQUIRED_FIELDS if field not in task]
    if missing:
        raise WorkbenchError(
            "E_SCHEMA_INVALID", "task is missing required fields",
            edit_id=edit_id, fields=missing,
        )

    strength = task["style_strength"]
    if not isinstance(strength, (int, float)) or isinstance(strength, bool) or not np.isfinite(strength):
        raise WorkbenchError("E_STYLE_INVALID", "style_strength must be numeric", edit_id=edit_id)
    if float(strength) < 0.0 or float(strength) > 1.0:
        raise WorkbenchError(
            "E_STYLE_INVALID", "style_strength must be in [0, 1]",
            edit_id=edit_id, style_strength=float(strength),
        )

    for field in ("iterations", "block_size", "revision", "image_seed"):
        value = task[field]
        if not isinstance(value, (int, np.integer)) or isinstance(value, bool) or int(value) <= 0:
            if field == "revision" and isinstance(value, int) and value >= 0:
                continue
            raise WorkbenchError(
                "E_SCHEMA_INVALID", f"{field} must be a positive integer",
                edit_id=edit_id, value=value,
            )
    if int(task["revision"]) < 1:
        raise WorkbenchError("E_SCHEMA_INVALID", "revision must be >= 1", edit_id=edit_id)

    shape = task["image_shape"]
    if (
        not isinstance(shape, list) or len(shape) != 3
        or not all(isinstance(v, (int, np.integer)) and int(v) > 0 for v in shape)
        or int(shape[2]) != 3
    ):
        raise WorkbenchError("E_SCHEMA_INVALID", "image_shape must be [H, W, 3]", edit_id=edit_id)

    try:
        PrecisionContext(
            str(task["dtype_profile"]) if not isinstance(task["dtype_profile"], dict)
            else str(task["dtype_profile"].get("kind", "fixed")),
            float(task.get("quant_scale", (params or {}).get("quantization", {}).get("default_quant_scale", 128))),
            str(task.get("rounding_mode", (params or {}).get("quantization", {}).get("rounding_mode", "nearest"))),
        )
    except ValueError as exc:
        raise WorkbenchError("E_SCHEMA_INVALID", str(exc), edit_id=edit_id) from exc

    matrix = resolve_color_matrix(task, params)
    validate_color_matrix(matrix, edit_id)
    return {"color_matrix": matrix}


def resolve_color_matrix(task: dict[str, Any], params: dict[str, Any] | None) -> np.ndarray:
    raw = task.get("color_matrix")
    if raw is None and params is not None:
        raw = params.get("color_matrix")
    if raw is None:
        raw = np.eye(3).tolist()
    try:
        matrix = np.asarray(raw, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        edit_id = str(task.get("edit_id", "<unknown>"))
        raise WorkbenchError("E_TRANSFORM_INVALID", "matrix is not numeric", edit_id=edit_id) from exc
    return matrix


def validate_color_matrix(matrix: np.ndarray, edit_id: str | None = None) -> None:
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise WorkbenchError(
            "E_TRANSFORM_INVALID",
            "color migration matrix must be a finite 3x3 matrix",
            edit_id=edit_id,
            shape=list(matrix.shape),
        )
