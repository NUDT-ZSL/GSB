from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .errors import DomainError, ErrorCode

SUPPORTED_ACC_DTYPES = frozenset({"int32"})


def _as_pair(value: Any, field_name: str, *, allow_zero: bool) -> tuple[int, int]:
    if isinstance(value, bool):
        raise DomainError(
            ErrorCode.E_INVALID_STRIDE_PADDING,
            f"{field_name} must be an integer or a pair of integers",
        )
    if isinstance(value, int):
        pair = (value, value)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            raise DomainError(
                ErrorCode.E_INVALID_STRIDE_PADDING,
                f"{field_name} must contain integers",
            )
        pair = (int(value[0]), int(value[1]))
    else:
        raise DomainError(
            ErrorCode.E_INVALID_STRIDE_PADDING,
            f"{field_name} must be an integer or a pair of integers",
        )
    minimum = 0 if allow_zero else 1
    if any(item < minimum for item in pair):
        relation = "non-negative" if allow_zero else "positive"
        raise DomainError(
            ErrorCode.E_INVALID_STRIDE_PADDING,
            f"{field_name} must be {relation}",
        )
    return pair


def _as_shape(value: Any, *, kernel: bool) -> tuple[int, int, int, int]:
    code = ErrorCode.E_INVALID_KERNEL_SHAPE if kernel else ErrorCode.E_INVALID_INPUT_SHAPE
    name = "kernel_shape" if kernel else "input_shape"
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise DomainError(code, f"{name} must contain exactly four dimensions")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise DomainError(code, f"{name} dimensions must be integers")
    shape = tuple(int(item) for item in value)
    if any(item <= 0 for item in shape):
        raise DomainError(code, f"{name} dimensions must be positive")
    return shape  # type: ignore[return-value]


def _as_int(value: Any, field_name: str, code: ErrorCode) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainError(code, f"{field_name} must be an integer")
    return int(value)


def _as_number(value: Any, field_name: str, code: ErrorCode) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainError(code, f"{field_name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise DomainError(code, f"{field_name} must be finite")
    return number


@dataclass(frozen=True)
class Job:
    job_id: str
    input_shape: tuple[int, int, int, int]
    kernel_shape: tuple[int, int, int, int]
    stride: tuple[int, int]
    padding: tuple[int, int]
    quant_scale: float
    zero_point: int
    acc_dtype: str
    seed: int
    priority: int

    @property
    def output_shape(self) -> tuple[int, int, int, int]:
        n, _, h, w = self.input_shape
        k, _, kh, kw = self.kernel_shape
        sh, sw = self.stride
        ph, pw = self.padding
        out_h = (h + 2 * ph - kh) // sh + 1
        out_w = (w + 2 * pw - kw) // sw + 1
        if out_h <= 0 or out_w <= 0:
            raise DomainError(
                ErrorCode.E_INVALID_KERNEL_SHAPE,
                "kernel does not fit the padded input",
                job_id=self.job_id,
            )
        return n, k, out_h, out_w

    @property
    def output_elements(self) -> int:
        return math.prod(self.output_shape)

    @property
    def mac_count(self) -> int:
        n, k, oh, ow = self.output_shape
        c, kh, kw = self.kernel_shape[1:]
        return n * k * oh * ow * c * kh * kw

    @property
    def terms_per_output(self) -> int:
        return math.prod(self.kernel_shape[1:])

    @property
    def quant_tier(self) -> str:
        if self.quant_scale <= 0.02:
            return "fine"
        if self.quant_scale <= 0.06:
            return "balanced"
        return "coarse"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "input_shape": list(self.input_shape),
            "kernel_shape": list(self.kernel_shape),
            "stride": list(self.stride),
            "padding": list(self.padding),
            "quant_scale": float(self.quant_scale),
            "zero_point": int(self.zero_point),
            "acc_dtype": self.acc_dtype,
            "seed": int(self.seed),
            "priority": int(self.priority),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Job":
        if not isinstance(raw, dict):
            raise DomainError(ErrorCode.E_INVALID_JOB_FIELD, "job must be a JSON object")
        job_id = raw.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise DomainError(ErrorCode.E_INVALID_JOB_FIELD, "job_id must be a non-empty string")
        input_shape = _as_shape(raw.get("input_shape"), kernel=False)
        kernel_shape = _as_shape(raw.get("kernel_shape"), kernel=True)
        if kernel_shape[1] != input_shape[1]:
            raise DomainError(
                ErrorCode.E_INVALID_KERNEL_SHAPE,
                "kernel input-channel dimension must equal input C",
                job_id=job_id,
            )
        stride = _as_pair(raw.get("stride", 1), "stride", allow_zero=False)
        padding = _as_pair(raw.get("padding", 0), "padding", allow_zero=True)
        quant_scale = _as_number(
            raw.get("quant_scale"), "quant_scale", ErrorCode.E_INVALID_QUANT_SCALE
        )
        if quant_scale <= 0:
            raise DomainError(
                ErrorCode.E_INVALID_QUANT_SCALE,
                "quant_scale must be positive",
                job_id=job_id,
            )
        zero_point = _as_int(
            raw.get("zero_point"), "zero_point", ErrorCode.E_INVALID_ZERO_POINT
        )
        if not -128 <= zero_point <= 127:
            raise DomainError(
                ErrorCode.E_INVALID_ZERO_POINT,
                "zero_point must be within [-128, 127]",
                job_id=job_id,
            )
        acc_dtype = raw.get("acc_dtype")
        if not isinstance(acc_dtype, str) or acc_dtype.lower() not in SUPPORTED_ACC_DTYPES:
            raise DomainError(
                ErrorCode.E_UNSUPPORTED_ACC_DTYPE,
                f"acc_dtype must be one of {sorted(SUPPORTED_ACC_DTYPES)}",
                job_id=job_id,
            )
        seed = _as_int(raw.get("seed"), "seed", ErrorCode.E_INVALID_JOB_FIELD)
        if seed < 0:
            raise DomainError(ErrorCode.E_INVALID_JOB_FIELD, "seed must be non-negative")
        priority = _as_int(raw.get("priority"), "priority", ErrorCode.E_INVALID_JOB_FIELD)
        job = cls(
            job_id=job_id,
            input_shape=input_shape,
            kernel_shape=kernel_shape,
            stride=stride,
            padding=padding,
            quant_scale=quant_scale,
            zero_point=zero_point,
            acc_dtype=acc_dtype.lower(),
            seed=seed,
            priority=priority,
        )
        # Force shape calculation here so malformed kernels fail during validation.
        _ = job.output_shape
        return job


@dataclass(frozen=True)
class Budget:
    mac_budget: int
    memory_budget_bytes: int
    worker_slots: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.mac_budget, bool)
            or not isinstance(self.mac_budget, int)
            or self.mac_budget <= 0
        ):
            raise DomainError(ErrorCode.E_INVALID_BUDGET, "mac_budget must be positive")
        if (
            isinstance(self.memory_budget_bytes, bool)
            or not isinstance(self.memory_budget_bytes, int)
            or self.memory_budget_bytes <= 0
        ):
            raise DomainError(
                ErrorCode.E_INVALID_BUDGET, "memory_budget_bytes must be positive"
            )
        if (
            isinstance(self.worker_slots, bool)
            or not isinstance(self.worker_slots, int)
            or self.worker_slots <= 0
        ):
            raise DomainError(ErrorCode.E_INVALID_BUDGET, "worker_slots must be positive")

    def to_dict(self) -> dict[str, int]:
        return {
            "mac_budget": self.mac_budget,
            "memory_budget_bytes": self.memory_budget_bytes,
            "worker_slots": self.worker_slots,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Budget":
        if not isinstance(raw, dict):
            raise DomainError(ErrorCode.E_INVALID_BUDGET, "budget must be an object")
        return cls(
            mac_budget=_as_int(
                raw.get("mac_budget"), "mac_budget", ErrorCode.E_INVALID_BUDGET
            ),
            memory_budget_bytes=_as_int(
                raw.get("memory_budget_bytes"),
                "memory_budget_bytes",
                ErrorCode.E_INVALID_BUDGET,
            ),
            worker_slots=_as_int(
                raw.get("worker_slots"), "worker_slots", ErrorCode.E_INVALID_BUDGET
            ),
        )


@dataclass(frozen=True)
class SchedulerConfig:
    max_workers_per_task: int = 4
    overflow_protection: bool = True
    max_segment_terms: int | None = None
    allow_coarse_reduced_reduction: bool = True
    priority_policy: tuple[str, ...] = (
        "task_priority_desc",
        "quant_precision_desc",
        "progress",
        "memory",
        "parallelism",
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_workers_per_task, bool)
            or not isinstance(self.max_workers_per_task, int)
            or self.max_workers_per_task <= 0
        ):
            raise DomainError(
                ErrorCode.E_INVALID_WORKER_CONFIG,
                "max_workers_per_task must be positive",
            )
        if self.max_segment_terms is not None and (
            isinstance(self.max_segment_terms, bool)
            or not isinstance(self.max_segment_terms, int)
            or self.max_segment_terms <= 0
        ):
            raise DomainError(
                ErrorCode.E_INVALID_WORKER_CONFIG,
                "max_segment_terms must be positive when set",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_workers_per_task": self.max_workers_per_task,
            "overflow_protection": self.overflow_protection,
            "max_segment_terms": self.max_segment_terms,
            "allow_coarse_reduced_reduction": self.allow_coarse_reduced_reduction,
            "priority_policy": list(self.priority_policy),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SchedulerConfig":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise DomainError(
                ErrorCode.E_INVALID_WORKER_CONFIG, "scheduler config must be an object"
            )
        known = {
            "max_workers_per_task",
            "overflow_protection",
            "max_segment_terms",
            "allow_coarse_reduced_reduction",
            "priority_policy",
        }
        unknown = sorted(set(raw) - known)
        if unknown:
            raise DomainError(
                ErrorCode.E_INVALID_WORKER_CONFIG,
                f"unknown scheduler config keys: {unknown}",
            )
        overflow = raw.get("overflow_protection", True)
        allow_reduced = raw.get("allow_coarse_reduced_reduction", True)
        if not isinstance(overflow, bool) or not isinstance(allow_reduced, bool):
            raise DomainError(
                ErrorCode.E_INVALID_WORKER_CONFIG,
                "boolean scheduling options must be true or false",
            )
        policy = raw.get(
            "priority_policy",
            [
                "task_priority_desc",
                "quant_precision_desc",
                "progress",
                "memory",
                "parallelism",
            ],
        )
        if not isinstance(policy, (list, tuple)) or not all(
            isinstance(item, str) for item in policy
        ):
            raise DomainError(
                ErrorCode.E_INVALID_WORKER_CONFIG,
                "priority_policy must be a list of strings",
            )
        max_segment = raw.get("max_segment_terms")
        if max_segment is not None:
            max_segment = _as_int(
                max_segment, "max_segment_terms", ErrorCode.E_INVALID_WORKER_CONFIG
            )
        return cls(
            max_workers_per_task=_as_int(
                raw.get("max_workers_per_task", 4),
                "max_workers_per_task",
                ErrorCode.E_INVALID_WORKER_CONFIG,
            ),
            overflow_protection=overflow,
            max_segment_terms=max_segment,
            allow_coarse_reduced_reduction=allow_reduced,
            priority_policy=tuple(policy),
        )


@dataclass(frozen=True)
class ToleranceConfig:
    atol: float = 0.05
    rtol: float = 0.01
    max_violations: int = 256

    def __post_init__(self) -> None:
        if (
            isinstance(self.atol, bool)
            or not isinstance(self.atol, (int, float))
            or not math.isfinite(float(self.atol))
            or self.atol < 0
        ):
            raise DomainError(
                ErrorCode.E_INVALID_BUDGET, "tolerance atol must be finite and non-negative"
            )
        if (
            isinstance(self.rtol, bool)
            or not isinstance(self.rtol, (int, float))
            or not math.isfinite(float(self.rtol))
            or self.rtol < 0
        ):
            raise DomainError(
                ErrorCode.E_INVALID_BUDGET, "tolerance rtol must be finite and non-negative"
            )
        if (
            isinstance(self.max_violations, bool)
            or not isinstance(self.max_violations, int)
            or self.max_violations <= 0
        ):
            raise DomainError(
                ErrorCode.E_INVALID_BUDGET, "max_violations must be positive"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "atol": float(self.atol),
            "rtol": float(self.rtol),
            "max_violations": int(self.max_violations),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToleranceConfig":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise DomainError(ErrorCode.E_INVALID_BUDGET, "tolerance must be an object")
        return cls(
            atol=float(raw.get("atol", 0.05)),
            rtol=float(raw.get("rtol", 0.01)),
            max_violations=int(raw.get("max_violations", 256)),
        )


@dataclass
class ExecutionConfig:
    max_processes: int
    fault_injections: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_processes, bool)
            or not isinstance(self.max_processes, int)
            or self.max_processes <= 0
        ):
            raise DomainError(
                ErrorCode.E_INVALID_WORKER_CONFIG, "max_processes must be positive"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_processes": self.max_processes,
            "fault_injections": [dict(item) for item in self.fault_injections],
        }

