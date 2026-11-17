"""Serializable domain models shared by engine, ledger, snapshot, and UI."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class HealthState(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DRAINING = "draining"

    @property
    def is_deciding(self) -> bool:
        return self in {HealthState.HEALTHY, HealthState.UNHEALTHY}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class MemberState:
    member_id: str
    state: HealthState = HealthState.UNKNOWN
    success_count: int = 0
    failure_count: int = 0
    slow_start_index: int = 0
    last_round: int = 0
    last_transition_round: int | None = None
    last_transition_reason: str = "initial unknown state"

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "state": self.state.value,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "slow_start_index": self.slow_start_index,
            "last_round": self.last_round,
            "last_transition_round": self.last_transition_round,
            "last_transition_reason": self.last_transition_reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemberState":
        return cls(
            member_id=str(value["member_id"]),
            state=HealthState(value["state"]),
            success_count=int(value.get("success_count", 0)),
            failure_count=int(value.get("failure_count", 0)),
            slow_start_index=int(value.get("slow_start_index", 0)),
            last_round=int(value.get("last_round", 0)),
            last_transition_round=(
                int(value["last_transition_round"])
                if value.get("last_transition_round") is not None
                else None
            ),
            last_transition_reason=str(value.get("last_transition_reason", "")),
        )


@dataclass
class RuntimeState:
    current_round: int
    member_states: dict[str, MemberState]
    available_members: tuple[str, ...]
    effective_weights: dict[str, int]
    full_weight_sum: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_round": self.current_round,
            "member_states": {
                key: self.member_states[key].to_dict() for key in sorted(self.member_states)
            },
            "available_members": list(self.available_members),
            "effective_weights": {
                key: self.effective_weights[key] for key in sorted(self.effective_weights)
            },
            "full_weight_sum": self.full_weight_sum,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeState":
        return cls(
            current_round=int(value.get("current_round", 0)),
            member_states={
                key: MemberState.from_dict(raw)
                for key, raw in value.get("member_states", {}).items()
            },
            available_members=tuple(value.get("available_members", [])),
            effective_weights={
                str(key): int(weight)
                for key, weight in value.get("effective_weights", {}).items()
            },
            full_weight_sum=int(value.get("full_weight_sum", 0)),
        )

    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProbeResult:
    round_no: int
    member_id: str
    protocol: str
    ok: bool
    start_mono_ms: int
    end_mono_ms: int
    summary: str
    failure_code: str | None = None

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_mono_ms - self.start_mono_ms)

    def deterministic(self) -> dict[str, Any]:
        return {
            "round": self.round_no,
            "member": self.member_id,
            "protocol": self.protocol,
            "ok": self.ok,
            "summary": self.summary,
            "failure_code": self.failure_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.deterministic(),
            "start_mono_ms": self.start_mono_ms,
            "end_mono_ms": self.end_mono_ms,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProbeResult":
        return cls(
            round_no=int(value["round"]),
            member_id=str(value["member"]),
            protocol=str(value["protocol"]),
            ok=bool(value["ok"]),
            start_mono_ms=int(value.get("start_mono_ms", 0)),
            end_mono_ms=int(value.get("end_mono_ms", 0)),
            summary=str(value.get("summary", "")),
            failure_code=value.get("failure_code"),
        )


@dataclass(frozen=True)
class Transition:
    round_no: int
    member_id: str
    from_state: HealthState
    to_state: HealthState
    reason: str
    occurred_mono_ms: int
    occurred_utc: str

    def deterministic(self) -> dict[str, Any]:
        return {
            "round": self.round_no,
            "member": self.member_id,
            "from": self.from_state.value,
            "to": self.to_state.value,
            "reason": self.reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.deterministic(),
            "occurred_mono_ms": self.occurred_mono_ms,
            "occurred_utc": self.occurred_utc,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Transition":
        return cls(
            round_no=int(value["round"]),
            member_id=str(value["member"]),
            from_state=HealthState(value["from"]),
            to_state=HealthState(value["to"]),
            reason=str(value.get("reason", "")),
            occurred_mono_ms=int(value.get("occurred_mono_ms", 0)),
            occurred_utc=str(value.get("occurred_utc", "")),
        )


@dataclass
class WeightDetail:
    member_id: str
    state: HealthState
    base_weight: int
    slow_start_index: int
    slow_start_percent: int
    effective_weight: int
    available: bool
    basis: str = "healthy: floor(base_weight * slow_start_percent / 100); otherwise 0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "state": self.state.value,
            "base_weight": self.base_weight,
            "slow_start_index": self.slow_start_index,
            "slow_start_percent": self.slow_start_percent,
            "effective_weight": self.effective_weight,
            "available": self.available,
            "basis": self.basis,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WeightDetail":
        return cls(
            member_id=str(value["member_id"]),
            state=HealthState(value["state"]),
            base_weight=int(value["base_weight"]),
            slow_start_index=int(value["slow_start_index"]),
            slow_start_percent=int(value["slow_start_percent"]),
            effective_weight=int(value["effective_weight"]),
            available=bool(value["available"]),
            basis=str(value.get("basis", "")),
        )


@dataclass
class RoundResult:
    round_no: int
    probes: tuple[ProbeResult, ...]
    transitions: tuple[Transition, ...]
    available_members: tuple[str, ...]
    effective_weights: dict[str, int]
    weight_details: tuple[WeightDetail, ...]
    state_after: RuntimeState
    full_weight_sum: int
    effective_weight_sum: int

    def deterministic(self) -> dict[str, Any]:
        return {
            "round": self.round_no,
            "probes": [probe.deterministic() for probe in self.probes],
            "transitions": [transition.deterministic() for transition in self.transitions],
            "available_members": list(self.available_members),
            "effective_weights": {
                key: self.effective_weights[key] for key in sorted(self.effective_weights)
            },
            "full_weight_sum": self.full_weight_sum,
            "effective_weight_sum": self.effective_weight_sum,
            "weight_details": [detail.to_dict() for detail in self.weight_details],
            "state_after": self.state_after.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.deterministic(),
            "probes": [probe.to_dict() for probe in self.probes],
            "transitions": [transition.to_dict() for transition in self.transitions],
            "state_after": self.state_after.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RoundResult":
        state_after = RuntimeState.from_dict(value["state_after"])
        return cls(
            round_no=int(value["round"]),
            probes=tuple(ProbeResult.from_dict(raw) for raw in value.get("probes", [])),
            transitions=tuple(Transition.from_dict(raw) for raw in value.get("transitions", [])),
            available_members=tuple(value.get("available_members", [])),
            effective_weights={
                str(key): int(weight)
                for key, weight in value.get("effective_weights", {}).items()
            },
            weight_details=tuple(
                WeightDetail.from_dict(raw) for raw in value.get("weight_details", [])
            ),
            state_after=state_after,
            full_weight_sum=int(value.get("full_weight_sum", 0)),
            effective_weight_sum=int(value.get("effective_weight_sum", 0)),
        )
