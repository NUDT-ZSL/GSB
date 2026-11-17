"""Health state machine, slow-start weights, and round orchestration."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .config import Config, MemberConfig
from .models import (
    HealthState,
    MemberState,
    ProbeResult,
    RoundResult,
    RuntimeState,
    Transition,
    WeightDetail,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mono_ms() -> int:
    return time.monotonic_ns() // 1_000_000


@dataclass
class PendingStateChange:
    action: str
    reason: str


class HealthEngine:
    """Pure, deterministic state transitions for one configured backend pool.

    All state-changing methods are serialized. Drain requests issued while a
    round is in flight take effect at the next round boundary; the current
    probes are always allowed to complete before the member leaves the usable
    set.
    """

    def __init__(self, config: Config, state: RuntimeState | None = None) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._pending: dict[str, PendingStateChange] = {}
        if state is None:
            self._states = {
                member.id: MemberState(member_id=member.id) for member in config.members
            }
            self._current_round = 0
            self._available: tuple[str, ...] = ()
            self._weights: dict[str, int] = {member.id: 0 for member in config.members}
        else:
            self._states = {
                member.id: MemberState.from_dict(state.member_states[member.id].to_dict())
                if member.id in state.member_states
                else MemberState(member_id=member.id)
                for member in config.members
            }
            self._current_round = state.current_round
            available = set(state.available_members)
            self._available = tuple(
                member.id for member in config.members if member.id in available
            )
            self._weights = {
                member.id: int(state.effective_weights.get(member.id, 0))
                for member in config.members
            }

    @classmethod
    def from_runtime_state(cls, config: Config, state: RuntimeState) -> "HealthEngine":
        return cls(config, state)

    @property
    def current_round(self) -> int:
        with self._lock:
            return self._current_round

    @property
    def available_members(self) -> tuple[str, ...]:
        with self._lock:
            return self._available

    @property
    def effective_weights(self) -> dict[str, int]:
        with self._lock:
            return dict(self._weights)

    @property
    def full_weight_sum(self) -> int:
        return self.config.pool.full_weight_sum

    def get_member_state(self, member_id: str) -> MemberState:
        with self._lock:
            state = self._states[member_id]
            return MemberState.from_dict(state.to_dict())

    def member_states(self) -> dict[str, MemberState]:
        with self._lock:
            return {
                member.id: MemberState.from_dict(self._states[member.id].to_dict())
                for member in self.config.members
            }

    def snapshot_state(self) -> RuntimeState:
        with self._lock:
            return RuntimeState(
                current_round=self._current_round,
                member_states={
                    member.id: MemberState.from_dict(self._states[member.id].to_dict())
                    for member in self.config.members
                },
                available_members=tuple(self._available),
                effective_weights={
                    member.id: int(self._weights.get(member.id, 0))
                    for member in self.config.members
                },
                full_weight_sum=self.full_weight_sum,
            )

    def request_drain(self, member_id: str, reason: str = "operator drain request") -> None:
        self._require_member(member_id)
        with self._lock:
            self._pending[member_id] = PendingStateChange("drain", reason)

    def request_undrain(self, member_id: str, reason: str = "operator resume request") -> None:
        self._require_member(member_id)
        with self._lock:
            self._pending[member_id] = PendingStateChange("undrain", reason)

    def prepare_round(self, round_no: int) -> tuple[Transition, ...]:
        """Apply administrative state changes after the previous round completed."""

        transitions: list[Transition] = []
        with self._lock:
            for member in self.config.members:
                change = self._pending.pop(member.id, None)
                if change is None:
                    continue
                current = self._states[member.id]
                if change.action == "drain" and current.state != HealthState.DRAINING:
                    old_state = current.state
                    current.state = HealthState.DRAINING
                    current.slow_start_index = 0
                    current.last_transition_round = round_no
                    current.last_transition_reason = change.reason
                    transitions.append(
                        self._make_transition(
                            round_no,
                            member.id,
                            old_state,
                            HealthState.DRAINING,
                            f"{change.reason}; exit available after round {round_no} probes complete",
                        )
                    )
                elif change.action == "undrain" and current.state == HealthState.DRAINING:
                    old_state = current.state
                    current.state = HealthState.UNKNOWN
                    current.success_count = 0
                    current.failure_count = 0
                    current.slow_start_index = 0
                    current.last_transition_round = round_no
                    current.last_transition_reason = change.reason
                    transitions.append(
                        self._make_transition(
                            round_no,
                            member.id,
                            old_state,
                            HealthState.UNKNOWN,
                            change.reason,
                        )
                    )
        return tuple(transitions)

    def complete_round(
        self,
        round_no: int,
        probes: Iterable[ProbeResult],
        pre_round_transitions: Iterable[Transition] = (),
    ) -> RoundResult:
        probe_by_member = {probe.member_id: probe for probe in probes}
        with self._lock:
            expected_ids = {member.id for member in self.config.members}
            if set(probe_by_member) != expected_ids:
                missing = sorted(expected_ids - set(probe_by_member))
                extra = sorted(set(probe_by_member) - expected_ids)
                raise ValueError(f"probe/member mismatch; missing={missing}, extra={extra}")

            transitions = list(pre_round_transitions)
            for member in self.config.members:
                transition = self._apply_probe(member, round_no, probe_by_member[member.id])
                if transition is not None:
                    transitions.append(transition)

            self._current_round = round_no
            available, weights, details = self._calculate_traffic()
            self._available = available
            self._weights = weights
            state_after = RuntimeState(
                current_round=round_no,
                member_states={
                    member.id: MemberState.from_dict(self._states[member.id].to_dict())
                    for member in self.config.members
                },
                available_members=available,
                effective_weights=dict(weights),
                full_weight_sum=self.full_weight_sum,
            )
            return RoundResult(
                round_no=round_no,
                probes=tuple(probe_by_member[member.id] for member in self.config.members),
                transitions=tuple(transitions),
                available_members=available,
                effective_weights=dict(weights),
                weight_details=tuple(details),
                state_after=state_after,
                full_weight_sum=self.full_weight_sum,
                effective_weight_sum=sum(weights.values()),
            )

    def _apply_probe(
        self, member: MemberConfig, round_no: int, probe: ProbeResult
    ) -> Transition | None:
        state = self._states[member.id]
        state.last_round = round_no
        success_threshold = self.config.pool.success_threshold
        failure_threshold = self.config.pool.failure_threshold

        if state.state == HealthState.DRAINING:
            if probe.ok:
                state.success_count = min(success_threshold, state.success_count + 1)
                state.failure_count = 0
            else:
                state.failure_count = min(failure_threshold, state.failure_count + 1)
                state.success_count = 0
            return None

        old_state = state.state
        reason: str | None = None
        if probe.ok:
            state.success_count = min(success_threshold, state.success_count + 1)
            state.failure_count = 0
            if state.state == HealthState.UNKNOWN and state.success_count >= success_threshold:
                state.state = HealthState.HEALTHY
                state.slow_start_index = 0
                reason = (
                    f"{state.success_count} consecutive successful probes reached "
                    f"success_threshold={success_threshold}"
                )
            elif state.state == HealthState.UNHEALTHY and state.success_count >= success_threshold:
                state.state = HealthState.HEALTHY
                state.slow_start_index = 0
                reason = (
                    f"recovered after {state.success_count} consecutive successful probes "
                    f"reached success_threshold={success_threshold}; slow start restarted"
                )
            elif state.state == HealthState.HEALTHY:
                last_index = len(self.config.pool.slow_start_weight_table) - 1
                if state.slow_start_index < last_index:
                    state.slow_start_index += 1
        else:
            state.failure_count = min(failure_threshold, state.failure_count + 1)
            state.success_count = 0
            if state.state == HealthState.HEALTHY:
                state.slow_start_index = 0
            if state.state in {HealthState.UNKNOWN, HealthState.HEALTHY} and (
                state.failure_count >= failure_threshold
            ):
                state.state = HealthState.UNHEALTHY
                state.slow_start_index = 0
                reason = (
                    f"{state.failure_count} consecutive failed probes reached "
                    f"failure_threshold={failure_threshold}"
                )

        if old_state == state.state:
            return None
        state.last_transition_round = round_no
        state.last_transition_reason = reason or "state transition"
        return self._make_transition(
            round_no,
            member.id,
            old_state,
            state.state,
            reason or "state transition",
        )

    def _calculate_traffic(self) -> tuple[tuple[str, ...], dict[str, int], list[WeightDetail]]:
        table = self.config.pool.slow_start_weight_table
        available: list[str] = []
        weights: dict[str, int] = {}
        details: list[WeightDetail] = []
        for member in self.config.members:
            state = self._states[member.id]
            is_available = state.state == HealthState.HEALTHY
            index = 0 if not is_available else min(
                max(0, state.slow_start_index), len(table) - 1
            )
            percent = table[index] if is_available else 0
            weight = (member.weight * percent) // 100 if is_available else 0
            if is_available:
                available.append(member.id)
            weights[member.id] = weight
            details.append(
                WeightDetail(
                    member_id=member.id,
                    state=state.state,
                    base_weight=member.weight,
                    slow_start_index=index,
                    slow_start_percent=percent,
                    effective_weight=weight,
                    available=is_available,
                )
            )
        if sum(weights.values()) < 0:
            raise AssertionError("effective weight sum cannot be negative")
        return tuple(available), weights, details

    def _make_transition(
        self,
        round_no: int,
        member_id: str,
        from_state: HealthState,
        to_state: HealthState,
        reason: str,
    ) -> Transition:
        return Transition(
            round_no=round_no,
            member_id=member_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            occurred_mono_ms=_mono_ms(),
            occurred_utc=_utc_now_iso(),
        )

    def _require_member(self, member_id: str) -> None:
        if member_id not in {member.id for member in self.config.members}:
            raise KeyError(f"unknown member id: {member_id}")


class HealthController:
    """Drives probe rounds and persists each completed round into a ledger."""

    def __init__(
        self,
        config: Config,
        ledger: Any,
        engine: HealthEngine,
        prober: Any,
        round_started_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.engine = engine
        self.prober = prober
        self.round_started_callback = round_started_callback
        self._next_tick_mono: float | None = None

    def run_round(self, round_no: int | None = None) -> RoundResult:
        pending_round = self.engine.current_round + 1 if round_no is None else round_no
        attempt_id = self.ledger.begin_round(
            round_no=pending_round,
            config=self.config,
            started_mono_ms=_mono_ms(),
            started_utc=_utc_now_iso(),
        )
        if self.round_started_callback is not None:
            self.round_started_callback(pending_round, attempt_id)

        pre_round_transitions = self.engine.prepare_round(pending_round)
        probes = self.prober.probe_all(pending_round)
        result = self.engine.complete_round(pending_round, probes, pre_round_transitions)
        self.ledger.complete_round(
            attempt_id=attempt_id,
            config=self.config,
            result=result,
            completed_mono_ms=_mono_ms(),
            completed_utc=_utc_now_iso(),
        )
        return result

    def run_rounds(self, count: int, fixed_period: bool = False) -> list[RoundResult]:
        results: list[RoundResult] = []
        for _ in range(count):
            if fixed_period and self._next_tick_mono is not None:
                delay = self._next_tick_mono - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            result = self.run_round()
            results.append(result)
            if fixed_period:
                interval_s = self.config.pool.interval_ms / 1000.0
                now = time.monotonic()
                if self._next_tick_mono is None:
                    self._next_tick_mono = now + interval_s
                else:
                    self._next_tick_mono += interval_s
                    if self._next_tick_mono < now:
                        self._next_tick_mono = now
        return results
