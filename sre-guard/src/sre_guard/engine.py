"""治理引擎：按事件顺序串联限流、熔断与降级决策。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .breaker import CircuitBreaker
from .degrade import DegradeDecision, choose_degrade
from .errors import E_DEGRADE, GovernanceError
from .fixedpoint import Grid
from .models import Event, Outcome, Policy
from .ratelimiter import TokenBucket

#: 重放时逐项比较的决策字段（逐位可复现的最小集合）
REPLAY_FIELDS = (
    "outcome",
    "allowed_by_rate_limiter",
    "breaker_state",
    "selected_degrade_target",
    "total_error_bound_ulp",
    "tokens_after_grid",
)


@dataclass
class DecisionRecord:
    event_index: int
    event: dict[str, Any]
    outcome: str
    allowed_by_rate_limiter: bool
    rate_limit: dict[str, Any]
    breaker_admitted: bool | None
    breaker_stats: dict[str, Any] | None
    breaker_state: str
    degrade: dict[str, Any] | None
    error_code: str | None
    total_error_bound_ulp: int
    tokens_after_grid: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_index": self.event_index,
            "event": self.event,
            "outcome": self.outcome,
            "allowed_by_rate_limiter": self.allowed_by_rate_limiter,
            "rate_limit": self.rate_limit,
            "breaker_admitted": self.breaker_admitted,
            "breaker_stats": self.breaker_stats,
            "breaker_state": self.breaker_state,
            "degrade": self.degrade,
            "error_code": self.error_code,
            "total_error_bound_ulp": self.total_error_bound_ulp,
            "tokens_after_grid": self.tokens_after_grid,
        }


@dataclass
class GovernanceEngine:
    policy: Policy
    grid: Grid
    buckets: dict[str, TokenBucket] = field(default_factory=dict)
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    decisions: list[DecisionRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        for service_id, service in self.policy.services.items():
            self.buckets[service_id] = TokenBucket.create(
                service_id, service.rate_limit, self.grid)
            self.breakers[service_id] = CircuitBreaker(
                service_id, self.grid, service.breaker)

    def _is_available(self, target: str) -> bool:
        breaker = self.breakers.get(target)
        if breaker is None:
            # 未受治理的外部目标按可用处理（权重选择仍由策略决定）
            return True
        return breaker.state != "open"

    def _observed_success(self, target: str):
        breaker = self.breakers.get(target)
        if breaker is None:
            return None
        return breaker.window_success_probability()

    def process_event(self, event: Event, event_index: int) -> DecisionRecord:
        service = self.policy.services.get(event.service_id)
        if service is None:
            # 未配置策略的服务不做治理，直接放行（仍记录轨迹）
            record = DecisionRecord(
                event_index=event_index, event=event.to_dict(),
                outcome=Outcome.PASS.value, allowed_by_rate_limiter=True,
                rate_limit={}, breaker_admitted=None, breaker_stats=None,
                breaker_state="unmanaged", degrade=None, error_code=None,
                total_error_bound_ulp=0, tokens_after_grid=-1,
            )
            self.decisions.append(record)
            return record

        bucket = self.buckets[event.service_id]
        breaker = self.breakers[event.service_id]

        rate_decision = bucket.try_consume(event.timestamp_ms)
        total_error = int(rate_decision["error_bound"]["per_refill_truncation_ulp"])

        if not rate_decision["allowed"]:
            record = DecisionRecord(
                event_index=event_index, event=event.to_dict(),
                outcome=Outcome.RATE_LIMITED.value,
                allowed_by_rate_limiter=False, rate_limit=rate_decision,
                breaker_admitted=None, breaker_stats=None,
                breaker_state=breaker.state, degrade=None, error_code=None,
                total_error_bound_ulp=total_error,
                tokens_after_grid=rate_decision["tokens_after_grid"],
            )
            self.decisions.append(record)
            return record

        admitted, admit_detail = breaker.admit(event.timestamp_ms)
        if not admitted:
            return self._handle_degrade(event, event_index, service.degrade_chain,
                                        breaker, rate_decision, total_error, admit_detail)
        stats = breaker.record_result(event.timestamp_ms, event.result)
        if breaker.state == "open":
            # 半开试探失败重新打开：主路径不可用，进入降级链
            return self._handle_degrade(event, event_index, service.degrade_chain,
                                        breaker, rate_decision, total_error,
                                        {"admit_detail": admit_detail, "stats": stats})
        record = DecisionRecord(
            event_index=event_index, event=event.to_dict(),
            outcome=Outcome.PASS.value, allowed_by_rate_limiter=True,
            rate_limit=rate_decision, breaker_admitted=True,
            breaker_stats=stats, breaker_state=breaker.state, degrade=None,
            error_code=None, total_error_bound_ulp=total_error,
            tokens_after_grid=rate_decision["tokens_after_grid"],
        )
        self.decisions.append(record)
        return record

    def _handle_degrade(self, event, event_index, chain, breaker,
                        rate_decision, total_error, admit_detail) -> DecisionRecord:
        try:
            degrade_decision: DegradeDecision = choose_degrade(
                service_id=event.service_id,
                event_index=event_index,
                timestamp_ms=event.timestamp_ms,
                chain=chain,
                grid=self.grid,
                is_available=self._is_available,
                observed_success=self._observed_success,
            )
        except GovernanceError as exc:
            if exc.code != E_DEGRADE:
                raise
            record = DecisionRecord(
                event_index=event_index, event=event.to_dict(),
                outcome=Outcome.DEGRADE_FAILED.value,
                allowed_by_rate_limiter=True, rate_limit=rate_decision,
                breaker_admitted=False, breaker_stats=admit_detail,
                breaker_state=breaker.state, degrade=None,
                error_code=E_DEGRADE, total_error_bound_ulp=total_error,
                tokens_after_grid=rate_decision["tokens_after_grid"],
            )
            self.decisions.append(record)
            return record
        total_error += degrade_decision.total_error_ulp
        record = DecisionRecord(
            event_index=event_index, event=event.to_dict(),
            outcome=Outcome.DEGRADED.value, allowed_by_rate_limiter=True,
            rate_limit=rate_decision, breaker_admitted=False,
            breaker_stats=admit_detail, breaker_state=breaker.state,
            degrade=degrade_decision.to_dict(), error_code=None,
            total_error_bound_ulp=total_error,
            tokens_after_grid=rate_decision["tokens_after_grid"],
        )
        self.decisions.append(record)
        return record

    def run(self, events: list[Event]) -> list[DecisionRecord]:
        for index, event in enumerate(events):
            self.process_event(event, index)
        return self.decisions

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "rate_limiters": {sid: bucket.snapshot()
                              for sid, bucket in self.buckets.items()},
            "breakers": {sid: breaker.snapshot()
                         for sid, breaker in self.breakers.items()},
        }
