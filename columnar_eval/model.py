from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


_KINDS = {"int64", "decimal", "string", "bool", "null"}


def format_scaled(raw: int, scale: int) -> str:
    """Render a scaled integer without floating-point conversion."""
    if scale == 0:
        return str(raw)
    sign = "-" if raw < 0 else ""
    digits = str(abs(raw))
    if len(digits) <= scale:
        digits = "0" * (scale + 1 - len(digits)) + digits
    return f"{sign}{digits[:-scale]}.{digits[-scale:]}"


def mask_to_bitstring(mask: Iterable[bool], bit_order: str = "msb_first") -> str:
    bits = "".join("1" if flag else "0" for flag in mask)
    return bits if bit_order == "msb_first" else bits[::-1]


def bitstring_to_mask(bits: str, length: int, bit_order: str = "msb_first") -> list[bool]:
    if len(bits) < length:
        raise ValueError(f"null bitmap has {len(bits)} bits, expected at least {length}")
    if bit_order == "msb_first":
        ordered = bits[:length]
    elif bit_order == "lsb_first":
        ordered = bits[:length][::-1]
    else:
        raise ValueError(f"unsupported bit order: {bit_order}")
    return [ch == "1" for ch in ordered]


@dataclass(frozen=True)
class DType:
    kind: str
    scale: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unsupported type kind: {self.kind}")
        if self.kind == "decimal":
            if self.scale is None or self.scale < 0:
                raise ValueError("decimal type requires a non-negative scale")
        elif self.scale is not None:
            raise ValueError(f"{self.kind} must not carry a decimal scale")

    @property
    def label(self) -> str:
        if self.kind == "decimal":
            return f"decimal(scale={self.scale})"
        return self.kind

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        if self.kind == "decimal":
            payload["scale"] = self.scale
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any] | str) -> "DType":
        if isinstance(payload, str):
            return cls(payload)
        return cls(kind=payload["kind"], scale=payload.get("scale"))


INT64 = DType("int64")
STRING = DType("string")
BOOL = DType("bool")
NULL = DType("null")


@dataclass
class ColumnVector:
    dtype: DType
    values: list[Any]
    null_mask: list[bool]
    source_field: str | None = None

    def __post_init__(self) -> None:
        if len(self.values) != len(self.null_mask):
            raise ValueError(
                f"vector length mismatch: values={len(self.values)} "
                f"mask={len(self.null_mask)}"
            )
        for idx, is_null in enumerate(self.null_mask):
            if not isinstance(is_null, bool):
                raise TypeError(f"null mask item {idx} is not bool")
            if is_null and self.values[idx] is not None:
                raise ValueError(f"null vector item {idx} must store None")
            if not is_null and self.values[idx] is None:
                raise ValueError(f"non-null vector item {idx} must not store None")
        if self.dtype.kind == "decimal":
            for idx, (value, is_null) in enumerate(zip(self.values, self.null_mask)):
                if not is_null and not isinstance(value, int):
                    raise TypeError(
                        f"decimal raw item {idx} must be int, got {type(value).__name__}"
                    )
        elif self.dtype.kind == "int64":
            for idx, (value, is_null) in enumerate(zip(self.values, self.null_mask)):
                if not is_null and (not isinstance(value, int) or isinstance(value, bool)):
                    raise TypeError(f"int64 item {idx} must be int")
        elif self.dtype.kind == "bool":
            for idx, (value, is_null) in enumerate(zip(self.values, self.null_mask)):
                if not is_null and not isinstance(value, bool):
                    raise TypeError(f"bool item {idx} must be bool")
        elif self.dtype.kind == "string":
            for idx, (value, is_null) in enumerate(zip(self.values, self.null_mask)):
                if not is_null and not isinstance(value, str):
                    raise TypeError(f"string item {idx} must be str")

    def __len__(self) -> int:
        return len(self.values)

    @property
    def null_count(self) -> int:
        return sum(self.null_mask)

    @property
    def null_bitmap(self) -> str:
        return mask_to_bitstring(self.null_mask, "msb_first")

    def subset(self, indices: Iterable[int]) -> "ColumnVector":
        selected = list(indices)
        return ColumnVector(
            dtype=self.dtype,
            values=[self.values[idx] for idx in selected],
            null_mask=[self.null_mask[idx] for idx in selected],
            source_field=self.source_field,
        )

    def with_metadata(self, source_field: str | None = None) -> "ColumnVector":
        return ColumnVector(
            dtype=self.dtype,
            values=list(self.values),
            null_mask=list(self.null_mask),
            source_field=self.source_field if source_field is None else source_field,
        )

    def to_json(self, include_display: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dtype": self.dtype.to_json(),
            "dtype_label": self.dtype.label,
            "null_mask": list(self.null_mask),
            "null_bitmap": self.null_bitmap,
            "null_count": self.null_count,
        }
        if self.dtype.kind == "decimal":
            scale = int(self.dtype.scale or 0)
            payload["raw_values"] = list(self.values)
            if include_display:
                payload["values"] = [
                    None if is_null else format_scaled(value, scale)
                    for value, is_null in zip(self.values, self.null_mask)
                ]
            else:
                payload["values"] = list(self.values)
        else:
            payload["values"] = list(self.values)
        if self.source_field is not None:
            payload["source_field"] = self.source_field
        return payload


@dataclass
class EvalBatch:
    batch_index: int
    row_start: int
    row_count: int
    columns: dict[str, ColumnVector] = field(default_factory=dict)

    def validate(self) -> None:
        if self.row_count < 0:
            raise ValueError("batch row_count must be non-negative")
        for name, vector in self.columns.items():
            if len(vector) != self.row_count:
                raise ValueError(
                    f"column {name} has {len(vector)} rows, batch declares {self.row_count}"
                )
        if not self.columns:
            raise ValueError("batch must contain at least one column")


class EvaluationError(Exception):
    """Raised when an expression cannot be evaluated under the declared model."""


class BudgetRejected(EvaluationError):
    """Raised instead of truncating an over-budget batch."""

    def __init__(
        self,
        *,
        stage: str,
        metric: str,
        observed: int,
        limit: int,
        policy: str,
        batch_index: int | None = None,
    ) -> None:
        self.stage = stage
        self.metric = metric
        self.observed = observed
        self.limit = limit
        self.policy = policy
        self.batch_index = batch_index
        super().__init__(
            f"{policy}: {stage} exceeded {metric}: observed={observed}, limit={limit}"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "rejected": True,
            "policy": self.policy,
            "stage": self.stage,
            "metric": self.metric,
            "observed": self.observed,
            "limit": self.limit,
            "batch_index": self.batch_index,
            "message": str(self),
        }


@dataclass(frozen=True)
class BatchBudget:
    max_rows: int
    max_input_columns: int
    max_working_columns: int
    max_cells: int | None = None
    rejection_policy: str = "reject"

    def __post_init__(self) -> None:
        if self.max_rows <= 0:
            raise ValueError("max_rows must be positive")
        if self.max_input_columns <= 0:
            raise ValueError("max_input_columns must be positive")
        if self.max_working_columns < self.max_input_columns:
            raise ValueError("max_working_columns must be >= max_input_columns")
        if self.max_cells is not None and self.max_cells <= 0:
            raise ValueError("max_cells must be positive when supplied")

    def check_input(
        self,
        *,
        batch_index: int | None,
        row_count: int,
        input_columns: int,
    ) -> None:
        if row_count > self.max_rows:
            raise BudgetRejected(
                stage="batch_input",
                metric="rows",
                observed=row_count,
                limit=self.max_rows,
                policy=self.rejection_policy,
                batch_index=batch_index,
            )
        if input_columns > self.max_input_columns:
            raise BudgetRejected(
                stage="batch_input",
                metric="input_columns",
                observed=input_columns,
                limit=self.max_input_columns,
                policy=self.rejection_policy,
                batch_index=batch_index,
            )
        if self.max_cells is not None and row_count * input_columns > self.max_cells:
            raise BudgetRejected(
                stage="batch_input",
                metric="input_cells",
                observed=row_count * input_columns,
                limit=self.max_cells,
                policy=self.rejection_policy,
                batch_index=batch_index,
            )

    def check_working(
        self,
        *,
        batch_index: int | None,
        row_count: int,
        working_columns: int,
    ) -> None:
        if working_columns > self.max_working_columns:
            raise BudgetRejected(
                stage="expression_working_set",
                metric="working_columns",
                observed=working_columns,
                limit=self.max_working_columns,
                policy=self.rejection_policy,
                batch_index=batch_index,
            )
        if self.max_cells is not None and row_count * working_columns > self.max_cells:
            raise BudgetRejected(
                stage="expression_working_set",
                metric="working_cells",
                observed=row_count * working_columns,
                limit=self.max_cells,
                policy=self.rejection_policy,
                batch_index=batch_index,
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "max_rows": self.max_rows,
            "max_input_columns": self.max_input_columns,
            "max_working_columns": self.max_working_columns,
            "max_cells": self.max_cells,
            "rejection_policy": self.rejection_policy,
        }
