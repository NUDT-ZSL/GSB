from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .ledger import FixedPointLedger, PrecisionMarker
from .models import (
    FunctionSpec,
    InstancePhase,
    RequestRecord,
    WarmupCandidate,
)
from .store import RecoveryReport, StateStore
from .utils import canonical_json, sha256_json


@dataclass
class EngineConfig:
    global_cold_start_limit: int = 2
    round_budget_ms: int = 220
    precision_tolerance_ms: int = 10
    fixed_point_scale: int = 100
    fixed_point_unit_ms: int = 1000
    fixed_point_rounding: str = "floor"
    recycle_grace_ms: int = 20
    capacity_shortage_policy: str = "hold_first_then_abandon_tail"
    budget_shortage_policy: str = "abandon"


class DiagnosticEngine:
    """Deterministic state-machine, scheduler, and serial dispatcher."""

    def __init__(
        self,
        functions: list[FunctionSpec],
        requests: list[RequestRecord] | None = None,
        policy_view: dict[str, Any] | None = None,
        config: EngineConfig | None = None,
        store: StateStore | None = None,
        db_path: str = ":memory:",
        policy_pointer: dict[str, Any] | None = None,
    ) -> None:
        self.functions = {item.name: item for item in functions}
        self.requests = list(requests or [])
        self.requests_by_seq = {item.seq: item for item in self.requests}
        self.policy_view = policy_view or {}
        self.config = config or EngineConfig()
        self.store = store or StateStore(db_path)
        self.owns_store = store is None
        self.policy_pointer = policy_pointer
        self.ledger = FixedPointLedger(
            scale=self.config.fixed_point_scale,
            unit_ms=self.config.fixed_point_unit_ms,
            default_rounding=self.config.fixed_point_rounding,
            tolerance_ms=self.config.precision_tolerance_ms,
        )
        self.instances: dict[str, dict[str, Any]] = {}
        self.calls: dict[int, dict[str, Any]] = {}
        self.queued_seqs: list[int] = []
        self.decisions: list[dict[str, Any]] = []
        self.budget_charges: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.new_events: list[dict[str, Any]] = []
        self.instance_sequences: dict[str, int] = {}
        self.logical_clock_watermark_ms = 0
        self.processed_request_seq = 0
        self.precision_pairs: set[tuple[int, int]] = set()
        self.recovery_report: RecoveryReport | None = None
        self.peak_warm_concurrency = 0

    def close(self) -> None:
        if self.owns_store:
            self.store.close()

    def _emit(
        self, clock_ms: int, kind: str, payload: dict[str, Any], step_id: str | None = None
    ) -> dict[str, Any]:
        event = {
            "event_index": len(self.events) + 1,
            "step_id": step_id,
            "clock_ms": clock_ms,
            "kind": kind,
            "payload": payload,
        }
        self.events.append(event)
        self.new_events.append(event)
        self.logical_clock_watermark_ms = max(self.logical_clock_watermark_ms, clock_ms)
        return event

    def _transition(
        self,
        instance: dict[str, Any],
        new_phase: InstancePhase | str,
        clock_ms: int,
        reason: str,
    ) -> None:
        new_value = new_phase.value if isinstance(new_phase, InstancePhase) else str(new_phase)
        old_value = instance["phase"]
        if old_value == new_value:
            return
        instance["history"].append(
            {
                "from_phase": old_value,
                "to_phase": new_value,
                "clock_ms": clock_ms,
                "reason": reason,
            }
        )
        instance["phase"] = new_value
        instance["phase_entered_clock_ms"] = clock_ms
        self._emit(
            clock_ms,
            "instance_phase_changed",
            {
                "instance_id": instance["id"],
                "function_name": instance["function_name"],
                "from_phase": old_value,
                "to_phase": new_value,
                "reason": reason,
            },
        )

    def _next_instance_id(self, function_name: str) -> str:
        index = self.instance_sequences.get(function_name, 0) + 1
        self.instance_sequences[function_name] = index
        return f"{function_name}-{index}"

    def ensure_instance(
        self,
        function_name: str,
        *,
        instance_id: str | None = None,
        request_seq: int | None = None,
        request_priority: int | None = None,
        round_id: str | None = None,
        candidate_order: int = 0,
    ) -> dict[str, Any]:
        spec = self.functions[function_name]
        if instance_id and instance_id in self.instances:
            return self.instances[instance_id]
        if instance_id is None:
            instance_id = self._next_instance_id(function_name)
        else:
            try:
                suffix = int(instance_id.rsplit("-", 1)[-1])
                self.instance_sequences[function_name] = max(
                    self.instance_sequences.get(function_name, 0), suffix
                )
            except (ValueError, IndexError):
                pass
        if request_priority is None and request_seq is not None:
            request = self.requests_by_seq.get(request_seq)
            request_priority = request.priority if request else 100
        instance = {
            "id": instance_id,
            "function_name": function_name,
            "phase": InstancePhase.PENDING_WARMUP.value,
            "instance_index": self.instance_sequences.get(function_name, 1),
            "created_clock_ms": self.logical_clock_watermark_ms,
            "phase_entered_clock_ms": self.logical_clock_watermark_ms,
            "logical_clock_ms": self.logical_clock_watermark_ms,
            "cold_start_ms": spec.cold_start_ms,
            "concurrency_limit": spec.concurrency_limit,
            "active_count": 0,
            "active_call_seqs": [],
            "ready_at_ms": None,
            "failure_at_ms": None,
            "warmup_expires_at_ms": None,
            "recycle_after_ms": None,
            "request_seq": request_seq,
            "request_priority": request_priority if request_priority is not None else 100,
            "function_priority": spec.priority,
            "round_id": round_id,
            "candidate_order": candidate_order,
            "charge_id": None,
            "retirement_reason": None,
            "policy_snapshot_id": (self.policy_pointer or {}).get("snapshot_id"),
            "history": [
                {
                    "from_phase": None,
                    "to_phase": InstancePhase.PENDING_WARMUP.value,
                    "clock_ms": self.logical_clock_watermark_ms,
                    "reason": "created",
                }
            ],
        }
        self.instances[instance_id] = instance
        return instance

    def _failure_delay(self, function_name: str) -> int | None:
        spec = self.functions[function_name]
        raw = spec.raw or {}
        if raw.get("warmup_failure") is True or raw.get("fail_warmup") is True:
            delay = raw.get("warmup_failure_delay_ms")
            if isinstance(delay, int) and not isinstance(delay, bool) and delay >= 0:
                return delay
            return max(1, spec.cold_start_ms // 2)
        rules = getattr(self.config, "warmup_failure_rules", None)
        if isinstance(rules, dict) and function_name in rules:
            value = rules[function_name]
            return value if isinstance(value, int) else max(1, spec.cold_start_ms // 2)
        return None

    def advance_to(self, clock_ms: int) -> None:
        if clock_ms < 0:
            raise ValueError("injected logical clock must be non-negative")
        self.logical_clock_watermark_ms = max(self.logical_clock_watermark_ms, clock_ms)

        while True:
            event = self._next_timed_event(clock_ms)
            if event is None:
                break
            event_clock, category, event_key, event_kind = event
            if category == 0:
                self._complete_call(int(event_key), event_clock)
                self._dispatch_queued(event_clock)
            elif category == 1:
                instance = self.instances[str(event_key)]
                if event[3] == "failure":
                    self._transition(
                        instance,
                        InstancePhase.FAILED,
                        event_clock,
                        "deterministic_warmup_failure",
                    )
                    self._append_failure_decision(instance)
                else:
                    self._transition(
                        instance, InstancePhase.READY, event_clock, "cold_start_completed"
                    )
                    window = self.functions[instance["function_name"]].warmup_window_ms
                    instance["warmup_expires_at_ms"] = (
                        event_clock + window if window > 0 else None
                    )
                    self._dispatch_queued(event_clock)
            elif category == 2 and event[3] == "expire":
                instance = self.instances[str(event_key)]
                if int(instance["active_count"]) == 0:
                    instance["retirement_reason"] = "warmup_window_expired"
                    self._transition(
                        instance,
                        InstancePhase.RECYCLING,
                        event_clock,
                        "warmup_window_expired",
                    )
                    instance["recycle_after_ms"] = (
                        event_clock + self.config.recycle_grace_ms
                    )
                else:
                    instance["retirement_reason"] = "warmup_window_expired_drain"
                instance["warmup_expires_at_ms"] = None
            elif category == 2 and event[3] == "recycle":
                instance = self.instances[str(event_key)]
                self._transition(
                    instance,
                    InstancePhase.RECYCLED,
                    event_clock,
                    "recycle_grace_elapsed",
                )

        for instance in self.instances.values():
            instance["logical_clock_ms"] = clock_ms
        self._dispatch_queued(clock_ms)

    def _next_timed_event(self, target_clock: int) -> tuple[int, int, str, str] | None:
        candidates: list[tuple[int, int, str, str]] = []
        for seq, call in self.calls.items():
            if call["status"] == "dispatched":
                completion_clock = int(call["completion_clock_ms"])
                if completion_clock <= target_clock:
                    candidates.append((completion_clock, 0, f"{seq:012d}", "complete"))

        for instance_id, instance in self.instances.items():
            phase = instance["phase"]
            if phase == InstancePhase.WARMING.value:
                ready_at = instance.get("ready_at_ms")
                failure_at = instance.get("failure_at_ms")
                if (
                    failure_at is not None
                    and int(failure_at) <= target_clock
                    and (ready_at is None or int(failure_at) < int(ready_at))
                ):
                    candidates.append((int(failure_at), 1, instance_id, "failure"))
                if ready_at is not None and int(ready_at) <= target_clock:
                    candidates.append((int(ready_at), 1, instance_id, "ready"))
            elif phase == InstancePhase.READY.value:
                expires = instance.get("warmup_expires_at_ms")
                if expires is not None and int(expires) <= target_clock:
                    candidates.append((int(expires), 2, instance_id, "expire"))
            elif phase == InstancePhase.RECYCLING.value:
                recycle_after = instance.get("recycle_after_ms")
                if recycle_after is not None and int(recycle_after) <= target_clock:
                    candidates.append((int(recycle_after), 2, instance_id, "recycle"))

        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1], item[2]))

    def _complete_call(self, seq: int, completion_clock: int) -> None:
        call = self.calls[seq]
        if call["status"] != "dispatched":
            return
        call["status"] = "completed"
        call["completed_clock_ms"] = completion_clock
        instance = self.instances[call["instance_id"]]
        instance["active_count"] = max(0, int(instance["active_count"]) - 1)
        if seq in instance["active_call_seqs"]:
            instance["active_call_seqs"].remove(seq)
        self._emit(
            completion_clock,
            "call_completed",
            {"seq": seq, "instance_id": instance["id"]},
        )
        if instance["active_count"] == 0:
            if instance.get("retirement_reason"):
                reason = str(instance["retirement_reason"])
                self._transition(instance, InstancePhase.RECYCLING, completion_clock, reason)
                instance["recycle_after_ms"] = completion_clock + self.config.recycle_grace_ms
            elif instance["phase"] == InstancePhase.BUSY.value:
                self._transition(
                    instance, InstancePhase.READY, completion_clock, "all_calls_completed"
                )

    def _append_failure_decision(self, instance: dict[str, Any]) -> None:
        decision = {
            "candidate_id": instance["id"],
            "function_name": instance["function_name"],
            "request_seq": instance.get("request_seq"),
            "outcome": "warmup_failed",
            "reason": "deterministic_warmup_failure",
            "round_id": instance.get("round_id") or "",
            "clock_ms": instance["phase_entered_clock_ms"],
            "cold_start_ms": instance["cold_start_ms"],
            "budget_before_ms": 0,
            "budget_after_ms": 0,
            "active_warm_before": 0,
            "active_warm_after": 0,
            "instance_id": instance["id"],
            "charge_id": instance.get("charge_id"),
        }
        self.decisions.append(decision)
        self._emit(
            decision["clock_ms"],
            "warmup_failed",
            {"instance_id": instance["id"], "charge_id": instance.get("charge_id")},
        )

    def _active_warm_count(self) -> int:
        return sum(
            1
            for instance in self.instances.values()
            if instance["phase"] == InstancePhase.WARMING.value
        )

    def _round_consumed_budget(self, round_id: str) -> int:
        return sum(
            int(charge["amount_ms"])
            for charge in self.budget_charges.values()
            if charge["round_id"] == round_id
        )

    def schedule_round(
        self,
        round_id: str,
        clock_ms: int,
        candidates: list[WarmupCandidate | dict[str, Any]] | None = None,
        *,
        budget_ms: int | None = None,
        global_limit: int | None = None,
        budget_shortage_policy: str | None = None,
        capacity_shortage_policy: str | None = None,
    ) -> list[dict[str, Any]]:
        self.advance_to(clock_ms)
        budget = self.config.round_budget_ms if budget_ms is None else budget_ms
        limit = self.config.global_cold_start_limit if global_limit is None else global_limit
        budget_policy = budget_shortage_policy or self.config.budget_shortage_policy
        cap_policy = capacity_shortage_policy or self.config.capacity_shortage_policy

        if candidates is not None:
            for order, candidate in enumerate(candidates):
                data = asdict(candidate) if isinstance(candidate, WarmupCandidate) else dict(candidate)
                function_name = data["function_name"]
                if function_name not in self.functions:
                    raise KeyError(f"unknown function {function_name!r}")
                self.ensure_instance(
                    function_name,
                    instance_id=str(data.get("candidate_id") or data.get("instance_id")),
                    request_seq=data.get("request_seq"),
                    request_priority=data.get("request_priority"),
                    round_id=round_id,
                    candidate_order=int(data.get("order_index", order)),
                )

        pending = [
            instance
            for instance in self.instances.values()
            if instance["phase"] == InstancePhase.PENDING_WARMUP.value
        ]
        pending.sort(
            key=lambda item: (
                int(item["function_priority"]),
                int(item.get("request_priority", 100)),
                int(item.get("request_seq") or 0),
                int(item.get("candidate_order", 0)),
                item["id"],
            )
        )

        round_decisions: list[dict[str, Any]] = []
        consumed = self._round_consumed_budget(round_id)
        active_warm = self._active_warm_count()
        self.peak_warm_concurrency = max(self.peak_warm_concurrency, active_warm)
        held_seen = False
        tail_abandon = False

        for instance in pending:
            cost = int(instance["cold_start_ms"])
            charge_id = f"warmup:{round_id}:{instance['id']}"
            base_decision = {
                "candidate_id": instance["id"],
                "function_name": instance["function_name"],
                "request_seq": instance.get("request_seq"),
                "round_id": round_id,
                "clock_ms": clock_ms,
                "cold_start_ms": cost,
                "instance_id": instance["id"],
                "charge_id": charge_id,
            }

            def record(
                outcome: str,
                reason: str,
                budget_before: int,
                budget_after: int,
                warm_before: int,
                warm_after: int,
            ) -> dict[str, Any]:
                decision = {
                    **base_decision,
                    "outcome": outcome,
                    "reason": reason,
                    "budget_before_ms": budget_before,
                    "budget_after_ms": budget_after,
                    "active_warm_before": warm_before,
                    "active_warm_after": warm_after,
                }
                self.decisions.append(decision)
                round_decisions.append(decision)
                self._emit(clock_ms, "warmup_decision", decision)
                return decision

            if tail_abandon:
                self._transition(
                    instance, InstancePhase.ABANDONED, clock_ms, "prior_shortage_tail"
                )
                instance["retirement_reason"] = "prior_shortage_tail"
                record(
                    "abandoned",
                    "prior_shortage_tail",
                    budget - consumed,
                    budget - consumed,
                    active_warm,
                    active_warm,
                )
                continue

            remaining = budget - consumed
            if cost > remaining:
                reason = "insufficient_logical_budget"
                if budget_policy == "hold":
                    record(
                        "not_started_in_budget",
                        reason,
                        remaining,
                        remaining,
                        active_warm,
                        active_warm,
                    )
                    continue
                self._transition(instance, InstancePhase.ABANDONED, clock_ms, reason)
                instance["retirement_reason"] = reason
                record("abandoned", reason, remaining, remaining, active_warm, active_warm)
                tail_abandon = True
                continue

            if active_warm >= limit:
                reason = "global_cold_start_limit"
                if cap_policy == "abandon" or (
                    cap_policy == "hold_first_then_abandon_tail" and held_seen
                ):
                    tail_reason = (
                        "global_cold_start_limit_tail"
                        if cap_policy == "hold_first_then_abandon_tail"
                        else reason
                    )
                    self._transition(instance, InstancePhase.ABANDONED, clock_ms, tail_reason)
                    instance["retirement_reason"] = tail_reason
                    record(
                        "abandoned",
                        tail_reason,
                        remaining,
                        remaining,
                        active_warm,
                        active_warm,
                    )
                    if cap_policy == "abandon":
                        tail_abandon = True
                    continue

                held_seen = True
                record(
                    "not_started_in_budget",
                    reason,
                    remaining,
                    remaining,
                    active_warm,
                    active_warm,
                )
                continue

            conversion = self.ledger.convert(
                cost, label=f"cold-start:{instance['id']}:{round_id}"
            )
            instance["fixed_point_entry_id"] = conversion["entry_id"]
            instance["phase"] = InstancePhase.WARMING.value
            instance["phase_entered_clock_ms"] = clock_ms
            instance["history"].append(
                {
                    "from_phase": InstancePhase.PENDING_WARMUP.value,
                    "to_phase": InstancePhase.WARMING.value,
                    "clock_ms": clock_ms,
                    "reason": "budget_and_global_slot_available",
                }
            )
            ready_at = clock_ms + cost
            instance["ready_at_ms"] = ready_at
            delay = self._failure_delay(instance["function_name"])
            if delay is not None:
                instance["failure_at_ms"] = clock_ms + delay
            instance["charge_id"] = charge_id
            charge = {
                "charge_id": charge_id,
                "step_id": None,
                "round_id": round_id,
                "candidate_id": instance["id"],
                "amount_ms": cost,
                "clock_ms": clock_ms,
            }
            self.budget_charges[charge_id] = charge
            consumed += cost
            active_warm += 1
            self.peak_warm_concurrency = max(self.peak_warm_concurrency, active_warm)
            self._emit(
                clock_ms,
                "instance_phase_changed",
                {
                    "instance_id": instance["id"],
                    "function_name": instance["function_name"],
                    "from_phase": InstancePhase.PENDING_WARMUP.value,
                    "to_phase": InstancePhase.WARMING.value,
                    "reason": "budget_and_global_slot_available",
                },
            )
            record(
                "started",
                "started",
                remaining,
                budget - consumed,
                active_warm - 1,
                active_warm,
            )

        return round_decisions

    def _eligible_instance(self, function_name: str) -> dict[str, Any] | None:
        eligible = [
            instance
            for instance in self.instances.values()
            if instance["function_name"] == function_name
            and instance["phase"] in {InstancePhase.READY.value, InstancePhase.BUSY.value}
            and int(instance["active_count"]) < int(instance["concurrency_limit"])
        ]
        eligible.sort(key=lambda item: (int(item["instance_index"]), item["id"]))
        return eligible[0] if eligible else None

    def _allocate(self, call: dict[str, Any], instance: dict[str, Any], clock_ms: int) -> None:
        seq = int(call["seq"])
        instance["active_count"] = int(instance["active_count"]) + 1
        instance["active_call_seqs"].append(seq)
        if instance["phase"] == InstancePhase.READY.value:
            self._transition(instance, InstancePhase.BUSY, clock_ms, "call_dispatched")
        call["status"] = "dispatched"
        call["instance_id"] = instance["id"]
        call["dispatch_clock_ms"] = clock_ms
        call["wait_ms"] = clock_ms - int(call["arrival_clock_ms"])
        call["completion_clock_ms"] = clock_ms + int(call["occupancy_ms"])
        call["remaining_concurrency_after"] = int(instance["concurrency_limit"]) - int(
            instance["active_count"]
        )
        self._emit(
            clock_ms,
            "call_dispatched",
            {
                "seq": seq,
                "function_name": call["function_name"],
                "instance_id": instance["id"],
                "wait_ms": call["wait_ms"],
                "completion_clock_ms": call["completion_clock_ms"],
            },
        )

    def _dispatch_queued(self, clock_ms: int) -> None:
        still_queued: list[int] = []
        for seq in list(self.queued_seqs):
            call = self.calls[seq]
            if call["status"] != "queued":
                continue
            if clock_ms < int(call["arrival_clock_ms"]):
                still_queued.append(seq)
                continue
            instance = self._eligible_instance(call["function_name"])
            if instance is None:
                still_queued.append(seq)
                continue
            self._allocate(call, instance, clock_ms)
        self.queued_seqs = still_queued

    def process_request(self, request: RequestRecord) -> dict[str, Any]:
        seq = request.seq
        if seq in self.calls and self.calls[seq]["status"] in {"dispatched", "completed"}:
            return self.calls[seq]

        self.processed_request_seq = max(self.processed_request_seq, seq)
        self.advance_to(request.logical_clock_ms)
        call = self.calls.get(
            seq,
            {
                "seq": seq,
                "function_name": request.function_name,
                "priority": request.priority,
                "arrival_clock_ms": request.logical_clock_ms,
                "expected_latency_ms": request.expected_latency_ms,
                "occupancy_ms": request.occupancy_ms,
                "call_status": request.call_status,
                "status": "queued",
                "instance_id": None,
                "dispatch_clock_ms": None,
                "wait_ms": None,
                "completion_clock_ms": None,
            },
        )
        self.calls[seq] = call

        instance = self._eligible_instance(request.function_name)
        if instance is not None:
            self._allocate(call, instance, request.logical_clock_ms)
            if seq in self.queued_seqs:
                self.queued_seqs.remove(seq)
            return call

        if seq not in self.queued_seqs:
            self.queued_seqs.append(seq)
        total_capacity = sum(
            int(item["concurrency_limit"])
            for item in self.instances.values()
            if item["function_name"] == request.function_name
            and item["phase"]
            not in {
                InstancePhase.RECYCLING.value,
                InstancePhase.RECYCLED.value,
                InstancePhase.ABANDONED.value,
                InstancePhase.FAILED.value,
            }
        )
        demand = sum(
            1
            for item in self.calls.values()
            if item["function_name"] == request.function_name
            and item["status"] in {"queued", "dispatched"}
        )
        if demand > total_capacity:
            self.ensure_instance(
                request.function_name,
                request_seq=seq,
                request_priority=request.priority,
                round_id=f"request:{seq}",
            )

        self.schedule_round(
            f"request:{seq}",
            request.logical_clock_ms,
            budget_ms=self.config.round_budget_ms,
            global_limit=self.config.global_cold_start_limit,
        )
        self._dispatch_queued(request.logical_clock_ms)
        return self.calls[seq]

    def run_requests(self) -> list[dict[str, Any]]:
        for request in sorted(self.requests, key=lambda item: item.seq):
            self.process_request(request)
        return [self.calls[seq] for seq in sorted(self.calls)]

    def complete_until(self, clock_ms: int) -> None:
        self.advance_to(clock_ms)
        self.analyze_precision_sensitivity()

    def analyze_precision_sensitivity(self) -> list[PrecisionMarker]:
        by_function: dict[str, list[dict[str, Any]]] = {}
        for seq in sorted(self.calls):
            call = self.calls[seq]
            if call["status"] in {"dispatched", "completed"} and call.get("wait_ms") is not None:
                if int(call["wait_ms"]) >= 0:
                    by_function.setdefault(call["function_name"], []).append(call)

        for calls in by_function.values():
            calls.sort(key=lambda call: (int(call["seq"]),))
            for left, right in zip(calls, calls[1:]):
                pair = (int(left["seq"]), int(right["seq"]))
                if pair in self.precision_pairs:
                    continue
                order_depends = (
                    left.get("priority") == right.get("priority")
                    and left.get("instance_id") == right.get("instance_id")
                )
                marker = self.ledger.assess_wait_pair(
                    pair[0],
                    int(left["wait_ms"]),
                    pair[1],
                    int(right["wait_ms"]),
                    order_depends_on_difference=order_depends,
                )
                if marker.sensitive:
                    self.precision_pairs.add(pair)
                else:
                    self.precision_pairs.add(pair)
        return list(self.ledger.markers)  # type: ignore[return-value]

    def state_dict(self) -> dict[str, Any]:
        return {
            "config": asdict(self.config),
            "logical_clock_watermark_ms": self.logical_clock_watermark_ms,
            "processed_request_seq": self.processed_request_seq,
            "instance_sequences": self.instance_sequences,
            "instances": self.instances,
            "calls": {str(seq): call for seq, call in sorted(self.calls.items())},
            "queued_seqs": self.queued_seqs,
            "decisions": self.decisions,
            "budget_charges": self.budget_charges,
            "events": self.events,
            "precision_pairs": [list(pair) for pair in sorted(self.precision_pairs)],
            "ledger": self.ledger.to_dict(),
            "policy_pointer": self.policy_pointer,
            "peak_warm_concurrency": self.peak_warm_concurrency,
            "cold_start_watermark": self.cold_start_watermark(),
        }

    def cold_start_watermark(self) -> dict[str, Any]:
        ready_times = [
            int(instance["ready_at_ms"])
            for instance in self.instances.values()
            if instance.get("ready_at_ms") is not None
        ]
        failure_times = [
            int(instance["failure_at_ms"])
            for instance in self.instances.values()
            if instance.get("failure_at_ms") is not None
        ]
        return {
            "peak_warm_concurrency": self.peak_warm_concurrency,
            "max_scheduled_ready_at_ms": max(ready_times, default=0),
            "max_scheduled_failure_at_ms": max(failure_times, default=0),
            "started_charge_count": len(self.budget_charges),
            "committed_budget_charge_total_ms": sum(
                int(charge["amount_ms"]) for charge in self.budget_charges.values()
            ),
        }

    def canonical_state_bytes(self) -> bytes:
        return canonical_json(self.state_dict()).encode("utf-8")

    def load_state(self, state: dict[str, Any]) -> None:
        self.logical_clock_watermark_ms = int(state.get("logical_clock_watermark_ms", 0))
        self.processed_request_seq = int(state.get("processed_request_seq", 0))
        self.instance_sequences = dict(state.get("instance_sequences", {}))
        self.instances = dict(state.get("instances", {}))
        self.calls = {int(seq): call for seq, call in state.get("calls", {}).items()}
        self.queued_seqs = list(state.get("queued_seqs", []))
        self.decisions = list(state.get("decisions", []))
        self.budget_charges = dict(state.get("budget_charges", {}))
        self.events = list(state.get("events", []))
        self.new_events = []
        self.precision_pairs = {tuple(pair) for pair in state.get("precision_pairs", [])}
        self.peak_warm_concurrency = int(state.get("peak_warm_concurrency", 0))
        ledger_data = state.get("ledger")
        if ledger_data:
            self.ledger.entries = list(ledger_data.get("entries", []))
            self.ledger.markers = list(ledger_data.get("markers", []))
            self.ledger._error_bound = __import__("fractions").Fraction(
                ledger_data.get("cumulative_error_bound_ms", "0")
            )
        self.policy_pointer = state.get("policy_pointer", self.policy_pointer)

    def persist_step(
        self,
        step_id: str,
        step_order: int,
        clock_ms: int,
        result: dict[str, Any],
        *,
        input_digest: str | None = None,
        planned: dict[str, Any] | None = None,
    ) -> None:
        digest = input_digest or sha256_json(result)
        for charge in self.budget_charges.values():
            if not charge.get("step_id"):
                charge["step_id"] = step_id
        self.store.prepare_step(
            step_id, step_order, digest, planned or result, clock_ms
        )
        functions_rows = [
            {
                "name": spec.name,
                "spec": {k: v for k, v in asdict(spec).items() if k != "raw"},
                "state": {"priority": spec.priority, "container_spec": spec.container_spec},
            }
            for spec in self.functions.values()
        ]
        self.store.commit_snapshot(
            step_id=step_id,
            step_order=step_order,
            clock_watermark_ms=self.logical_clock_watermark_ms,
            processed_request_seq=self.processed_request_seq,
            engine_state=self.state_dict(),
            functions=functions_rows,
            instances=list(self.instances.values()),
            calls=list(self.calls.values()),
            events=self.new_events,
            ledger_entries=self.ledger.entries,
            budget_charges=list(self.budget_charges.values()),
            policy_pointer=self.policy_pointer,
            result=result,
        )
        self.new_events = []

    def recover(self, recovery_clock_ms: int = 0) -> RecoveryReport:
        report = self.store.recover(recovery_clock_ms=recovery_clock_ms)
        state = self.store.load_engine_state()
        if state is not None:
            self.load_state(state)
        self.recovery_report = report
        return report

    def dispatch_report(self) -> list[dict[str, Any]]:
        return [
            {
                "seq": seq,
                "function_name": call["function_name"],
                "status": call["status"],
                "instance_id": call.get("instance_id"),
                "arrival_clock_ms": call["arrival_clock_ms"],
                "dispatch_clock_ms": call.get("dispatch_clock_ms"),
                "wait_ms": call.get("wait_ms"),
                "completion_clock_ms": call.get("completion_clock_ms"),
            }
            for seq, call in sorted(self.calls.items())
        ]
