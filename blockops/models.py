"""Task, configuration, and result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from .errors import Diagnostic
from .util import sha256_json


REQUIRED_TASK_FIELDS = (
    "job_id",
    "op_kind",
    "input_shape",
    "block_size",
    "axis",
    "eps",
    "dtype",
    "seed",
    "norm_mode",
)


@dataclass(frozen=True)
class OperatorTask:
    raw: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OperatorTask":
        return cls(raw=dict(raw))

    @property
    def job_id(self) -> str:
        value = self.raw.get("job_id", "<missing>")
        return value if isinstance(value, str) else str(value)

    @property
    def op_kind(self) -> Any:
        return self.raw.get("op_kind")

    @property
    def input_shape(self) -> Any:
        return self.raw.get("input_shape")

    @property
    def block_size(self) -> Any:
        return self.raw.get("block_size")

    @property
    def axis(self) -> Any:
        return self.raw.get("axis")

    @property
    def eps(self) -> Any:
        return self.raw.get("eps")

    @property
    def dtype(self) -> Any:
        return self.raw.get("dtype")

    @property
    def seed(self) -> Any:
        return self.raw.get("seed")

    @property
    def norm_mode(self) -> Any:
        return self.raw.get("norm_mode")

    def parameter_summary(self) -> dict[str, Any]:
        return {name: self.raw.get(name) for name in REQUIRED_TASK_FIELDS}


@dataclass(frozen=True)
class ToleranceConfig:
    abs_tol: float = 1e-12
    rel_tol: float = 1e-12
    relative_floor: float = 1e-300

    def __post_init__(self) -> None:
        if self.abs_tol < 0 or self.rel_tol < 0 or self.relative_floor <= 0:
            raise ValueError("tolerance values must be non-negative with a positive floor")

    def to_dict(self) -> dict[str, float]:
        return {
            "abs_tol": self.abs_tol,
            "rel_tol": self.rel_tol,
            "relative_floor": self.relative_floor,
        }


@dataclass(frozen=True)
class ResourceLimits:
    max_blocks: int | None = 100_000
    max_block_elements: int | None = 1_000_000
    max_stat_bytes: int | None = 64 * 1024 * 1024
    allow_block_shrink: bool = True
    allow_float32_stats: bool = True

    def __post_init__(self) -> None:
        for name in ("max_blocks", "max_block_elements", "max_stat_bytes"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_blocks": self.max_blocks,
            "max_block_elements": self.max_block_elements,
            "max_stat_bytes": self.max_stat_bytes,
            "allow_block_shrink": self.allow_block_shrink,
            "allow_float32_stats": self.allow_float32_stats,
        }


@dataclass(frozen=True)
class FaultParameters:
    """Injection values supplied through public parameters, never source patches."""

    stat_overflow_scale: float | None = None
    write_index_offset: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stat_overflow_scale": self.stat_overflow_scale,
            "write_index_offset": self.write_index_offset,
        }


@dataclass(frozen=True)
class ExecutionOptions:
    tolerance: ToleranceConfig = field(default_factory=ToleranceConfig)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    eps_floor: float = 1e-5
    faults: FaultParameters = field(default_factory=FaultParameters)

    def __post_init__(self) -> None:
        if not np.isfinite(self.eps_floor) or self.eps_floor <= 0:
            raise ValueError("eps_floor must be finite and positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tolerance": self.tolerance.to_dict(),
            "limits": self.limits.to_dict(),
            "eps_floor": self.eps_floor,
            "faults": self.faults.to_dict(),
        }

    def sha256(self) -> str:
        return sha256_json(self.to_dict())


@dataclass
class BlockRecord:
    block_id: str
    axis_start: int
    axis_end: int
    element_indices: list[int]
    rows: list[dict[str, Any]]
    statistics: dict[str, Any]
    output_values: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "axis_start": self.axis_start,
            "axis_end": self.axis_end,
            "element_indices": list(self.element_indices),
            "rows": [dict(row) for row in self.rows],
            "statistics": dict(self.statistics),
            "output_values": list(self.output_values),
        }


@dataclass
class OperatorResult:
    job_id: str
    task: OperatorTask
    op_kind: str | None
    status: str
    output: np.ndarray | None
    reference: np.ndarray | None
    plan: dict[str, Any]
    blocks: list[BlockRecord]
    diagnostics: list[Diagnostic]
    degradations: list[dict[str, Any]]
    tolerance_report: dict[str, Any] | None
    stage_indices: dict[str, set[int]]
    self_check: dict[str, Any] | None = None
    unattributed_errors: list[dict[str, Any]] = field(default_factory=list)
    compute_dtype: str = "float64"

    def business_codes(self) -> list[str]:
        seen: set[str] = set()
        codes: list[str] = []
        for diagnostic in self.diagnostics:
            if diagnostic.code not in seen:
                seen.add(diagnostic.code)
                codes.append(diagnostic.code)
        return codes
