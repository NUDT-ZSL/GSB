"""Public data contracts for metadata and runtime budgets."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .errors import SparseOperatorError
from .codes import E_CONFIG_INVALID


SUPPORTED_DTYPES = frozenset({"float32", "float64"})
SUPPORTED_KINDS = frozenset({"spmm", "spspmm"})


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    kind: str
    shape_mnk: tuple[int, int, int]
    batch_size: int
    nnz_a: int
    nnz_b: int
    dtype: str
    seed: int
    block_candidates: tuple[int, ...]
    generation: dict[str, Any] = field(default_factory=dict)
    checksums: dict[str, Any] = field(default_factory=dict)
    b_storage: str = "sparse"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def m(self) -> int:
        return self.shape_mnk[0]

    @property
    def k(self) -> int:
        return self.shape_mnk[1]

    @property
    def n(self) -> int:
        return self.shape_mnk[2]


@dataclass(frozen=True)
class Operands:
    a: tuple[Any, ...]
    b_sparse: tuple[Any, ...] | None = None
    b_dense: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class OperatorConfig:
    atol: float = 1e-12
    rtol: float = 1e-10
    max_memory_bytes: int = 64 * 1024 * 1024
    max_blocks: int = 4096
    max_aggregation_bytes: int = 8 * 1024 * 1024
    compensated_sum: bool = True
    resource_policy: str = "degrade"
    strategy_priority: str = "precision_first"
    allow_aggregation: bool = True
    enable_self_check: bool = True
    diagnose: bool = True
    max_reported_violations: int = 8
    max_self_check_elements: int = 2_000_000
    aggregation_entry_bytes: int = 24
    forced_row_block: int | None = None
    forced_accumulation_order: str | None = None
    forced_aggregation: bool | None = None

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None) -> "OperatorConfig":
        values = dict(values or {})
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise SparseOperatorError(
                E_CONFIG_INVALID,
                "unknown configuration field(s)",
                phase="configuration",
                field="config",
                details={"unknown_fields": unknown},
            )
        try:
            cfg = cls(**values)
        except (TypeError, ValueError) as error:
            raise SparseOperatorError(
                E_CONFIG_INVALID,
                "configuration values have invalid types",
                phase="configuration",
                field="config",
                details={"error": str(error)},
            ) from error
        cfg.validate()
        return cfg

    def validate(self) -> None:
        for name in ("atol", "rtol"):
            raw_value = getattr(self, name)
            if isinstance(raw_value, bool) or not isinstance(
                raw_value, (int, float)
            ):
                raise SparseOperatorError(
                    E_CONFIG_INVALID,
                    f"{name} must be a finite non-negative number",
                    phase="configuration",
                    field=name,
                )
            value = float(raw_value)
            if not math.isfinite(value) or value < 0.0:
                raise SparseOperatorError(
                    E_CONFIG_INVALID,
                    f"{name} must be a finite non-negative number",
                    phase="configuration",
                    field=name,
                )
        for name in (
            "max_memory_bytes",
            "max_blocks",
            "max_aggregation_bytes",
            "max_reported_violations",
            "max_self_check_elements",
            "aggregation_entry_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SparseOperatorError(
                    E_CONFIG_INVALID,
                    f"{name} must be a positive integer",
                    phase="configuration",
                    field=name,
                )
        if self.resource_policy not in {"degrade", "error"}:
            raise SparseOperatorError(
                E_CONFIG_INVALID,
                "resource_policy must be 'degrade' or 'error'",
                phase="configuration",
                field="resource_policy",
            )
        if self.strategy_priority not in {"precision_first", "memory_first"}:
            raise SparseOperatorError(
                E_CONFIG_INVALID,
                "strategy_priority must be 'precision_first' or 'memory_first'",
                phase="configuration",
                field="strategy_priority",
            )
        if self.forced_accumulation_order not in {None, "forward", "reverse", "pairwise"}:
            raise SparseOperatorError(
                E_CONFIG_INVALID,
                "forced_accumulation_order must be forward, reverse or pairwise",
                phase="configuration",
                field="forced_accumulation_order",
            )
        for name in (
            "compensated_sum",
            "allow_aggregation",
            "enable_self_check",
            "diagnose",
        ):
            if not isinstance(getattr(self, name), bool):
                raise SparseOperatorError(
                    E_CONFIG_INVALID,
                    f"{name} must be boolean",
                    phase="configuration",
                    field=name,
                )
        if self.forced_aggregation is not None and not isinstance(
            self.forced_aggregation, bool
        ):
            raise SparseOperatorError(
                E_CONFIG_INVALID,
                "forced_aggregation must be boolean or null",
                phase="configuration",
                field="forced_aggregation",
            )
        if (
            self.forced_accumulation_order == "pairwise"
            and self.compensated_sum
        ):
            raise SparseOperatorError(
                E_CONFIG_INVALID,
                "pairwise reduction is only supported when compensated_sum is false",
                phase="configuration",
                field="forced_accumulation_order",
            )
        if self.forced_row_block is not None and (
            not isinstance(self.forced_row_block, int)
            or isinstance(self.forced_row_block, bool)
            or self.forced_row_block <= 0
        ):
            raise SparseOperatorError(
                E_CONFIG_INVALID,
                "forced_row_block must be a positive integer",
                phase="configuration",
                field="forced_row_block",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "atol": self.atol,
            "rtol": self.rtol,
            "max_memory_bytes": self.max_memory_bytes,
            "max_blocks": self.max_blocks,
            "max_aggregation_bytes": self.max_aggregation_bytes,
            "compensated_sum": self.compensated_sum,
            "resource_policy": self.resource_policy,
            "strategy_priority": self.strategy_priority,
            "allow_aggregation": self.allow_aggregation,
            "enable_self_check": self.enable_self_check,
            "diagnose": self.diagnose,
            "max_reported_violations": self.max_reported_violations,
            "max_self_check_elements": self.max_self_check_elements,
            "aggregation_entry_bytes": self.aggregation_entry_bytes,
            "forced_row_block": self.forced_row_block,
            "forced_accumulation_order": self.forced_accumulation_order,
            "forced_aggregation": self.forced_aggregation,
        }

