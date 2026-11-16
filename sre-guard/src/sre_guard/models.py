"""治理策略与业务事件的数据模型（均为可 JSON 序列化的 dataclass）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

#: 事件结果的标准枚举值
RESULT_SUCCESS = "success"
RESULT_FAILURE = "failure"
RESULT_TIMEOUT = "timeout"

#: 中文/别名到标准枚举的映射
RESULT_ALIASES = {
    "success": RESULT_SUCCESS,
    "成功": RESULT_SUCCESS,
    "failure": RESULT_FAILURE,
    "fail": RESULT_FAILURE,
    "失败": RESULT_FAILURE,
    "timeout": RESULT_TIMEOUT,
    "超时": RESULT_TIMEOUT,
}


class Outcome(str, Enum):
    """单事件最终决策。"""

    PASS = "pass"                    # 主路径放行
    RATE_LIMITED = "rate_limited"    # 限流拒绝
    DEGRADED = "degraded"            # 熔断打开后走降级链
    DEGRADE_FAILED = "degrade_failed"  # 降级链耗尽（E_DEGRADE，不静默放行）


@dataclass(frozen=True)
class RateLimitPolicy:
    rate_per_second: int       # 令牌桶填充速率（整数令牌/秒，正数）
    burst_capacity: int        # 突发容量（非负整数）


@dataclass(frozen=True)
class BreakerPolicy:
    window_minutes: int        # 滑动窗口分钟数（正整数）
    failure_threshold_percent: int  # 失败率阈值，0..100 整数百分比
    min_request_count: int     # 触发判定的最小请求数（正整数）
    half_open_probe: int       # 半开试探放行数（正整数）
    cooldown_ms: int = 60_000  # 打开后冷却时长（毫秒，正整数）


@dataclass(frozen=True)
class DegradeTarget:
    target: str                # 降级目标服务标识
    weight: int                # 权重（非负整数）
    success_permille: int = 1000  # 该目标名义成功概率，0..1000 的千分比整数


@dataclass(frozen=True)
class ServicePolicy:
    service_id: str
    rate_limit: RateLimitPolicy
    breaker: BreakerPolicy
    degrade_chain: list[DegradeTarget] = field(default_factory=list)


@dataclass(frozen=True)
class Policy:
    version: int
    services: dict[str, ServicePolicy]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "services": {sid: asdict(service) for sid, service in self.services.items()},
        }


@dataclass(frozen=True)
class Event:
    timestamp_ms: int
    service_id: str
    result: str
    latency_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp_ms": self.timestamp_ms,
            "service_id": self.service_id,
            "result": self.result,
            "latency_ms": self.latency_ms,
        }
