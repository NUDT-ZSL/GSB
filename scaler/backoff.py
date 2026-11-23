from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .models import ScalingConfig


@dataclass(frozen=True)
class BackoffDecision:
    retry_at_ms: int
    delay_ms: int
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "retry_at_ms": self.retry_at_ms,
            "delay_ms": self.delay_ms,
            "evidence": self.evidence,
        }


def deterministic_backoff(
    *,
    service: str,
    replica_id: str,
    failure_kind: str,
    streak: int,
    now_ms: int,
    config: ScalingConfig,
) -> BackoffDecision:
    exponent = max(0, streak - 1)
    capped_exponent = min(exponent, 62)
    threshold_reached = streak >= config.failure_threshold
    exponential_delay = min(
        config.backoff_max_ms, config.backoff_base_ms * (2**capped_exponent)
    )
    raw_delay = config.backoff_max_ms if threshold_reached else exponential_delay
    seed_text = f"{service}|{replica_id}|{failure_kind}|{streak}|{raw_delay}"
    seed = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    span = (raw_delay * config.jitter_percent) // 100
    if config.jitter_mode == "deterministic" and span > 0:
        bucket = int(seed[:16], 16) % (2 * span + 1)
        offset = bucket - span
    else:
        bucket = 0
        offset = 0
    delay = max(0, min(config.backoff_max_ms, raw_delay + offset))
    retry_at = now_ms + delay
    evidence = {
        "failure_kind": failure_kind,
        "failure_streak": streak,
        "now_ms": now_ms,
        "base_ms": config.backoff_base_ms,
        "max_ms": config.backoff_max_ms,
        "failure_threshold": config.failure_threshold,
        "failure_threshold_reached": threshold_reached,
        "exponential_delay_before_threshold_ms": exponential_delay,
        "raw_exponential_delay_ms": raw_delay,
        "jitter_mode": config.jitter_mode,
        "jitter_percent": config.jitter_percent,
        "jitter_span_ms": span,
        "jitter_offset_ms": offset,
        "jitter_bucket": bucket,
        "seed": seed,
        "delay_ms": delay,
        "retry_at_ms": retry_at,
        "clock_source": "injected_logical_clock",
    }
    return BackoffDecision(retry_at_ms=retry_at, delay_ms=delay, evidence=evidence)
