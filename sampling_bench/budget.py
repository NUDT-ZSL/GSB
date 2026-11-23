"""三预算账本：总采样步数、驻留 KV 缓存条目、并发槽位。

账本只记录确定性事件（带单调序号），任何扣减与释放都可由逐请求明细完整
重算。采用「准入即驻留」模型：请求一旦通过准入即占用一份缓存与一个槽位，
直到该请求采样完成后统一释放，从而可以静态、确定性地裁决准入集合。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import LedgerEvent, Request


def request_cache_entries(request: Request) -> int:
    """单个请求的 KV 缓存规模：提示词长度加目标生成步数。"""
    return len(request.prompt_tokens) + request.target_length


def request_step_demand(request: Request) -> int:
    """单个请求的采样步数需求（至多 target_length 步）。"""
    return request.target_length


@dataclass
class BudgetLimits:
    step_budget: int
    cache_budget: int
    slot_budget: int

    def to_dict(self) -> dict[str, int]:
        return {
            "step_budget": self.step_budget,
            "cache_budget": self.cache_budget,
            "slot_budget": self.slot_budget,
        }


@dataclass
class BudgetLedger:
    limits: BudgetLimits
    events: list[LedgerEvent] = field(default_factory=list)
    _seq: int = 0
    _used: dict[str, int] = field(default_factory=lambda: {"steps": 0, "cache": 0, "slots": 0})

    def balances(self) -> dict[str, int]:
        return {
            "steps": self.limits.step_budget - self._used["steps"],
            "cache": self.limits.cache_budget - self._used["cache"],
            "slots": self.limits.slot_budget - self._used["slots"],
        }

    def used(self) -> dict[str, int]:
        return dict(self._used)

    def _append(self, request_id: str, kind: str, dimension: str, amount: int) -> None:
        self._seq += 1
        if kind == "reserve":
            self._used[dimension] += amount
        else:
            self._used[dimension] -= amount
        balance = self._limit_for(dimension) - self._used[dimension]
        self.events.append(
            LedgerEvent(
                seq=self._seq,
                request_id=request_id,
                kind=kind,
                dimension=dimension,
                amount=amount,
                balance_after=balance,
            )
        )

    def _limit_for(self, dimension: str) -> int:
        return {
            "steps": self.limits.step_budget,
            "cache": self.limits.cache_budget,
            "slots": self.limits.slot_budget,
        }[dimension]

    def can_reserve(self, request: Request) -> tuple[bool, list[str]]:
        """返回是否可准入以及被违反的维度列表（按 steps/cache/slots 固定顺序）。"""
        violated: list[str] = []
        if self._used["steps"] + request_step_demand(request) > self.limits.step_budget:
            violated.append("steps")
        if self._used["cache"] + request_cache_entries(request) > self.limits.cache_budget:
            violated.append("cache")
        if self._used["slots"] + 1 > self.limits.slot_budget:
            violated.append("slots")
        return not violated, violated

    def reserve(self, request: Request) -> None:
        self._append(request.request_id, "reserve", "steps", request_step_demand(request))
        self._append(request.request_id, "reserve", "cache", request_cache_entries(request))
        self._append(request.request_id, "reserve", "slots", 1)

    def release(self, request: Request) -> None:
        self._append(request.request_id, "release", "slots", 1)
        self._append(request.request_id, "release", "cache", request_cache_entries(request))
        self._append(request.request_id, "release", "steps", request_step_demand(request))

    def to_dict(self) -> dict[str, object]:
        return {
            "limits": self.limits.to_dict(),
            "used": self.used(),
            "balances": self.balances(),
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_events(cls, limits: BudgetLimits, events: list[LedgerEvent]) -> "BudgetLedger":
        ledger = cls(limits=limits)
        for event in events:
            ledger._seq = max(ledger._seq, event.seq)
            sign = 1 if event.kind == "reserve" else -1
            ledger._used[event.dimension] += sign * event.amount
        ledger.events = list(events)
        return ledger
