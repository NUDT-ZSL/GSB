"""整数定点令牌桶。

设计要点
========
设网格缩放因子 ``S = 10^k``（1 令牌 = S 格，1 ulp = 1 格），
速率 ``r``（令牌/秒）。经过 ``e`` 毫秒的**精确**补充量（格）为::

    exact = r * S * e / 1000

实现上维护整数格空间的余数结转 ``carry_grid``（满足 0 <= carry < 1000）::

    num = r * S * e + carry
    refill_grid, new_carry = divmod(num, 1000)   # 全程整数、floor 截断
    carry <- new_carry

* 截断方向恒为 floor：补充只会「欠发」，绝不「超发」。
* 余数跨事件结转后，任意时刻相对精确轨迹的累计欠发严格小于 1 格（1 ulp）。
* 补充后若超过突发容量则封顶，溢出部分显式记录（防超发）。
* 每次请求消耗恰好 1 令牌（S 格），不足则拒绝且不扣令牌。
* 仅使用 int 运算，核心判定路径无浮点；重复推进得到逐位相同轨迹。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .fixedpoint import Grid, format_fixed
from .models import RateLimitPolicy


@dataclass
class TokenBucket:
    service_id: str
    grid: Grid
    rate_per_second: int
    capacity_grid: int
    tokens_grid: int
    carry_grid: int
    last_ts_ms: int | None
    calls: int = 0

    @classmethod
    def create(cls, service_id: str, policy: RateLimitPolicy, grid: Grid) -> "TokenBucket":
        capacity_grid = policy.burst_capacity * grid.scale
        return cls(
            service_id=service_id,
            grid=grid,
            rate_per_second=policy.rate_per_second,
            capacity_grid=capacity_grid,
            # 冷启动按突发容量预填满令牌（标准令牌桶约定）
            tokens_grid=capacity_grid,
            carry_grid=0,
            last_ts_ms=None,
        )

    def refill(self, timestamp_ms: int) -> dict[str, Any]:
        """按时间戳推进补充，返回补充量、余数结转与封顶明细。"""
        if self.last_ts_ms is None:
            self.last_ts_ms = timestamp_ms
            return {
                "elapsed_ms": 0,
                "refill_grid": 0,
                "carry_before_grid": 0,
                "carry_after_grid": 0,
                "capped": False,
                "overflow_grid": 0,
            }
        elapsed = timestamp_ms - self.last_ts_ms
        if elapsed < 0:  # 防御性处理；加载层已保证时间戳单调不减
            elapsed = 0
        carry_before = self.carry_grid
        numerator = self.rate_per_second * self.grid.scale * elapsed + carry_before
        refill_grid, new_carry = divmod(numerator, 1000)
        self.carry_grid = new_carry
        capped = False
        overflow = 0
        if refill_grid > 0:
            candidate = self.tokens_grid + refill_grid
            if candidate > self.capacity_grid:
                overflow = candidate - self.capacity_grid
                self.tokens_grid = self.capacity_grid
                capped = True
            else:
                self.tokens_grid = candidate
        self.last_ts_ms = timestamp_ms
        return {
            "elapsed_ms": elapsed,
            "refill_grid": refill_grid,
            "carry_before_grid": carry_before,
            "carry_after_grid": new_carry,
            "capped": capped,
            "overflow_grid": overflow,
        }

    def try_consume(self, timestamp_ms: int) -> dict[str, Any]:
        """推进补充并尝试消耗 1 令牌，返回完整决策与误差界。"""
        refill_info = self.refill(timestamp_ms)
        self.calls += 1
        cost = self.grid.scale
        allowed = self.tokens_grid >= cost
        before_consume = self.tokens_grid
        if allowed:
            self.tokens_grid -= cost
        return {
            "service_id": self.service_id,
            "timestamp_ms": timestamp_ms,
            "allowed": allowed,
            "cost_grid": cost,
            "tokens_before_grid": before_consume,
            "tokens_after_grid": self.tokens_grid,
            "capacity_grid": self.capacity_grid,
            "tokens_after_decimal": format_fixed(self.tokens_grid, self.grid),
            "refill": refill_info,
            "error_bound": {
                "unit": "ulp",
                "grid_scale": self.grid.scale,
                "per_refill_truncation_ulp": 1,
                "cumulative_under_issue_ulp": 1,
                "over_issue_ulp": 0,
                "note": "floor 截断只欠不超；余数跨事件结转，累计欠发严格 < 1 ulp",
            },
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "rate_per_second": self.rate_per_second,
            "capacity_grid": self.capacity_grid,
            "tokens_grid": self.tokens_grid,
            "carry_grid": self.carry_grid,
            "last_ts_ms": self.last_ts_ms,
            "calls": self.calls,
        }

    @classmethod
    def restore(cls, data: dict[str, Any], grid: Grid) -> "TokenBucket":
        return cls(
            service_id=data["service_id"],
            grid=grid,
            rate_per_second=data["rate_per_second"],
            capacity_grid=data["capacity_grid"],
            tokens_grid=data["tokens_grid"],
            carry_grid=data["carry_grid"],
            last_ts_ms=data["last_ts_ms"],
            calls=data.get("calls", 0),
        )
