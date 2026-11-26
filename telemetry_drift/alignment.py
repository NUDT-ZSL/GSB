"""Deterministic dual-sided variable alignment on a common logical clock."""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .canonical import (
    atomic_write_json,
    canonical_json,
    read_json,
    sha256_value,
    stable_float,
)
from .config import PipelineConfig
from .errors import DriftError, E_ALIGNMENT_INVALID


@dataclass(frozen=True)
class AlignmentResult:
    times_ms: tuple[int, ...]
    values: dict[int, tuple[float, ...]]
    sources: dict[int, str]
    roles: dict[int, str]
    fill_flags: dict[int, tuple[str, ...]]
    period_ms: int
    digest: str

    def matrix(self, variable_ids: Iterable[int]) -> np.ndarray:
        columns = [self.values[variable_id] for variable_id in variable_ids]
        return np.asarray(columns, dtype=np.float64).T

    def variable(self, variable_id: int) -> np.ndarray:
        return np.asarray(self.values[variable_id], dtype=np.float64)

    def to_dict(self) -> dict[str, Any]:
        groups: dict[str, list[int]] = {"input": [], "output": [], "aux": []}
        for variable_id in sorted(self.values):
            groups.setdefault(self.roles[variable_id], []).append(variable_id)
        payload = {
            "schema": "aligned-series-v1",
            "period_ms": self.period_ms,
            "times_ms": list(self.times_ms),
            "variables": {
                str(variable_id): {
                    "source": self.sources[variable_id],
                    "values": list(self.values[variable_id]),
                    "fill_flags": list(self.fill_flags[variable_id]),
                }
                for variable_id in sorted(self.values)
            },
            "groups": groups,
            "digest": self.digest,
        }
        return payload

    def serialized(self) -> str:
        return canonical_json(self.to_dict(), newline=True)


def load_observations(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    records: list[dict[str, Any]] = []
    try:
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    payload = __import__("json").loads(line)
                except Exception as exc:
                    raise DriftError(
                        E_ALIGNMENT_INVALID,
                        f"invalid observation JSON at line {line_number}",
                    ) from exc
                records.append(_validate_record(payload, line_number))
    except OSError as exc:
        raise DriftError(E_ALIGNMENT_INVALID, f"cannot read observations {source}") from exc
    if not records:
        raise DriftError(E_ALIGNMENT_INVALID, "observation stream is empty")
    return records


def _validate_record(payload: Any, line_number: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DriftError(E_ALIGNMENT_INVALID, "observation must be an object")
    try:
        variable = int(payload["variable"])
        source = str(payload["source"])
        logical_ms = int(payload["logical_ms"])
        values = payload["values"]
    except (KeyError, TypeError, ValueError) as exc:
        raise DriftError(
            E_ALIGNMENT_INVALID,
            f"observation line {line_number} is missing required fields",
        ) from exc
    if variable < 0 or logical_ms < 0 or not source:
        raise DriftError(
            E_ALIGNMENT_INVALID,
            f"observation line {line_number} has invalid metadata",
        )
    if not isinstance(values, list) or not values:
        raise DriftError(
            E_ALIGNMENT_INVALID,
            f"observation line {line_number} must carry a value vector",
        )
    normalized: list[float | None] = []
    for item in values:
        if item is None:
            normalized.append(None)
            continue
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise DriftError(
                E_ALIGNMENT_INVALID,
                f"observation line {line_number} has a non-numeric value",
            ) from exc
        if not math.isfinite(number):
            raise DriftError(
                E_ALIGNMENT_INVALID,
                f"observation line {line_number} has a non-finite value",
            )
        normalized.append(number)
    return {
        "variable": variable,
        "source": source,
        "logical_ms": logical_ms,
        "values": normalized,
        "line_number": line_number,
    }


def infer_common_period_ms(
    records: list[dict[str, Any]], fallback_ms: int = 1
) -> int:
    """Infer the finest common grid implied by observed logical timestamps."""
    timestamps_by_variable: dict[int, set[int]] = {}
    for record in records:
        timestamps_by_variable.setdefault(record["variable"], set()).add(
            record["logical_ms"]
        )
    period = 0
    for timestamps in timestamps_by_variable.values():
        ordered = sorted(timestamps)
        for left, right in zip(ordered, ordered[1:]):
            delta = right - left
            if delta > 0:
                period = delta if period == 0 else math.gcd(period, delta)
    return max(1, period or fallback_ms)


def align_observations(
    records: list[dict[str, Any]],
    config: PipelineConfig,
    *,
    period_ms: int | None = None,
) -> AlignmentResult:
    declared = {variable.id: variable for variable in config.variables}
    observed_ids = {record["variable"] for record in records}
    unknown = observed_ids - set(declared)
    if unknown:
        raise DriftError(
            E_ALIGNMENT_INVALID,
            "observations contain undeclared variables",
            variables=sorted(unknown),
        )
    expected = set(declared)
    if observed_ids != expected:
        raise DriftError(
            E_ALIGNMENT_INVALID,
            "observation stream does not cover every declared variable",
            missing=sorted(expected - observed_ids),
        )

    timeline = (
        int(period_ms)
        if period_ms is not None
        else (
            config.alignment.period_ms
            if config.alignment.period_ms > 0
            else infer_common_period_ms(records, fallback_ms=100)
        )
    )
    if timeline <= 0:
        raise DriftError(E_ALIGNMENT_INVALID, "common period must be positive")

    records_by_variable: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        records_by_variable.setdefault(record["variable"], []).append(record)
    for variable_id in records_by_variable:
        records_by_variable[variable_id].sort(
            key=lambda item: (item["logical_ms"], item["line_number"])
        )

    start_ms = min(record["logical_ms"] for record in records)
    end_ms = max(record["logical_ms"] for record in records)
    times = list(range(start_ms, end_ms + 1, timeline))
    if times[-1] != end_ms:
        times.append(end_ms)

    aligned_values: dict[int, list[float]] = {}
    fill_flags: dict[int, list[str]] = {}
    sources: dict[int, str] = {}
    for variable_id in sorted(records_by_variable):
        variable_records = records_by_variable[variable_id]
        sources[variable_id] = variable_records[0]["source"]
        timestamps = [item["logical_ms"] for item in variable_records]
        raw_values: list[float | None] = []
        flags: list[str] = []
        for tick in times:
            position = bisect.bisect_right(timestamps, tick) - 1
            if position < 0:
                raw_values.append(None)
                flags.append("missing_no_history")
                continue
            record = variable_records[position]
            value = record["values"][0]
            age = tick - record["logical_ms"]
            if value is None:
                raw_values.append(None)
                flags.append("missing_null")
            elif age > config.alignment.stale_after_ms:
                raw_values.append(None)
                flags.append("missing_stale")
            elif age == 0:
                raw_values.append(float(value))
                flags.append("observed")
            else:
                raw_values.append(float(value))
                flags.append("sample_hold")
        filled, fill_method = _fill_missing(
            raw_values, config.alignment.missing_strategy
        )
        aligned_values[variable_id] = [
            stable_float(value) for value in filled
        ]
        final_flags: list[str] = []
        for flag, method in zip(flags, fill_method):
            final_flags.append(method if method else flag)
        fill_flags[variable_id] = final_flags

    provisional = {
        "schema": "aligned-series-v1",
        "period_ms": timeline,
        "times_ms": times,
        "variables": {
            str(variable_id): {
                "source": sources[variable_id],
                "values": aligned_values[variable_id],
                "fill_flags": fill_flags[variable_id],
            }
            for variable_id in sorted(aligned_values)
        },
        "groups": {
            role: sorted(
                variable_id
                for variable_id in aligned_values
                if declared[variable_id].role == role
            )
            for role in ("input", "output", "aux")
        },
    }
    digest = sha256_value(provisional)
    return AlignmentResult(
        times_ms=tuple(times),
        values={
            variable_id: tuple(values)
            for variable_id, values in aligned_values.items()
        },
        sources=sources,
        roles={variable_id: declared[variable_id].role for variable_id in sources},
        fill_flags={
            variable_id: tuple(flags) for variable_id, flags in fill_flags.items()
        },
        period_ms=timeline,
        digest=digest,
    )


def _fill_missing(
    values: list[float | None], strategy: str
) -> tuple[list[float], list[str | None]]:
    valid_indices = [index for index, value in enumerate(values) if value is not None]
    if not valid_indices:
        raise DriftError(
            E_ALIGNMENT_INVALID, "variable has no usable values after alignment"
        )
    result: list[float] = []
    methods: list[str | None] = []
    if strategy == "forward_fill":
        last_value = float(values[valid_indices[0]])
        for index, value in enumerate(values):
            if value is None:
                result.append(last_value)
                methods.append(
                    "edge_forward_fill" if index < valid_indices[0] else "forward_fill"
                )
            else:
                last_value = float(value)
                result.append(last_value)
                methods.append(None)
        return result, methods
    if strategy != "minimal_change":
        raise DriftError(E_ALIGNMENT_INVALID, f"unknown fill strategy {strategy}")

    cursor = 0
    for index, value in enumerate(values):
        if value is not None:
            result.append(float(value))
            methods.append(None)
            cursor = index
            continue
        previous = cursor if values[cursor] is not None else None
        next_position = bisect.bisect_right(valid_indices, index)
        following = (
            valid_indices[next_position] if next_position < len(valid_indices) else None
        )
        if previous is None:
            assert following is not None
            result.append(float(values[following]))
            methods.append("edge_minimal_change")
        elif following is None:
            result.append(float(values[previous]))
            methods.append("edge_minimal_change")
        else:
            fraction = (index - previous) / (following - previous)
            left = float(values[previous])
            right = float(values[following])
            result.append(left + fraction * (right - left))
            methods.append("minimal_change")
    return result, methods


def write_alignment(path: str | Path, alignment: AlignmentResult) -> None:
    atomic_write_json(path, alignment.to_dict())


def read_alignment(path: str | Path) -> dict[str, Any]:
    return read_json(path)

