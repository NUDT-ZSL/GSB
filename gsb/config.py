"""引擎裁决配置（标度、容忍度、默认速率等），来源于 v1 thresholds。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .errors import Code, GsbError
from .fixedpoint import Scale
from .model import Bundle, is_int


@dataclass(frozen=True)
class EngineConfig:
    now_ms: int
    alert_lag_ms: int
    tolerance_ms: Decimal
    delay_scale: Scale
    margin_scale: Scale
    deadline_scale: Scale
    fine_factor: int
    rate_bytes_per_ms: Decimal
    safety_factor: Decimal
    slack_ms: Decimal

    @classmethod
    def from_bundle(cls, bundle: Bundle, now_override: Optional[int] = None) -> "EngineConfig":
        cfg = bundle.config
        now_ms = now_override if now_override is not None else cfg.get("judge_now_ms")
        if not is_int(now_ms):
            raise GsbError(Code.BAD_CONFIG, "judge_now_ms missing or non-integer",
                           {"path": "thresholds.judge_now_ms"})
        alert = cfg.get("alert_lag_ms", 8000)
        if not is_int(alert) or alert < 0:
            raise GsbError(Code.BAD_CONFIG, "alert_lag_ms must be non-negative integer")
        try:
            tol = Decimal(str(cfg.get("batch_tolerance_ms", "5")))
            delay_scale = Scale.make(cfg.get("delay_unit_ms", "100"), "ROUND_FLOOR", "delay-100ms")
            margin_scale = Scale.make(cfg.get("margin_unit_ms", "10"), "ROUND_CEILING", "margin-10ms")
            deadline_scale = Scale.make(cfg.get("deadline_unit_ms", "1000"), "ROUND_FLOOR", "deadline-1s")
            rate = Decimal(str(cfg.get("rate_bytes_per_ms", "80")))
            safety = Decimal(str(cfg.get("safety_factor", "1.25")))
            slack = Decimal(str(cfg.get("slack_ms", "2")))
            fine_factor = int(cfg.get("fine_scale_factor", 1000))
        except Exception as exc:  # Decimal/Scale 构造错误统一为配置错误
            raise GsbError(Code.BAD_CONFIG, f"invalid fixed-point config: {exc}")
        if tol <= 0 or rate <= 0 or safety <= 0 or fine_factor < 2:
            raise GsbError(Code.BAD_CONFIG, "tolerance/rate/safety/fine_factor out of range")
        return cls(
            now_ms=int(now_ms),
            alert_lag_ms=int(alert),
            tolerance_ms=tol,
            delay_scale=delay_scale,
            margin_scale=margin_scale,
            deadline_scale=deadline_scale,
            fine_factor=fine_factor,
            rate_bytes_per_ms=rate,
            safety_factor=safety,
            slack_ms=slack,
        )

    def watermark(self) -> dict:
        return {"injected_now_ms": self.now_ms, "alert_lag_ms": self.alert_lag_ms,
                "batch_tolerance_ms": str(self.tolerance_ms)}
