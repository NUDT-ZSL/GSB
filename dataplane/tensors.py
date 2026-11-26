from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

import numpy as np

from .errors import BizError, ErrorCode
from .hashing import array_digest


DTYPES = {
    "float16": np.float16,
    "float32": np.float32,
    "float64": np.float64,
    "int8": np.int8,
    "int16": np.int16,
    "uint8": np.uint8,
}


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_finite_positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def block_count(data_size: int, block_plan: Dict[str, Any]) -> int:
    if not isinstance(block_plan, dict):
        raise BizError(ErrorCode.BLOCK_PLAN_INVALID, "block_plan must be an object")
    if block_plan.get("strategy") == "fixed_rows":
        size = block_plan.get("block_size")
        if not is_positive_int(size):
            raise BizError(
                ErrorCode.BLOCK_PLAN_INVALID,
                "block_plan.block_size must be a positive integer",
                {"block_plan": block_plan},
            )
        return (int(data_size) + int(size) - 1) // int(size)
    if block_plan.get("strategy") == "explicit":
        ranges = block_plan.get("ranges")
        if not isinstance(ranges, list) or not ranges:
            raise BizError(
                ErrorCode.BLOCK_PLAN_INVALID,
                "explicit block_plan.ranges must be a non-empty list",
                {"block_plan": block_plan},
            )
        for index, item in enumerate(ranges):
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or not all(is_non_negative_int(x) for x in item)
                or item[0] >= item[1]
                or item[1] > data_size
            ):
                raise BizError(
                    ErrorCode.BLOCK_PLAN_INVALID,
                    "explicit block range is invalid",
                    {"index": index, "range": item, "data_size": data_size},
                )
        return len(ranges)
    raise BizError(
        ErrorCode.BLOCK_PLAN_INVALID,
        "unsupported block_plan.strategy",
        {"strategy": block_plan.get("strategy")},
    )


def block_bounds(
    data_size: int, block_plan: Dict[str, Any], block_index: int
) -> Tuple[int, int]:
    count = block_count(data_size, block_plan)
    if not is_non_negative_int(block_index) or block_index >= count:
        raise BizError(
            ErrorCode.BLOCK_RANGE_OUT_OF_BOUNDS,
            "block_index is outside the block plan",
            {"block_index": block_index, "block_count": count},
        )
    if block_plan.get("strategy") == "fixed_rows":
        size = int(block_plan["block_size"])
        start = block_index * size
        end = min(data_size, start + size)
        return start, end
    start, end = block_plan["ranges"][block_index]
    return int(start), int(end)


def validate_block_range(
    case_id: str,
    data_size: int,
    block_plan: Dict[str, Any],
    start: Any,
    end: Any,
) -> Tuple[int, int, int]:
    count = block_count(data_size, block_plan)
    if not is_non_negative_int(start) or not is_positive_int(end):
        raise BizError(
            ErrorCode.BLOCK_RANGE_OUT_OF_BOUNDS,
            "patch block_range must be [start, end) integer pair",
            {"case_id": case_id, "block_range": [start, end], "block_count": count},
        )
    if start >= end or end > count:
        raise BizError(
            ErrorCode.BLOCK_RANGE_OUT_OF_BOUNDS,
            "patch block range is outside the case block plan",
            {
                "case_id": case_id,
                "block_start": start,
                "block_end": end,
                "block_count": count,
            },
        )
    return int(start), int(end), count


def generate_distribution_array(
    shape: List[int], dtype_name: str, distribution: Dict[str, Any]
) -> np.ndarray:
    if dtype_name not in DTYPES:
        raise BizError(
            ErrorCode.DTYPE_UNSUPPORTED,
            "unsupported dtype",
            {"dtype": dtype_name, "supported": sorted(DTYPES)},
        )
    if not isinstance(distribution, dict):
        raise BizError(ErrorCode.SAMPLE_INVALID, "distribution must be an object")
    seed = distribution.get("seed")
    if not is_non_negative_int(seed):
        raise BizError(
            ErrorCode.SAMPLE_INVALID,
            "distribution.seed must be a non-negative integer",
            {"distribution": distribution},
        )
    rng = np.random.default_rng(int(seed))
    kind = distribution.get("kind", "normal")
    if kind == "normal":
        mean = float(distribution.get("mean", 0.0))
        std = float(distribution.get("std", 1.0))
        if not math.isfinite(mean) or not math.isfinite(std) or std < 0:
            raise BizError(
                ErrorCode.SAMPLE_INVALID, "invalid normal distribution parameters"
            )
        values = rng.normal(mean, std, size=tuple(shape))
    elif kind == "uniform":
        low = float(distribution.get("low", -1.0))
        high = float(distribution.get("high", 1.0))
        if not math.isfinite(low) or not math.isfinite(high) or low >= high:
            raise BizError(
                ErrorCode.SAMPLE_INVALID, "invalid uniform distribution parameters"
            )
        values = rng.uniform(low, high, size=tuple(shape))
    elif kind == "constant":
        value = distribution.get("value", 0.0)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise BizError(ErrorCode.SAMPLE_INVALID, "invalid constant value")
        values = np.full(shape, float(value), dtype=np.float64)
    else:
        raise BizError(
            ErrorCode.SAMPLE_INVALID,
            "unsupported distribution kind",
            {"kind": kind},
        )

    np_dtype = DTYPES[dtype_name]
    if np.issubdtype(np_dtype, np.integer):
        info = np.iinfo(np_dtype)
        values = np.clip(np.rint(values), info.min, info.max).astype(np_dtype)
    else:
        values = values.astype(np_dtype)
    return values


def verify_descriptor_digest(descriptor: Dict[str, Any]) -> str:
    shape = descriptor.get("shape")
    dtype_name = descriptor.get("dtype")
    distribution = descriptor.get("distribution")
    expected = descriptor.get("digest")
    if not isinstance(shape, list) or not all(is_positive_int(x) for x in shape):
        raise BizError(
            ErrorCode.TENSOR_SHAPE_INVALID,
            "tensor shape must be a list of positive integers",
            {"descriptor": {k: descriptor.get(k) for k in ("name", "shape")}},
        )
    array = generate_distribution_array(shape, dtype_name, distribution)
    actual = array_digest(array)
    if expected != actual:
        raise BizError(
            ErrorCode.DIGEST_MISMATCH,
            "tensor digest does not match deterministically generated content",
            {
                "name": descriptor.get("name"),
                "expected_digest": expected,
                "actual_digest": actual,
            },
        )
    return actual
