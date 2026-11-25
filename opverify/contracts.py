from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from .errors import OpVerifyError
from .hashing import sha256_json
from .policy import normalize_block_policy


DTYPE_ALIASES = {
    "float32": "float32",
    "f32": "float32",
    "<f4": "float32",
    "float64": "float64",
    "f64": "float64",
    "double": "float64",
    "<f8": "float64",
}
NUMPY_DTYPES = {"float32": "<f4", "float64": "<f8"}
INTERP_MODES = ("nearest", "bilinear", "bicubic")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _job_id(record: dict[str, Any]) -> str | None:
    value = record.get("job_id")
    if value is None:
        return None
    return str(value)


def _raise(code: str, message: str, record: dict[str, Any], field: str, **details: Any) -> None:
    raise OpVerifyError(code, message, job_id=_job_id(record), field=field, details=details)


def _positive_int_pair(value: Any, record: dict[str, Any], field: str) -> tuple[int, int]:
    if _is_int(value):
        if value <= 0:
            _raise("E_SHAPE_INVALID", "dimension must be positive", record, field, value=value)
        return int(value), int(value)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        if not all(_is_int(item) and item > 0 for item in value):
            _raise("E_SHAPE_INVALID", "pair must contain positive integers", record, field, value=value)
        return int(value[0]), int(value[1])
    _raise("E_SHAPE_INVALID", "expected positive integer or pair [height, width]", record, field, value=value)
    raise AssertionError("unreachable")


def _stride_pair(value: Any, record: dict[str, Any]) -> tuple[int, int]:
    field = "$.stride"
    if _is_int(value):
        values = (int(value), int(value))
    elif isinstance(value, (list, tuple)) and len(value) == 2 and all(_is_int(item) for item in value):
        values = (int(value[0]), int(value[1]))
    else:
        _raise("E_STRIDE_INVALID", "stride must be an integer or [sy, sx]", record, field, value=value)
        raise AssertionError("unreachable")
    if min(values) < 1:
        _raise("E_STRIDE_INVALID", "stride must be >= 1", record, field, value=list(values))
    return values


def _padding_pair(value: Any, record: dict[str, Any], kernel: tuple[int, int] | None = None) -> tuple[int, int]:
    field = "$.padding"
    if isinstance(value, str):
        mode = value.lower()
        if mode == "valid":
            return 0, 0
        if mode == "same":
            if kernel is None:
                _raise("E_PADDING_INVALID", "same padding requires kernel_shape", record, field)
            return (kernel[0] - 1) // 2, (kernel[1] - 1) // 2
        _raise("E_PADDING_INVALID", "padding string must be valid or same", record, field, value=value)
    if _is_int(value):
        values = (int(value), int(value))
    elif isinstance(value, (list, tuple)) and len(value) == 2 and all(_is_int(item) for item in value):
        values = (int(value[0]), int(value[1]))
    else:
        _raise("E_PADDING_INVALID", "padding must be a non-negative integer/pair or valid/same", record, field, value=value)
        raise AssertionError("unreachable")
    if min(values) < 0:
        _raise(
            "E_PADDING_INVALID",
            "padding must be inside declared legal range [0, +infinity)",
            record,
            field,
            value=list(values),
            legal_range=[0, None],
        )
    return values


def normalize_input_shape(value: Any, record: dict[str, Any]) -> tuple[int, int, int, int]:
    field = "$.input_shape"
    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            shape = [1, 1, *value]
        elif len(value) == 3:
            shape = [1, *value]
        elif len(value) == 4:
            shape = list(value)
        else:
            _raise("E_SHAPE_INVALID", "input_shape must have 2, 3 or 4 dimensions", record, field, value=value)
            raise AssertionError("unreachable")
    else:
        _raise("E_SHAPE_INVALID", "input_shape must be an array", record, field, value=value)
        raise AssertionError("unreachable")
    if not all(_is_int(dim) and dim > 0 for dim in shape):
        _raise("E_SHAPE_INVALID", "input dimensions must be positive integers", record, field, value=value)
    return int(shape[0]), int(shape[1]), int(shape[2]), int(shape[3])


def normalize_scale(value: Any, record: dict[str, Any]) -> tuple[float, float]:
    field = "$.scale"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        scales = (float(value), float(value))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        scales = tuple(float(item) for item in value)  # type: ignore[assignment]
    else:
        _raise("E_SHAPE_INVALID", "scale must be a positive finite number or [sy, sx]", record, field, value=value)
        raise AssertionError("unreachable")
    if len(scales) != 2 or not all(math.isfinite(item) and item > 0.0 for item in scales):
        _raise("E_SHAPE_INVALID", "scale must be positive and finite", record, field, value=value)
    return scales  # type: ignore[return-value]


def check_revision(
    revision: Any,
    record: dict[str, Any],
    prior_revision: int | None = None,
) -> int:
    if not _is_int(revision) or revision < 1:
        _raise("E_REVISION_ROLLBACK", "revision must be a positive integer", record, "$.revision", value=revision)
    current = int(revision)
    if prior_revision is not None and current < int(prior_revision):
        _raise(
            "E_REVISION_ROLLBACK",
            f"revision {current} is older than committed revision {prior_revision}",
            record,
            "$.revision",
            submitted_revision=current,
            committed_revision=int(prior_revision),
        )
    return current


def normalize_task(
    record: dict[str, Any],
    *,
    prior_revision: int | None = None,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise OpVerifyError("E_MANIFEST_INVALID", "task record must be a JSON object", field="$")
    job_id = _job_id(record)
    if not job_id:
        _raise("E_MANIFEST_INVALID", "job_id is required", record, "$.job_id")

    revision = check_revision(record.get("revision", 1), record, prior_revision)

    op_kind = record.get("op_kind")
    if op_kind not in ("conv2d", "interpolate"):
        _raise(
            "E_SHAPE_INVALID",
            "op_kind must be conv2d or interpolate",
            record,
            "$.op_kind",
            value=op_kind,
        )

    raw_dtype = record.get("dtype", "float64")
    dtype = DTYPE_ALIASES.get(str(raw_dtype).lower()) if raw_dtype is not None else None
    if dtype is None:
        _raise(
            "E_DTYPE_UNSUPPORTED",
            "only float32 and float64 are supported",
            record,
            "$.dtype",
            value=raw_dtype,
            supported=["float32", "float64"],
        )

    input_shape = normalize_input_shape(record.get("input_shape"), record)
    stride = _stride_pair(record.get("stride", 1), record)

    kernel: tuple[int, int] | None = None
    scale: tuple[float, float] | None = None
    interp_mode: str | None = None

    if op_kind == "conv2d":
        kernel = _positive_int_pair(record.get("kernel_shape"), record, "$.kernel_shape")
        padding = _padding_pair(record.get("padding", 0), record, kernel)
        out_h = (input_shape[2] + 2 * padding[0] - kernel[0]) // stride[0] + 1
        out_w = (input_shape[3] + 2 * padding[1] - kernel[1]) // stride[1] + 1
        if out_h <= 0 or out_w <= 0:
            _raise(
                "E_SHAPE_INVALID",
                "convolution output shape is empty; check kernel, padding and stride",
                record,
                "$.kernel_shape",
                output_shape=[input_shape[0], input_shape[1], out_h, out_w],
            )
    else:
        scale = normalize_scale(record.get("scale"), record)
        padding = _padding_pair(record.get("padding", 0), record)
        interp_mode = str(record.get("interp_mode", "nearest")).lower()
        if interp_mode not in INTERP_MODES:
            _raise(
                "E_INTERP_MODE_UNSUPPORTED",
                "interp_mode must be nearest, bilinear or bicubic",
                record,
                "$.interp_mode",
                value=record.get("interp_mode"),
                supported=list(INTERP_MODES),
            )
        out_h = max(1, math.floor(input_shape[2] * scale[0]))
        out_w = max(1, math.floor(input_shape[3] * scale[1]))

    policy = normalize_block_policy(record.get("block_policy"))

    depends_on = record.get("depends_on", [])
    if depends_on is None:
        depends_on = []
    if not isinstance(depends_on, (list, tuple)) or not all(isinstance(item, str) and item for item in depends_on):
        _raise("E_MANIFEST_INVALID", "depends_on must be a list of job_id strings", record, "$.depends_on")

    task: dict[str, Any] = {
        "job_id": job_id,
        "op_kind": op_kind,
        "input_shape": list(input_shape),
        "stride": list(stride),
        "padding": list(padding),
        "dtype": dtype,
        "revision": revision,
        "seed": int(record.get("seed", 0)),
        "weight_seed": int(record.get("weight_seed", record.get("seed", 0))),
        "block_policy": policy,
        "depends_on": list(depends_on),
        "output_shape": [input_shape[0], input_shape[1], out_h, out_w],
        "input_file": record.get("input_file"),
        "weight_file": record.get("weight_file"),
        "tolerance": record.get("tolerance"),
    }
    if kernel is not None:
        task["kernel_shape"] = list(kernel)
    if scale is not None:
        task["scale"] = list(scale)
    if interp_mode is not None:
        task["interp_mode"] = interp_mode
    return task


def semantic_parameters(task: dict[str, Any]) -> dict[str, Any]:
    """Parameters that define numerical output, excluding paths and revision."""
    keys = (
        "op_kind",
        "input_shape",
        "kernel_shape",
        "scale",
        "stride",
        "padding",
        "interp_mode",
        "dtype",
        "seed",
        "weight_seed",
        "depends_on",
        "output_shape",
    )
    return {key: deepcopy(task[key]) for key in keys if key in task}


def parameter_digest(task: dict[str, Any]) -> str:
    return sha256_json(semantic_parameters(task))


def output_spatial_shape(task: dict[str, Any]) -> tuple[int, int]:
    return int(task["output_shape"][2]), int(task["output_shape"][3])
