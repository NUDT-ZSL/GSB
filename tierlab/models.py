from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .errors import BusinessError, ErrorCode

HOT = "hot"
COLD = "cold"
LAYERS = (HOT, COLD)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=json_default,
    )


def stable_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SamplingConfig:
    ratio: Decimal
    confidence_z: Decimal
    min_sample_per_stratum: int
    random_seed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ratio": decimal_text(self.ratio),
            "confidence_z": decimal_text(self.confidence_z),
            "min_sample_per_stratum": self.min_sample_per_stratum,
            "random_seed": self.random_seed,
        }


@dataclass(frozen=True)
class AdaptiveConfig:
    exact_max_objects: int
    exact_max_candidates: int
    max_estimated_error_ratio: Decimal
    exact_scan_unit_cost: Decimal
    exact_candidate_unit_cost: Decimal
    sampled_index_unit_cost: Decimal
    sampled_object_unit_cost: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "exact_max_objects": self.exact_max_objects,
            "exact_max_candidates": self.exact_max_candidates,
            "max_estimated_error_ratio": decimal_text(
                self.max_estimated_error_ratio
            ),
            "exact_scan_unit_cost": decimal_text(self.exact_scan_unit_cost),
            "exact_candidate_unit_cost": decimal_text(
                self.exact_candidate_unit_cost
            ),
            "sampled_index_unit_cost": decimal_text(
                self.sampled_index_unit_cost
            ),
            "sampled_object_unit_cost": decimal_text(
                self.sampled_object_unit_cost
            ),
        }


@dataclass(frozen=True)
class BackoffConfig:
    base_delay_clocks: int
    max_delay_clocks: int
    multiplier: int
    max_consecutive_failures: int

    def delay_for(self, consecutive_failures: int) -> int:
        exponent = max(0, consecutive_failures - 1)
        if self.multiplier == 1:
            return min(self.base_delay_clocks, self.max_delay_clocks)
        delay = self.base_delay_clocks
        steps = exponent
        while steps > 0 and delay < self.max_delay_clocks:
            delay *= self.multiplier
            steps -= 1
        return min(delay, self.max_delay_clocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_delay_clocks": self.base_delay_clocks,
            "max_delay_clocks": self.max_delay_clocks,
            "multiplier": self.multiplier,
            "max_consecutive_failures": self.max_consecutive_failures,
        }


@dataclass(frozen=True)
class TierConfig:
    hot_capacity_bytes: int
    cold_capacity_bytes: int
    round_byte_budget: int
    migration_concurrency: int
    hot_threshold: Decimal
    clock_tolerance: int
    backoff: BackoffConfig
    sampling: SamplingConfig
    adaptive: AdaptiveConfig
    mode: str = "auto"

    def capacity(self, layer: str) -> int:
        return self.hot_capacity_bytes if layer == HOT else self.cold_capacity_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "hot_capacity_bytes": self.hot_capacity_bytes,
            "cold_capacity_bytes": self.cold_capacity_bytes,
            "round_byte_budget": self.round_byte_budget,
            "migration_concurrency": self.migration_concurrency,
            "hot_threshold": decimal_text(self.hot_threshold),
            "clock_tolerance": self.clock_tolerance,
            "backoff": self.backoff.to_dict(),
            "sampling": self.sampling.to_dict(),
            "adaptive": self.adaptive.to_dict(),
            "mode": self.mode,
        }


@dataclass(frozen=True)
class StorageObject:
    bucket: str
    key: str
    size_bytes: int
    last_access_clock: int
    access_count: int
    heat_weight: Decimal
    declared_layer: str
    migratable: bool
    current_layer: str
    migration_state: str = "pending"
    failure_count: int = 0
    backoff_until_clock: int = 0
    version: int = 0
    last_round: int = 0
    last_outcome: str | None = None


@dataclass(frozen=True)
class Manifest:
    bucket: str
    objects: tuple[StorageObject, ...]


@dataclass
class Evaluation:
    obj: StorageObject
    score: Decimal
    target_layer: str
    eligible: bool
    exclusion_reason: str | None
    direction: str | None


@dataclass
class Candidate:
    obj: StorageObject
    score: Decimal
    source_layer: str
    target_layer: str
    direction: str


@dataclass
class Observation:
    mode: str
    truth_source: str
    population_count: int
    observed_count: int
    candidate_count: int
    candidate_bytes: int
    candidates: list[Candidate]
    observations: list[dict[str, Any]] = field(default_factory=list)
    sampling_summary: dict[str, Any] | None = None
    excluded: dict[str, int] = field(default_factory=dict)


@dataclass
class ModeDecision:
    requested_mode: str
    selected_mode: str
    trigger_conditions: list[str]
    rejected_mode: str
    rejected_reason: str
    cost_estimate: dict[str, str]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "selected_mode": self.selected_mode,
            "trigger_conditions": self.trigger_conditions,
            "rejected_mode": self.rejected_mode,
            "rejected_reason": self.rejected_reason,
            "cost_estimate": self.cost_estimate,
            "metrics": self.metrics,
        }


@dataclass(frozen=True)
class FailureSpec:
    fail_keys: tuple[str, ...] = ()
    fail_count: int = 0
    fail_every: int = 0

    def should_fail(self, key: str, start_order: int) -> bool:
        if key in self.fail_keys:
            return True
        if self.fail_count > 0 and start_order <= self.fail_count:
            return True
        if self.fail_every > 0 and start_order % self.fail_every == 0:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "fail_keys": list(self.fail_keys),
            "fail_count": self.fail_count,
            "fail_every": self.fail_every,
        }


def require_object(obj: Any, where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise BusinessError(
            ErrorCode.INVALID_TYPE,
            f"{where} must be a JSON object",
        )
    return obj


def first_present(data: dict[str, Any], names: tuple[str, ...], where: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    raise BusinessError(
        ErrorCode.MISSING_FIELD,
        f"{where} is missing required field {names[0]}",
        {"accepted_names": list(names)},
    )


def require_bool(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise BusinessError(
            ErrorCode.INVALID_TYPE,
            f"{where} must be a boolean",
            {"value_type": type(value).__name__},
        )
    return value


def require_int(value: Any, where: str) -> int:
    if type(value) is not int:
        raise BusinessError(
            ErrorCode.INVALID_TYPE,
            f"{where} must be an integer",
            {"value_type": type(value).__name__},
        )
    return value


def require_nonnegative_int(value: Any, where: str, code: str) -> int:
    number = require_int(value, where)
    if number < 0:
        raise BusinessError(code, f"{where} must be >= 0", {"value": number})
    return number


def require_positive_int(value: Any, where: str, code: str) -> int:
    number = require_int(value, where)
    if number <= 0:
        raise BusinessError(code, f"{where} must be > 0", {"value": number})
    return number


def require_decimal(value: Any, where: str, code: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
        raise BusinessError(
            ErrorCode.INVALID_TYPE,
            f"{where} must be a finite number",
            {"value_type": type(value).__name__},
        )
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - normalized to a business error.
        raise BusinessError(
            code,
            f"{where} must be a finite decimal number",
        ) from exc
    if not result.is_finite():
        raise BusinessError(code, f"{where} must be finite")
    return result
