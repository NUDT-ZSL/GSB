from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from .errors import BusinessError, ErrorCode
from .models import digest_value


@dataclass(frozen=True)
class Policy:
    base_backoff_ticks: int = 1
    max_backoff_ticks: int = 8
    jitter_ticks: int = 1
    logical_time_budget_ticks: int = 8
    action_timeout_seconds: float = 1.0
    launch_cost_ticks: int | None = None

    def action_cost(self, cold_start_ticks: int) -> int:
        if self.launch_cost_ticks is not None:
            return self.launch_cost_ticks
        return cold_start_ticks

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())


def _int_field(raw: dict[str, Any], name: str, default: int, minimum: int) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BusinessError(
            ErrorCode.INPUT_SCHEMA,
            f"policy.{name} must be an integer >= {minimum}",
        )
    return value


def parse_policy(raw: dict[str, Any] | None) -> Policy:
    raw = dict(raw or {})
    base = _int_field(raw, "base_backoff_ticks", 1, 1)
    maximum = _int_field(raw, "max_backoff_ticks", 8, 1)
    if maximum < base:
        raise BusinessError(
            ErrorCode.INPUT_SCHEMA,
            "policy.max_backoff_ticks must be >= base_backoff_ticks",
        )
    jitter = _int_field(raw, "jitter_ticks", 1, 0)
    budget = _int_field(raw, "logical_time_budget_ticks", 8, 0)

    timeout_value = raw.get("action_timeout_seconds", 1.0)
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise BusinessError(
            ErrorCode.INPUT_SCHEMA,
            "policy.action_timeout_seconds must be a finite positive number",
        )
    timeout = float(timeout_value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise BusinessError(
            ErrorCode.INPUT_SCHEMA,
            "policy.action_timeout_seconds must be a finite positive number",
        )

    launch_cost: int | None = None
    if raw.get("launch_cost_ticks") is not None:
        launch_cost = _int_field(raw, "launch_cost_ticks", 0, 0)

    return Policy(
        base_backoff_ticks=base,
        max_backoff_ticks=maximum,
        jitter_ticks=jitter,
        logical_time_budget_ticks=budget,
        action_timeout_seconds=timeout,
        launch_cost_ticks=launch_cost,
    )

