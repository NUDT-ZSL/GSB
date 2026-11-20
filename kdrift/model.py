from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

from .errors import EngineError, Issue
from .jsonutil import is_int, sha256_hex

KINDS = ("ConfigMap", "Deployment", "Service")
KIND_ORDER = {kind: index for index, kind in enumerate(KINDS)}
PHASES = ("pending", "converging", "converged", "failed", "abandoned")


@dataclass(frozen=True)
class Resource:
    kind: str
    namespace: str
    name: str
    body: dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.namespace}/{self.name}"

    @property
    def identity(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "namespace": self.namespace,
            "name": self.name,
        }

    @property
    def sort_key(self) -> tuple[Any, ...]:
        return (self.namespace, KIND_ORDER[self.kind], self.name)

    @property
    def digest(self) -> str:
        return sha256_hex(self.body)

    def copy(self) -> "Resource":
        return Resource(self.kind, self.namespace, self.name, copy.deepcopy(self.body))


@dataclass(frozen=True)
class EngineConfig:
    max_concurrency: int
    operation_timeout_ms: int
    exact_object_threshold: int
    exact_field_threshold: int
    sampling_ratio: float
    tolerance: float
    logical_step_ms: int
    initial_logical_ms: int
    retry_limit: int
    retry_backoff_ms: int
    equivalence_audit: bool
    strata_count: int
    simulated_failures: tuple[str, ...] = ()
    operation_durations_ms: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_concurrency": self.max_concurrency,
            "operation_timeout_ms": self.operation_timeout_ms,
            "exact_object_threshold": self.exact_object_threshold,
            "exact_field_threshold": self.exact_field_threshold,
            "sampling_ratio": self.sampling_ratio,
            "tolerance": self.tolerance,
            "logical_step_ms": self.logical_step_ms,
            "initial_logical_ms": self.initial_logical_ms,
            "retry_limit": self.retry_limit,
            "retry_backoff_ms": self.retry_backoff_ms,
            "equivalence_audit": self.equivalence_audit,
            "strata_count": self.strata_count,
            "simulated_failures": list(self.simulated_failures),
            "operation_durations_ms": dict(
                sorted(self.operation_durations_ms.items())
            ),
        }

    def duration_for(self, resource_key: str) -> int:
        return self.operation_durations_ms.get(resource_key, self.logical_step_ms)

    def should_fail(self, resource_key: str) -> bool:
        return resource_key in self.simulated_failures

    @classmethod
    def from_dict(cls, raw: Any) -> "EngineConfig":
        if not isinstance(raw, dict):
            raise EngineError(
                "E_BAD_CONFIG", "config root must be a JSON object"
            )
        try:
            object_threshold = raw.get(
                "exact_object_threshold",
                raw.get("small_scale_threshold", raw.get("exact_threshold", 8)),
            )
            field_threshold = raw.get(
                "exact_field_threshold",
                raw.get("small_scale_threshold", raw.get("exact_threshold", 12)),
            )
            config = cls(
                max_concurrency=raw.get("max_concurrency", 3),
                operation_timeout_ms=raw.get("operation_timeout_ms", 100),
                exact_object_threshold=object_threshold,
                exact_field_threshold=field_threshold,
                sampling_ratio=raw.get("sampling_ratio", 0.5),
                tolerance=raw.get("tolerance", 0),
                logical_step_ms=raw.get("logical_step_ms", 25),
                initial_logical_ms=raw.get("initial_logical_ms", 0),
                retry_limit=raw.get("retry_limit", 1),
                retry_backoff_ms=raw.get("retry_backoff_ms", 10),
                equivalence_audit=raw.get("equivalence_audit", True),
                strata_count=raw.get("strata_count", 12),
                simulated_failures=tuple(raw.get("simulated_failures", [])),
                operation_durations_ms={
                    str(key): value
                    for key, value in raw.get("operation_durations_ms", {}).items()
                },
            )
        except (TypeError, ValueError) as exc:
            raise EngineError(
                "E_BAD_CONFIG", f"invalid convergence config: {exc}"
            ) from exc
        config.validate()
        return config

    def validate(self) -> None:
        checks = {
            "max_concurrency": (is_int, 1, None),
            "operation_timeout_ms": (is_int, 1, None),
            "exact_object_threshold": (is_int, 1, None),
            "exact_field_threshold": (is_int, 1, None),
            "logical_step_ms": (is_int, 1, None),
            "initial_logical_ms": (is_int, 0, None),
            "retry_limit": (is_int, 0, None),
            "retry_backoff_ms": (is_int, 0, None),
            "strata_count": (is_int, 1, 64),
        }
        for name, (predicate, minimum, maximum) in checks.items():
            value = getattr(self, name)
            if not predicate(value):
                raise EngineError(
                    "E_BAD_CONFIG", f"{name} must be an integer"
                )
            if minimum is not None and value < minimum:
                raise EngineError(
                    "E_BAD_CONFIG", f"{name} must be >= {minimum}"
                )
            if maximum is not None and value > maximum:
                raise EngineError(
                    "E_BAD_CONFIG", f"{name} must be <= {maximum}"
                )
        for name in ("sampling_ratio",):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0 < value <= 1
            ):
                raise EngineError(
                    "E_BAD_CONFIG", f"{name} must be in (0, 1]"
                )
        if (
            not isinstance(self.tolerance, (int, float))
            or isinstance(self.tolerance, bool)
            or not math.isfinite(self.tolerance)
            or self.tolerance < 0
        ):
            raise EngineError(
                "E_BAD_CONFIG", "tolerance must be a finite number >= 0"
            )
        if not isinstance(self.equivalence_audit, bool):
            raise EngineError("E_BAD_CONFIG", "equivalence_audit must be boolean")
        if not all(isinstance(item, str) and item for item in self.simulated_failures):
            raise EngineError(
                "E_BAD_CONFIG", "simulated_failures must contain resource keys"
            )
        for key, duration in self.operation_durations_ms.items():
            if not key or not is_int(duration) or duration < 1:
                raise EngineError(
                    "E_BAD_CONFIG",
                    "operation_durations_ms must map resource keys to positive integers",
                )


@dataclass
class Bundle:
    desired: dict[str, Resource]
    actual: dict[str, Resource]
    config: EngineConfig
    issues: list[Issue]

    def sorted_desired(self) -> list[Resource]:
        return [self.desired[key] for key in sorted(self.desired, key=self._resource_key)]

    def sorted_actual(self) -> list[Resource]:
        return [self.actual[key] for key in sorted(self.actual, key=self._resource_key)]

    @staticmethod
    def _resource_key(key: str) -> tuple[Any, ...]:
        kind, namespace, name = key.split("/", 2)
        return (namespace, KIND_ORDER.get(kind, 99), name, kind)

