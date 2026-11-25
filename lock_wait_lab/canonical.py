"""Canonical serialization, hashing and deterministic fixed-point conversion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ErrorCode, LabError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def stable_output(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)


def read_json(path: Path, *, required: bool = True) -> Any:
    if not path.exists():
        if required:
            raise LabError(
                ErrorCode.SAMPLE_MISSING,
                "required sample file is missing",
                path=str(path),
            )
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LabError(
            ErrorCode.JSON_INVALID,
            f"invalid JSON: {exc.msg}",
            field=f"line:{exc.lineno}",
            path=str(path),
        ) from exc
    except OSError as exc:
        raise LabError(
            ErrorCode.SAMPLE_MISSING,
            f"cannot read sample file: {exc.strerror}",
            path=str(path),
        ) from exc


def read_jsonl(path: Path) -> list[Any]:
    if not path.exists():
        raise LabError(
            ErrorCode.SAMPLE_MISSING,
            "required JSONL sample file is missing",
            path=str(path),
        )
    records: list[Any] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise LabError(
                        ErrorCode.JSON_INVALID,
                        f"invalid JSONL record: {exc.msg}",
                        field=f"line:{line_number}",
                        path=str(path),
                    ) from exc
    except OSError as exc:
        raise LabError(
            ErrorCode.SAMPLE_MISSING,
            f"cannot read JSONL sample: {exc.strerror}",
            path=str(path),
        ) from exc
    return records


@dataclass(frozen=True)
class FixedDuration:
    raw_ns: int
    scale: int
    rounding: str
    rounded_units: int
    rounded_ns: int
    rounded_ms: str
    direction: str
    discarded_remainder_ns: int
    residual_ns: int
    error_bound_ns: int
    quantum_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_ns": self.raw_ns,
            "scale": self.scale,
            "rounding": self.rounding,
            "rounded_units": self.rounded_units,
            "rounded_ns": self.rounded_ns,
            "rounded_ms": self.rounded_ms,
            "rounding_direction": self.direction,
            "discarded_remainder_ns": self.discarded_remainder_ns,
            "residual_ns": self.residual_ns,
            "error_bound_ns": self.error_bound_ns,
            "quantum_ns": self.quantum_ns,
        }


def validate_scale(scale: Any, field: str, path: str | None = None) -> int:
    if not isinstance(scale, int) or isinstance(scale, bool) or not 0 <= scale <= 6:
        raise LabError(
            ErrorCode.SCHEMA_INVALID,
            "fixed-point scale must be an integer in [0, 6]",
            field=field,
            path=path,
        )
    return scale


def format_fixed(units: int, scale: int) -> str:
    sign = "-" if units < 0 else ""
    absolute = abs(units)
    if scale == 0:
        return f"{sign}{absolute}"
    divisor = 10**scale
    whole, fraction = divmod(absolute, divisor)
    return f"{sign}{whole}.{fraction:0{scale}d}"


def convert_duration(raw_ns: int, scale: int, rounding: str = "HALF_UP") -> FixedDuration:
    if not isinstance(raw_ns, int) or isinstance(raw_ns, bool) or raw_ns < 0:
        raise LabError(
            ErrorCode.STATE_INVALID,
            "duration must be a non-negative integer nanosecond value",
            field="duration_ns",
        )
    if rounding != "HALF_UP":
        raise LabError(
            ErrorCode.SCHEMA_INVALID,
            "only HALF_UP fixed-point rounding is supported",
            field="rounding",
        )
    scale = validate_scale(scale, "scale")
    quantum_ns = 10 ** (6 - scale)
    rounded_units = (raw_ns + quantum_ns // 2) // quantum_ns
    rounded_ns = rounded_units * quantum_ns
    residual_ns = raw_ns - rounded_ns
    if residual_ns < 0:
        direction = "UP"
    elif residual_ns > 0:
        direction = "DOWN"
    else:
        direction = "EXACT"
    return FixedDuration(
        raw_ns=raw_ns,
        scale=scale,
        rounding=rounding,
        rounded_units=rounded_units,
        rounded_ns=rounded_ns,
        rounded_ms=format_fixed(rounded_units, scale),
        direction=direction,
        discarded_remainder_ns=abs(residual_ns),
        residual_ns=residual_ns,
        error_bound_ns=(quantum_ns + 1) // 2,
        quantum_ns=quantum_ns,
    )


def budget_trigger_ns(budget_ns: int, scale: int) -> int:
    """First raw duration whose HALF_UP result at ``scale`` reaches budget."""

    quantum_ns = 10 ** (6 - scale)
    return max(0, budget_ns - quantum_ns // 2)
