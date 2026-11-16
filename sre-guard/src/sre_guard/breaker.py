"""熔断器：滑动分钟窗口 + 冷却 + 半开试探状态机（全程整数）。

统计口径
========
* 事件按 ``timestamp_ms // 60000`` 归入整分钟桶，窗口滑动时**整桶移除**，
  不做逐条递减。
* 失败计数 = failure 计数 + timeout 计数，二者分别保存、可明确区分。
* 失败率与阈值比较采用整数交叉相乘（无浮点、比较零误差）；
  同时输出定点失败率及其 1 ulp 量化误差上界。
状态机
======
CLOSED ->（达到最小请求数且失败率 >= 阈值）-> OPEN
OPEN   ->（冷却结束后允许最多 half_open_probe 个试探）-> HALF_OPEN
HALF_OPEN ->（试探期出现任一失败/超时）-> OPEN
HALF_OPEN ->（试探成功数达到 half_open_probe）-> CLOSED（窗口清空）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fixedpoint import Grid, compare_percent, format_fixed, quantize
from .models import BreakerPolicy, RESULT_FAILURE, RESULT_SUCCESS, RESULT_TIMEOUT

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"
MINUTE_MS = 60_000


@dataclass
class MinuteBucket:
    minute: int
    success: int = 0
    failure: int = 0
    timeout: int = 0

    @property
    def total(self) -> int:
        return self.success + self.failure + self.timeout

    @property
    def failed(self) -> int:
        return self.failure + self.timeout

    def to_dict(self) -> dict[str, int]:
        return {"minute": self.minute, "success": self.success,
                "failure": self.failure, "timeout": self.timeout}


@dataclass
class BreakerTransition:
    timestamp_ms: int
    from_state: str
    to_state: str
    window_minutes: list[int]
    failure_count: int
    total_count: int
    failure_rate_fixed: int
    failure_rate_decimal: str
    threshold_percent: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_ms": self.timestamp_ms,
            "from": self.from_state,
            "to": self.to_state,
            "window_minutes": self.window_minutes,
            "failure_count": self.failure_count,
            "total_count": self.total_count,
            "failure_rate_fixed": self.failure_rate_fixed,
            "failure_rate_decimal": self.failure_rate_decimal,
            "threshold_percent": self.threshold_percent,
            "reason": self.reason,
        }


@dataclass
class CircuitBreaker:
    service_id: str
    grid: Grid
    policy: BreakerPolicy
    state: str = STATE_CLOSED
    buckets: dict[int, MinuteBucket] = field(default_factory=dict)
    opened_at_ms: int | None = None
    probe_allowed: int = 0
    probe_success: int = 0
    probe_failed: int = 0
    transitions: list[BreakerTransition] = field(default_factory=list)

    def _evict(self, current_minute: int) -> list[int]:
        """整体移除滑出窗口的整分钟桶，返回被移除的分钟标识。"""
        oldest = current_minute - self.policy.window_minutes + 1
        removed = [minute for minute in self.buckets if minute < oldest]
        for minute in removed:
            del self.buckets[minute]
        return sorted(removed)

    def _window_stats(self) -> tuple[int, int, int, int, list[int]]:
        minutes = sorted(self.buckets)
        success = sum(b.success for b in self.buckets.values())
        failure = sum(b.failure for b in self.buckets.values())
        timeout = sum(b.timeout for b in self.buckets.values())
        total = success + failure + timeout
        return success, failure, timeout, total, minutes

    def window_success_probability(self) -> tuple[int, int] | None:
        """以窗口观测统计量化成功概率，返回 (定点值, 量化误差 ulp)；无样本返回 None。"""
        success, _, _, total, _ = self._window_stats()
        if total <= 0:
            return None
        quantized = quantize(success, total, self.grid)
        return quantized.value, quantized.error_ulps

    def admit(self, timestamp_ms: int) -> tuple[bool, dict[str, Any]]:
        """决定当前事件是否放行（限流通过后调用），并推进冷却/半开状态。"""
        current_minute = timestamp_ms // MINUTE_MS
        removed = self._evict(current_minute)
        detail: dict[str, Any] = {
            "state_before": self.state,
            "evicted_minutes": removed,
            "current_minute": current_minute,
        }
        if self.state == STATE_CLOSED:
            return True, detail
        if self.state == STATE_OPEN:
            if self.opened_at_ms is None:
                return False, detail
            cooled = timestamp_ms - self.opened_at_ms >= self.policy.cooldown_ms
            if cooled:
                self.state = STATE_HALF_OPEN
                self.probe_allowed = self.policy.half_open_probe
                self.probe_success = 0
                self.probe_failed = 0
                self.transitions.append(BreakerTransition(
                    timestamp_ms=timestamp_ms, from_state=STATE_OPEN,
                    to_state=STATE_HALF_OPEN, window_minutes=[],
                    failure_count=0, total_count=0, failure_rate_fixed=0,
                    failure_rate_decimal=format_fixed(0, self.grid),
                    threshold_percent=self.policy.failure_threshold_percent,
                    reason="cooldown elapsed, enter half-open",
                ))
                detail["state_before"] = STATE_OPEN
                detail["cooldown_to_half_open"] = True
                in_flight = 0
                detail["state_after"] = STATE_HALF_OPEN
                return True, detail
            else:
                # 拒绝但推进窗口时间基准，保证后续冷却判定使用最新时间戳
                detail["state_after"] = STATE_OPEN
                return False, detail
        # 到达这里时 state == HALF_OPEN（冷却转换可能在上面刚发生）
        in_flight = self.probe_success + self.probe_failed
        if in_flight >= self.probe_allowed:
            return False, detail
        return True, detail

    def record_result(self, timestamp_ms: int, result: str) -> dict[str, Any]:
        """登记已放行事件的结果并推进状态迁移，返回统计与定点失败率。"""
        current_minute = timestamp_ms // MINUTE_MS
        self._evict(current_minute)
        bucket = self.buckets.setdefault(current_minute, MinuteBucket(minute=current_minute))
        if result == RESULT_SUCCESS:
            bucket.success += 1
        elif result == RESULT_FAILURE:
            bucket.failure += 1
        elif result == RESULT_TIMEOUT:
            bucket.timeout += 1

        if self.state == STATE_HALF_OPEN:
            if result == RESULT_SUCCESS:
                self.probe_success += 1
            else:
                self.probe_failed += 1
            self._evaluate_half_open(timestamp_ms)
        elif self.state == STATE_CLOSED:
            self._evaluate_closed(timestamp_ms)
        # OPEN 状态下被拒绝的事件不会进入本方法（未放行不登记）

        success, failure, timeout, total, minutes = self._window_stats()
        rate = quantize(failure + timeout, total, self.grid) if total > 0 else None
        return {
            "state_after": self.state,
            "window_minutes": minutes,
            "success": success,
            "failure": failure,
            "timeout": timeout,
            "total": total,
            "failure_rate_fixed": None if rate is None else rate.value,
            "failure_rate_decimal": None if rate is None else format_fixed(rate.value, self.grid),
            "failure_rate_quant_remainder": None if rate is None else rate.remainder,
            "failure_rate_error_ulp": None if rate is None else rate.error_ulps,
            "threshold_percent": self.policy.failure_threshold_percent,
        }

    def _evaluate_closed(self, timestamp_ms: int) -> None:
        _, failure, timeout, total, minutes = self._window_stats()
        if total < self.policy.min_request_count:
            return
        failed = failure + timeout
        if not compare_percent(failed, total, self.policy.failure_threshold_percent):
            return
        rate = quantize(failed, total, self.grid)
        self.state = STATE_OPEN
        self.opened_at_ms = timestamp_ms
        self.transitions.append(BreakerTransition(
            timestamp_ms=timestamp_ms, from_state=STATE_CLOSED, to_state=STATE_OPEN,
            window_minutes=minutes, failure_count=failed, total_count=total,
            failure_rate_fixed=rate.value,
            failure_rate_decimal=format_fixed(rate.value, self.grid),
            threshold_percent=self.policy.failure_threshold_percent,
            reason="failure rate reached threshold",
        ))

    def _evaluate_half_open(self, timestamp_ms: int) -> None:
        if self.probe_failed > 0:
            _, failure, timeout, total, minutes = self._window_stats()
            rate = quantize(failure + timeout, total, self.grid) if total > 0 else None
            self.state = STATE_OPEN
            self.opened_at_ms = timestamp_ms
            self.transitions.append(BreakerTransition(
                timestamp_ms=timestamp_ms, from_state=STATE_HALF_OPEN, to_state=STATE_OPEN,
                window_minutes=minutes,
                failure_count=0 if rate is None else failure + timeout,
                total_count=total,
                failure_rate_fixed=0 if rate is None else rate.value,
                failure_rate_decimal=format_fixed(0 if rate is None else rate.value, self.grid),
                threshold_percent=self.policy.failure_threshold_percent,
                reason="half-open probe failed",
            ))
            return
        if self.probe_success >= self.probe_allowed:
            self.state = STATE_CLOSED
            self.buckets.clear()  # 关闭后重置窗口
            self.opened_at_ms = None
            self.probe_success = 0
            self.probe_failed = 0
            self.transitions.append(BreakerTransition(
                timestamp_ms=timestamp_ms, from_state=STATE_HALF_OPEN, to_state=STATE_CLOSED,
                window_minutes=[], failure_count=0, total_count=0,
                failure_rate_fixed=0,
                failure_rate_decimal=format_fixed(0, self.grid),
                threshold_percent=self.policy.failure_threshold_percent,
                reason="all half-open probes succeeded",
            ))

    def snapshot(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "state": self.state,
            "buckets": [bucket.to_dict() for bucket in self.buckets.values()],
            "opened_at_ms": self.opened_at_ms,
            "probe_allowed": self.probe_allowed,
            "probe_success": self.probe_success,
            "probe_failed": self.probe_failed,
            "transitions": [transition.to_dict() for transition in self.transitions],
        }

    @classmethod
    def restore(cls, data: dict[str, Any], policy: BreakerPolicy, grid: Grid) -> "CircuitBreaker":
        breaker = cls(service_id=data["service_id"], grid=grid, policy=policy,
                      state=data["state"], opened_at_ms=data["opened_at_ms"],
                      probe_allowed=data.get("probe_allowed", 0),
                      probe_success=data.get("probe_success", 0),
                      probe_failed=data.get("probe_failed", 0))
        breaker.buckets = {
            item["minute"]: MinuteBucket(
                minute=item["minute"], success=item["success"],
                failure=item["failure"], timeout=item["timeout"])
            for item in data.get("buckets", [])
        }
        breaker.transitions = [
            BreakerTransition(
                timestamp_ms=item["timestamp_ms"], from_state=item["from"],
                to_state=item["to"], window_minutes=item["window_minutes"],
                failure_count=item["failure_count"], total_count=item["total_count"],
                failure_rate_fixed=item["failure_rate_fixed"],
                failure_rate_decimal=item["failure_rate_decimal"],
                threshold_percent=item["threshold_percent"], reason=item["reason"],
            ) for item in data.get("transitions", [])
        ]
        return breaker
