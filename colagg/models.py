from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, Sequence

from .canonical import (
    canonical_dumps,
    float_hex,
    json_float_cell,
    normalize_float_cell,
    sha256_json,
)
from .errors import E_BAD_BATCH, E_BAD_QUERY, EngineError


SCALAR_TYPES = {"int", "float", "bool", "string"}
NUMERIC_TYPES = {"int", "float"}
AGGREGATE_OPS = {"sum", "mean", "variance", "count"}
PHASES = {"read", "aggregate", "window", "write"}
INJECTION_KINDS = {
    "malformed_column",
    "digest_mismatch",
    "fixed_overflow",
    "window_corrupt",
    "spill_tamper",
    "spill_readback_corrupt",
    "crash_after_spill",
    "crash_before_commit",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str
    values: tuple[Any, ...]

    def external_values(self) -> list[Any]:
        if self.type == "float":
            return [json_float_cell(value) for value in self.values]
        return list(self.values)

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "values": self.external_values(),
        }


@dataclass(frozen=True, slots=True)
class Batch:
    batch_seq: int
    row_count: int
    columns: tuple[Column, ...]
    digest: str
    payload: dict[str, Any] = field(compare=False)

    @property
    def by_name(self) -> dict[str, Column]:
        return {column.name: column for column in self.columns}

    def column(self, name: str) -> Column:
        try:
            return self.by_name[name]
        except KeyError as exc:
            raise EngineError(
                E_BAD_QUERY,
                f"referenced column {name!r} does not exist",
                column=name,
                batch_seq=self.batch_seq,
            ) from exc


@dataclass(frozen=True, slots=True)
class Aggregate:
    name: str
    op: str
    column: str


@dataclass(frozen=True, slots=True)
class WindowSpec:
    length: int
    stride: int
    column: str
    outputs: tuple[str, ...] = ("count", "sum", "mean", "variance")


@dataclass(frozen=True, slots=True)
class Query:
    query_id: str
    group_by: tuple[str, ...]
    aggregates: tuple[Aggregate, ...]
    window: WindowSpec

    @property
    def value_columns(self) -> tuple[str, ...]:
        names = {aggregate.column for aggregate in self.aggregates if aggregate.op != "count"}
        names.add(self.window.column)
        return tuple(sorted(names))

    @property
    def aggregate_columns(self) -> tuple[str, ...]:
        names = {aggregate.column for aggregate in self.aggregates if aggregate.op != "count"}
        return tuple(sorted(names))

    def as_payload(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "group_by": list(self.group_by),
            "aggregates": [
                {"name": item.name, "op": item.op, "column": item.column}
                for item in self.aggregates
            ],
            "window": {
                "length": self.window.length,
                "stride": self.window.stride,
                "column": self.window.column,
                "outputs": list(self.window.outputs),
            },
        }


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    memory_budget_mib: int
    max_batch_rows: int
    spill_dir: str
    fixed_scale: int
    fixed_int_bits: int
    float_tolerance: float
    logical_clock_step: int
    numeric_mode: str

    @property
    def memory_budget_bytes(self) -> int:
        return self.memory_budget_mib * 1024 * 1024

    @property
    def fixed_min(self) -> int:
        return -(1 << (self.fixed_int_bits - 1))

    @property
    def fixed_max(self) -> int:
        return (1 << (self.fixed_int_bits - 1)) - 1

    def as_payload(self) -> dict[str, Any]:
        return {
            "memory_budget_mib": self.memory_budget_mib,
            "max_batch_rows": self.max_batch_rows,
            "spill_dir": self.spill_dir,
            "fixed_scale": self.fixed_scale,
            "fixed_int_bits": self.fixed_int_bits,
            "float_tolerance": self.float_tolerance,
            "logical_clock_step": self.logical_clock_step,
            "numeric_mode": self.numeric_mode,
        }


@dataclass(frozen=True, slots=True)
class Injection:
    injection_id: str
    phase: str
    batch_seq: int
    kind: str
    column: str | None = None
    group: tuple[Any, ...] | None = None
    target: str | None = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.injection_id,
            "phase": self.phase,
            "batch_seq": self.batch_seq,
            "kind": self.kind,
        }
        if self.column is not None:
            payload["column"] = self.column
        if self.group is not None:
            payload["group"] = list(self.group)
        if self.target is not None:
            payload["target"] = self.target
        return payload


@dataclass(frozen=True, slots=True)
class InjectionManifest:
    injections: tuple[Injection, ...] = ()

    def matching(
        self,
        phase: str,
        batch_seq: int,
        kind: str | None = None,
    ) -> tuple[Injection, ...]:
        return tuple(
            item
            for item in self.injections
            if item.phase == phase
            and item.batch_seq == batch_seq
            and (kind is None or item.kind == kind)
        )

    def as_payload(self) -> dict[str, Any]:
        return {"injections": [item.as_payload() for item in self.injections]}


@dataclass(frozen=True, slots=True)
class Plan:
    query: Query
    config: ExecutionConfig
    manifest: InjectionManifest
    plan_digest: str

    @classmethod
    def build(
        cls,
        query: Query,
        config: ExecutionConfig,
        manifest: InjectionManifest,
    ) -> "Plan":
        payload = {
            "query": query.as_payload(),
            "config": config.as_payload(),
            "manifest": manifest.as_payload(),
        }
        return cls(query, config, manifest, sha256_json(payload))

    def as_payload(self) -> dict[str, Any]:
        return {
            "query": self.query.as_payload(),
            "config": self.config.as_payload(),
            "manifest": self.manifest.as_payload(),
            "plan_digest": self.plan_digest,
        }


@dataclass(slots=True)
class StatState:
    total_count: int = 0
    finite_count: int = 0
    nan_count: int = 0
    pos_inf_count: int = 0
    neg_inf_count: int = 0
    total: float | int = 0
    square_total: float | int = 0

    def as_payload(self, mode: str) -> dict[str, Any]:
        return {
            "total_count": self.total_count,
            "finite_count": self.finite_count,
            "nan_count": self.nan_count,
            "pos_inf_count": self.pos_inf_count,
            "neg_inf_count": self.neg_inf_count,
            "total": self.total,
            "square_total": self.square_total,
        }


@dataclass(slots=True)
class GroupState:
    key: tuple[Any, ...]
    group_hash: str
    version: int
    row_count: int
    stats: dict[str, StatState]
    window_values: list[float]
    window_base: int
    last_touch: int
    status: str = "active"
    failed_code: str | None = None
    dirty: bool = False
    loaded_version: int = 0

    def as_payload(self, mode: str) -> dict[str, Any]:
        return {
            "key": list(self.key),
            "group_hash": self.group_hash,
            "version": self.version,
            "row_count": self.row_count,
            "stats": {
                name: stat.as_payload(mode)
                for name, stat in sorted(self.stats.items())
            },
            "window_values": [json_float_cell(value) for value in self.window_values],
            "window_base": self.window_base,
            "last_touch": self.last_touch,
            "status": self.status,
            "failed_code": self.failed_code,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        mode: str,
    ) -> "GroupState":
        stats: dict[str, StatState] = {}
        for name, raw in payload["stats"].items():
            stats[name] = StatState(
                total_count=int(raw["total_count"]),
                finite_count=int(raw["finite_count"]),
                nan_count=int(raw["nan_count"]),
                pos_inf_count=int(raw["pos_inf_count"]),
                neg_inf_count=int(raw["neg_inf_count"]),
                total=raw["total"],
                square_total=raw["square_total"],
            )
        return cls(
            key=tuple(payload["key"]),
            group_hash=payload["group_hash"],
            version=int(payload["version"]),
            row_count=int(payload["row_count"]),
            stats=stats,
            window_values=[normalize_float_cell(value) for value in payload["window_values"]],
            window_base=int(payload["window_base"]),
            last_touch=int(payload["last_touch"]),
            status=payload.get("status", "active"),
            failed_code=payload.get("failed_code"),
            dirty=False,
            loaded_version=int(payload["version"]),
        )

    def estimated_bytes(self) -> int:
        key_size = sum(len(str(part).encode("utf-8")) for part in self.key)
        return (
            4096
            + key_size
            + 512 * len(self.stats)
            + 512
            + 64 * len(self.window_values)
        )


def group_hash(key: Sequence[Any]) -> str:
    normalized: list[Any] = []
    for value in key:
        if isinstance(value, float):
            normalized.append(json_float_cell(value))
        else:
            normalized.append(value)
    return hashlib.sha256(canonical_dumps(normalized).encode("utf-8")).hexdigest()


def aggregate_result_payload(
    mode: str,
    op: str,
    stat: StatState,
    row_count: int,
    fixed_scale: int,
) -> dict[str, Any]:
    if op == "count":
        return {"representation": "integer", "value": row_count, "count": row_count}

    if op == "sum":
        if fixed_scale and mode == "fixed":
            return _fixed_fraction_payload(int(stat.total), fixed_scale)
        return _float_payload(float(stat.total))

    if op == "mean":
        if stat.finite_count == 0:
            return {"representation": "float", "value": None, "reason": "no_finite_values"}
        if mode == "fixed":
            return _fixed_fraction_payload(
                int(stat.total),
                fixed_scale * stat.finite_count,
            )
        value = float(stat.total) / stat.finite_count
        return _float_payload(value)

    if op == "variance":
        if stat.finite_count == 0:
            return {"representation": "float", "value": None, "reason": "no_finite_values"}
        if stat.finite_count == 1:
            if mode == "fixed":
                return _fixed_fraction_payload(0, 1)
            return _float_payload(0.0)
        if mode == "fixed":
            numerator = (
                stat.finite_count * int(stat.square_total)
                - int(stat.total) * int(stat.total)
            )
            denominator = (
                fixed_scale
                * fixed_scale
                * stat.finite_count
                * stat.finite_count
            )
            return _fixed_fraction_payload(numerator, denominator)
        mean = float(stat.total) / stat.finite_count
        value = float(stat.square_total) / stat.finite_count - mean * mean
        if value < 0.0 and abs(value) <= 1e-15:
            value = 0.0
        return _float_payload(value)

    raise EngineError(E_BAD_QUERY, f"unsupported aggregate operation {op!r}")


def _float_payload(value: float) -> dict[str, Any]:
    return {
        "representation": "float64",
        "value": value,
        "hex": float_hex(value),
    }


def _fixed_fraction_payload(numerator: int, denominator: int) -> dict[str, Any]:
    fraction = Fraction(numerator, denominator)
    approx = float(fraction)
    return {
        "representation": "fixed",
        "value": approx,
        "hex": float_hex(approx),
        "exact": f"{fraction.numerator}/{fraction.denominator}",
    }


def window_result_payload(
    mode: str,
    values: Sequence[float],
    window_start: int,
    fixed_scale: int,
    fixed_min: int,
    fixed_max: int,
) -> dict[str, Any]:
    finite: list[int] | list[float] = []
    nan_count = 0
    pos_inf_count = 0
    neg_inf_count = 0
    for value in values:
        if math.isnan(value):
            nan_count += 1
        elif math.isinf(value):
            if value > 0:
                pos_inf_count += 1
            else:
                neg_inf_count += 1
        elif mode == "fixed":
            from .numeric import convert_fixed

            converted = convert_fixed(value, fixed_scale, fixed_min, fixed_max)
            finite.append(converted.scaled)
        else:
            finite.append(float(value))

    finite_count = len(finite)
    total: int | float = sum(finite)
    square_total: int | float = sum(
        value * value for value in finite
    )
    if mode == "fixed":
        from .numeric import ensure_fixed_bounds

        ensure_fixed_bounds(
            int(total),
            fixed_min,
            fixed_max,
            field="window_sum",
            details={"window_start": window_start},
        )
        ensure_fixed_bounds(
            int(square_total),
            fixed_min,
            fixed_max,
            field="window_square_sum",
            details={"window_start": window_start},
        )
    stat = StatState(
        total_count=len(values),
        finite_count=finite_count,
        nan_count=nan_count,
        pos_inf_count=pos_inf_count,
        neg_inf_count=neg_inf_count,
        total=total,
        square_total=square_total,
    )
    payload: dict[str, Any] = {
        "window_start": window_start,
        "window_end": window_start + len(values),
        "count": len(values),
        "finite_count": finite_count,
        "nan_count": nan_count,
        "pos_inf_count": pos_inf_count,
        "neg_inf_count": neg_inf_count,
    }
    for op in ("count", "sum", "mean", "variance"):
        payload[op] = aggregate_result_payload(
            mode,
            op,
            stat,
            len(values),
            fixed_scale,
        )
    return payload


def parse_group_key(raw: Any) -> tuple[Any, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    values: list[Any] = []
    for item in raw:
        if isinstance(item, (str, bool, int, float)) or item is None:
            values.append(item)
        else:
            return None
    return tuple(values)


def load_json_file(path: str) -> Any:
    from .canonical import strict_json_loads

    with open(path, "r", encoding="utf-8") as handle:
        return strict_json_loads(handle.read())
