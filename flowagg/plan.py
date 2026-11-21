from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import FlowAggError, fail


DEFAULT_PROTOCOLS = (
    "TCP",
    "UDP",
    "ICMP",
    "ICMPV6",
    "SCTP",
    "QUIC",
    "HTTP",
    "HTTPS",
    "DNS",
)
DEFAULT_DIRECTIONS = ("ingress", "egress")
ALLOWED_DIMENSIONS = (
    "endpoint_pair",
    "src_endpoint",
    "dst_endpoint",
    "protocol",
    "direction",
)
MAX_I64 = (1 << 63) - 1


def _strict_json_load(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise fail(
            "E_NAN_OR_OVERFLOW",
            f"non-finite JSON number {value!r} is not permitted",
            policy="reject",
            value=value,
        )

    try:
        return json.loads(text, parse_constant=reject_constant)
    except FlowAggError:
        raise
    except Exception as exc:
        raise fail("E_PLAN_INVALID", f"cannot parse plan JSON: {exc}") from exc
@dataclass(frozen=True, slots=True)
class AggregationPlan:
    plan_id: str
    dimensions: tuple[str, ...]
    protocols: tuple[str, ...]
    directions: tuple[str, ...]
    memory_budget_mib: float
    memory_budget_bytes: int
    max_batch_rows: int
    spill_dir: Path
    fixed_scale: int
    quantiles: tuple[float, ...]
    max_clock_regression_ms: int
    endpoint_pair_order: str
    protocol_case: str
    direction_case: str
    key_separator: str
    read_buffer_fraction: float
    plan_hash: str
    source_path: Path
    raw: dict[str, Any]

    @property
    def read_buffer_bytes(self) -> int:
        return max(1, int(self.memory_budget_bytes * self.read_buffer_fraction))

    @property
    def active_budget_bytes(self) -> int:
        return self.memory_budget_bytes - self.read_buffer_bytes

    def group_key(self, src: str, dst: str, protocol: str, direction: str) -> tuple[Any, ...]:
        pair = f"{src}->{dst}"
        if self.endpoint_pair_order == "sorted":
            left, right = sorted((src, dst))
            pair = f"{left}<->{right}"
        values: list[Any] = []
        for dimension in self.dimensions:
            if dimension == "endpoint_pair":
                values.append(pair)
            elif dimension == "src_endpoint":
                values.append(src)
            elif dimension == "dst_endpoint":
                values.append(dst)
            elif dimension == "protocol":
                values.append(protocol.upper() if self.protocol_case == "upper" else protocol.lower())
            elif dimension == "direction":
                values.append(
                    direction.lower() if self.direction_case == "lower" else direction.upper()
                )
            else:  # guarded by plan validation
                raise AssertionError(dimension)
        return tuple(values)

    def canonical_group_key(self, values: tuple[Any, ...]) -> str:
        return json.dumps(
            list(values),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

    def dimension_map(self, values: tuple[Any, ...]) -> dict[str, Any]:
        return dict(zip(self.dimensions, values, strict=True))

    def display_group_key(self, values: tuple[Any, ...]) -> str:
        return self.key_separator.join(str(value) for value in values)

    def as_output(self) -> dict[str, Any]:
        return {
            "active_budget_bytes": self.active_budget_bytes,
            "dimensions": list(self.dimensions),
            "fixed_scale": self.fixed_scale,
            "key_normalization": {
                "direction_case": self.direction_case,
                "endpoint_pair_order": self.endpoint_pair_order,
                "key_separator": self.key_separator,
                "protocol_case": self.protocol_case,
            },
            "max_batch_rows": self.max_batch_rows,
            "memory_budget_bytes": self.memory_budget_bytes,
            "memory_budget_mib": self.memory_budget_mib,
            "plan_id": self.plan_id,
            "quantiles": list(self.quantiles),
            "read_buffer_bytes": self.read_buffer_bytes,
            "read_buffer_fraction": self.read_buffer_fraction,
            "spill_dir": self.spill_dir.name,
        }


def _require_object(value: Any, field_name: str = "plan") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise fail("E_PLAN_INVALID", f"{field_name} must be a JSON object")
    return value


def _positive_int(obj: dict[str, Any], name: str, default: int | None = None) -> int:
    value = obj.get(name, default)
    if type(value) is not int or value <= 0:
        raise fail("E_PLAN_INVALID", f"{name} must be a positive integer")
    return value


def _enum_list(
    obj: dict[str, Any],
    name: str,
    default: tuple[str, ...],
    normalizer: Any,
) -> tuple[str, ...]:
    raw = obj.get(name, list(default))
    if not isinstance(raw, list) or not raw:
        raise fail("E_PLAN_INVALID", f"{name} must be a non-empty array")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise fail("E_PLAN_INVALID", f"{name} entries must be non-empty strings")
        normalized = normalizer(item.strip())
        if normalized not in values:
            values.append(normalized)
    return tuple(values)


def _canonical_plan_hash(raw: dict[str, Any]) -> str:
    canonical = json.dumps(
        raw,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_plan(path: str | Path) -> AggregationPlan:
    source = Path(path)
    if not source.exists():
        raise fail("E_SAMPLE_MISSING", f"plan file does not exist: {source}", path=str(source))
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise fail("E_SAMPLE_MISSING", f"cannot read plan: {exc}", path=str(source)) from exc
    raw = _require_object(_strict_json_load(text))

    plan_id = raw.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise fail("E_PLAN_INVALID", "plan_id must be a non-empty string")

    dimensions_raw = raw.get("dimensions", ["endpoint_pair"])
    if not isinstance(dimensions_raw, list) or not dimensions_raw:
        raise fail("E_PLAN_INVALID", "dimensions must be a non-empty array")
    dimensions: list[str] = []
    for dimension in dimensions_raw:
        if dimension not in ALLOWED_DIMENSIONS:
            raise fail(
                "E_PLAN_INVALID",
                f"unsupported dimension {dimension!r}",
                allowed=list(ALLOWED_DIMENSIONS),
            )
        if dimension in dimensions:
            raise fail("E_PLAN_INVALID", f"duplicate dimension {dimension!r}")
        dimensions.append(dimension)

    budget_mib = raw.get("memory_budget_mib")
    if isinstance(budget_mib, bool) or not isinstance(budget_mib, (int, float)):
        raise fail("E_PLAN_INVALID", "memory_budget_mib must be a number")
    budget_mib = float(budget_mib)
    if not math.isfinite(budget_mib) or budget_mib <= 0:
        raise fail("E_PLAN_INVALID", "memory_budget_mib must be finite and positive")
    budget_bytes = int(budget_mib * 1024 * 1024)
    if budget_bytes < 256:
        raise fail("E_MEMORY_BUDGET", "memory budget must be at least 256 bytes")

    max_batch_rows = _positive_int(raw, "max_batch_rows")
    fixed_scale = _positive_int(raw, "fixed_scale")

    spill_raw = raw.get("spill_dir")
    if not isinstance(spill_raw, str) or not spill_raw.strip():
        raise fail("E_PLAN_INVALID", "spill_dir must be a non-empty string")
    spill_dir = Path(spill_raw)
    if not spill_dir.is_absolute():
        spill_dir = (source.parent / spill_dir).resolve()
    if spill_dir.exists() and not spill_dir.is_dir():
        raise fail(
            "E_PLAN_INVALID",
            f"spill_dir is not a directory: {spill_dir}",
        )

    quantiles_raw = raw.get("quantiles", [0.5, 0.9, 0.99])
    if not isinstance(quantiles_raw, list) or not quantiles_raw:
        raise fail("E_PLAN_INVALID", "quantiles must be a non-empty array")
    quantiles: list[float] = []
    for q in quantiles_raw:
        if isinstance(q, bool) or not isinstance(q, (int, float)):
            raise fail("E_PLAN_INVALID", "quantile values must be numbers")
        qf = float(q)
        if not math.isfinite(qf) or qf < 0.0 or qf > 1.0:
            raise fail("E_PLAN_INVALID", "quantiles must be finite and in [0, 1]")
        quantiles.append(qf)
    if len(set(quantiles)) != len(quantiles):
        raise fail("E_PLAN_INVALID", "quantile values must be unique")

    regression = raw.get("max_clock_regression_ms", 1000)
    if type(regression) is not int or regression < 0:
        raise fail("E_PLAN_INVALID", "max_clock_regression_ms must be a non-negative integer")

    normalization = raw.get("key_normalization", {})
    if not isinstance(normalization, dict):
        raise fail("E_PLAN_INVALID", "key_normalization must be an object")
    pair_order = normalization.get("endpoint_pair_order", "ordered")
    if pair_order not in ("ordered", "sorted"):
        raise fail("E_PLAN_INVALID", "endpoint_pair_order must be 'ordered' or 'sorted'")
    protocol_case = normalization.get("protocol_case", "upper")
    if protocol_case not in ("upper", "lower"):
        raise fail("E_PLAN_INVALID", "protocol_case must be 'upper' or 'lower'")
    direction_case = normalization.get("direction_case", "lower")
    if direction_case not in ("lower", "upper"):
        raise fail("E_PLAN_INVALID", "direction_case must be 'lower' or 'upper'")
    key_separator = normalization.get("key_separator", "|")
    if not isinstance(key_separator, str) or not key_separator:
        raise fail("E_PLAN_INVALID", "key_separator must be a non-empty string")

    read_fraction = raw.get("read_buffer_fraction", 0.25)
    if isinstance(read_fraction, bool) or not isinstance(read_fraction, (int, float)):
        raise fail("E_PLAN_INVALID", "read_buffer_fraction must be a number")
    read_fraction = float(read_fraction)
    if not math.isfinite(read_fraction) or not 0.05 <= read_fraction <= 0.75:
        raise fail(
            "E_PLAN_INVALID",
            "read_buffer_fraction must be finite and in [0.05, 0.75]",
        )

    protocols = _enum_list(
        raw,
        "protocols",
        DEFAULT_PROTOCOLS,
        lambda item: item.upper() if protocol_case == "upper" else item.lower(),
    )
    directions = _enum_list(
        raw,
        "directions",
        DEFAULT_DIRECTIONS,
        lambda item: item.lower() if direction_case == "lower" else item.upper(),
    )

    return AggregationPlan(
        plan_id=plan_id.strip(),
        dimensions=tuple(dimensions),
        protocols=protocols,
        directions=directions,
        memory_budget_mib=budget_mib,
        memory_budget_bytes=budget_bytes,
        max_batch_rows=max_batch_rows,
        spill_dir=spill_dir,
        fixed_scale=fixed_scale,
        quantiles=tuple(quantiles),
        max_clock_regression_ms=regression,
        endpoint_pair_order=pair_order,
        protocol_case=protocol_case,
        direction_case=direction_case,
        key_separator=key_separator,
        read_buffer_fraction=read_fraction,
        plan_hash=_canonical_plan_hash(raw),
        source_path=source.resolve(),
        raw=raw,
    )


def fixed_quantile(raw_value: Decimal, scale: int) -> int:
    try:
        scaled = (raw_value * Decimal(scale)).to_integral_value(rounding="ROUND_HALF_UP")
    except InvalidOperation as exc:
        raise fail("E_NAN_OR_OVERFLOW", f"fixed-point conversion failed: {exc}") from exc
    result = int(scaled)
    if result < -MAX_I64 or result > MAX_I64:
        raise fail("E_NAN_OR_OVERFLOW", "fixed-point quantile exceeds signed 64-bit range")
    return result

