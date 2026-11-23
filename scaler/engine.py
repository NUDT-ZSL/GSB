from __future__ import annotations

import hashlib
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .backoff import deterministic_backoff
from .conversion import TargetConversion, convert_target
from .errors import BusinessError
from .launch_client import LaunchClient, OperationResult
from .models import ScalerSpec, canonical_json
from .packing import choose_placement, make_initial_ledger
from .state import (
    active_pending_count,
    get_spec,
    refresh_service_counters,
    release_replica_allocation,
    set_replica_phase,
    sorted_replica_list,
    validate_state,
)
from .storage import SQLiteStore

ROUND_HISTORY_LIMIT = 50
PLACEMENT_DECISION_LIMIT = 500


class Autoscaler:
    def __init__(
        self, store: SQLiteStore, spec: ScalerSpec, *, initialize: bool = True
    ) -> None:
        self.store = store
        self.spec = spec
        if initialize:
            self.store.initialize(spec)

    @classmethod
    def from_store(cls, store: SQLiteStore) -> Autoscaler:
        state, _ = store.load()
        return cls(store, get_spec(state), initialize=False)

    def run(
        self,
        *,
        rounds: int,
        start_clock_ms: int | None = None,
        services: list[str] | None = None,
        request_id: str | None = None,
        fault_round: int | None = None,
        fault_after_dispatch: int | None = None,
    ) -> dict[str, Any]:
        if rounds < 1:
            raise BusinessError(
                "E_INPUT_INVALID", "rounds must be at least 1", {"rounds": rounds}
            )
        selected = self._validate_service_filter(services)
        start = (
            self.recommended_start_clock()
            if start_clock_ms is None
            else int(start_clock_ms)
        )
        results = []
        clock = start
        for index in range(rounds):
            round_request_id = None
            if request_id:
                round_request_id = request_id if rounds == 1 else f"{request_id}:{index + 1}"
            result = self.run_round(
                clock_ms=clock,
                services=selected,
                request_id=round_request_id,
                fault_round=fault_round,
                fault_after_dispatch=fault_after_dispatch,
            )
            results.append(result)
            if result.get("idempotent"):
                clock = int(result.get("logical_clock_ms", clock)) + self.spec.config.clock_step_ms
            else:
                clock += self.spec.config.clock_step_ms
        state, _ = self.store.load()
        return {
            "ok": True,
            "rounds_completed": len(results),
            "rounds": results,
            "status": self.status_dict(state),
        }

    def recommended_start_clock(self) -> int:
        state, _ = self.store.load()
        if state.get("round_watermark", 0) > 0 or state.get("pending_rounds"):
            pending_clocks = [
                int(round_data["logical_clock_ms"])
                for round_data in state.get("pending_rounds", {}).values()
            ]
            latest = max([int(state.get("logical_clock_ms", 0)), *pending_clocks])
            return latest + self.spec.config.clock_step_ms
        return int(self.spec.trace.first_clock_ms())

    def run_round(
        self,
        *,
        clock_ms: int,
        services: list[str] | None = None,
        request_id: str | None = None,
        fault_round: int | None = None,
        fault_after_dispatch: int | None = None,
    ) -> dict[str, Any]:
        selected = self._validate_service_filter(services)
        prepared = self._prepare_round(
            clock_ms=clock_ms,
            services=selected,
            request_id=request_id,
        )
        if prepared.get("idempotent"):
            return prepared
        round_no = int(prepared["round_no"])
        pending = prepared["pending_round"]
        results: dict[str, OperationResult] = {}
        if pending["operations"]:
            results = self._dispatch(
                pending,
                fault_round=fault_round,
                fault_after_dispatch=fault_after_dispatch,
            )
        return self._finalize_round(
            round_no=round_no,
            results=results,
            recovery_decision=None,
        )

    def recover(self) -> dict[str, Any]:
        recovered_rounds: list[dict[str, Any]] = []
        while True:
            state, _ = self.store.load()
            pending_rounds = state.get("pending_rounds", {})
            if not pending_rounds:
                break
            round_no = min(int(key) for key in pending_rounds)
            pending = pending_rounds[str(round_no)]
            spec = get_spec(state)
            client = LaunchClient(spec.config.launch_target_url)
            decision: dict[str, Any] = {
                "round_no": round_no,
                "logical_clock_ms": int(pending["logical_clock_ms"]),
                "watermark_before_recovery": int(state.get("round_watermark", 0)),
                "pending_operation_count_before": len(pending["operations"]),
                "actions": [],
                "budget_recharged": False,
                "rule": (
                    "only operations persisted in the prepared round are reconciled; "
                    "already-reserved logical budget is never charged again"
                ),
            }
            results: dict[str, OperationResult] = {}
            for operation_id in sorted(pending["operations"]):
                operation = pending["operations"][operation_id]
                if operation.get("result") is not None:
                    continue
                if operation["type"] == "launch":
                    result = client.recover_operation(
                        operation_id=operation_id,
                        service=operation["service"],
                        replica_id=operation["replica_id"],
                        model_version=operation["model_version"],
                        cold_start_budget_ms=operation["cold_start_budget_ms"],
                        round_no=round_no,
                    )
                else:
                    if operation.get("phase") == "RECYCLED":
                        continue
                    result = client.health(operation["target_operation_id"])
                    result.operation_id = operation_id
                    result.source = "recovered_health"
                results[operation_id] = result
                decision["actions"].append(
                    {
                        "operation_id": operation_id,
                        "operation_type": operation["type"],
                        "recovery_action": result.source,
                        "ok": result.ok,
                        "code": result.code,
                    }
                )
            finalized = self._finalize_round(
                round_no=round_no,
                results=results,
                recovery_decision=decision,
            )
            recovered_rounds.append(finalized)
        state, _ = self.store.load()
        return {
            "ok": True,
            "recovered_rounds": recovered_rounds,
            "status": self.status_dict(state),
        }

    def _validate_service_filter(
        self, services: list[str] | None
    ) -> list[str] | None:
        if services is None:
            return None
        normalized = sorted(set(services))
        for name in normalized:
            if name not in self.spec.services:
                raise BusinessError(
                    "E_SERVICE_UNKNOWN",
                    f"service '{name}' is not declared",
                    {"service": name},
                )
        return normalized

    def _prepare_round(
        self,
        *,
        clock_ms: int,
        services: list[str] | None,
        request_id: str | None,
    ) -> dict[str, Any]:
        with self.store.process_lock():
            state, snapshot_id = self.store.load_unlocked()
            spec_changed = self._ensure_spec_locked(state)
            selected = services or sorted(get_spec(state).services)
            effective_clock, clock_adjustment = self._check_clock(
                state, clock_ms, get_spec(state)
            )
            request_digest = hashlib.sha256(
                canonical_json(
                    {
                        "spec_digest": get_spec(state).digest,
                        "clock_ms": int(clock_ms),
                        "services": selected,
                    }
                ).encode("utf-8")
            ).hexdigest()
            if request_id:
                existing = state["requests"].get(request_id)
                if existing:
                    if existing["digest"] != request_digest:
                        raise BusinessError(
                            "E_REQUEST_CONFLICT",
                            "idempotency key was reused with a different request",
                            {
                                "request_id": request_id,
                                "existing_digest": existing["digest"],
                                "request_digest": request_digest,
                            },
                        )
                    if existing["status"] != "completed":
                        raise BusinessError(
                            "E_REQUEST_IN_PROGRESS",
                            "the idempotent request has an unfinished round",
                            {
                                "request_id": request_id,
                                "round_no": existing["round_no"],
                            },
                        )
                    return {
                        "ok": True,
                        "idempotent": True,
                        "round_no": existing["round_no"],
                        "logical_clock_ms": existing["logical_clock_ms"],
                        "round": existing.get("summary"),
                    }

            events: list[dict[str, Any]] = []
            if spec_changed:
                events.append(
                    {
                        "kind": "spec.updated",
                        "round_no": None,
                        "service": None,
                        "data": {
                            "config_version": get_spec(state).config.config_version,
                            "spec_digest": get_spec(state).digest,
                        },
                    }
                )

            round_no = int(state["next_round_seq"])
            state["next_round_seq"] = round_no + 1
            lifecycle: list[dict[str, Any]] = self._recycle_finished_drains(
                state, round_no=round_no, clock_ms=effective_clock
            )
            conversions = self._convert_services(
                state,
                selected=selected,
                round_no=round_no,
                clock_ms=effective_clock,
            )
            for conversion in conversions:
                state["services"][conversion.service]["target_replicas"] = (
                    conversion.target_replicas
                )
                state["services"][conversion.service]["last_conversion"] = (
                    conversion.to_dict()
                )
            lifecycle.extend(
                self._reconcile_target_replica_counts(
                    state,
                    selected=selected,
                    round_no=round_no,
                    clock_ms=effective_clock,
                )
            )
            refresh_service_counters(state)

            round_data: dict[str, Any] = {
                "round_no": round_no,
                "logical_clock_ms": effective_clock,
                "requested_clock_ms": int(clock_ms),
                "clock_adjustment": clock_adjustment,
                "request_id": request_id,
                "config_version": get_spec(state).config.config_version,
                "spec_digest": get_spec(state).digest,
                "selected_services": selected,
                "status": "prepared",
                "conversions": [item.to_dict() for item in conversions],
                "targets": {
                    name: state["services"][name]["target_replicas"]
                    for name in selected
                },
                "budget": {
                    "total_ms": get_spec(state).config.round_budget_ms,
                    "reserved_ms": 0,
                    "remaining_ms": get_spec(state).config.round_budget_ms,
                    "reserved_by_service": {},
                    "exhausted": False,
                    "deferred": [],
                    "candidate_order": [],
                    "per_service_launch_limit": get_spec(
                        state
                    ).config.per_service_launch_limit,
                },
                "operations": {},
                "placements": [],
                "dispositions": [],
                "deferred_backoff": [],
                "lifecycle": lifecycle,
            }
            self._plan_launches(
                state,
                round_data=round_data,
                selected=selected,
                round_no=round_no,
                clock_ms=effective_clock,
            )
            self._plan_health_checks(
                state,
                round_data=round_data,
                selected=selected,
                round_no=round_no,
                clock_ms=effective_clock,
            )
            state["pending_rounds"][str(round_no)] = round_data
            if request_id:
                state["requests"][request_id] = {
                    "digest": request_digest,
                    "status": "in_progress",
                    "round_no": round_no,
                    "logical_clock_ms": effective_clock,
                    "summary": None,
                }
            validate_state(state)
            self.store.commit_unlocked(
                state,
                expected_snapshot_id=snapshot_id,
                reason=f"prepare_round_{round_no}",
                events=events
                + [
                    {
                        "kind": "round.prepared",
                        "round_no": round_no,
                        "service": None,
                        "data": {
                            "logical_clock_ms": effective_clock,
                            "operations": sorted(round_data["operations"]),
                            "budget": round_data["budget"],
                        },
                    }
                ],
            )
            return {
                "ok": True,
                "idempotent": False,
                "round_no": round_no,
                "logical_clock_ms": effective_clock,
                "pending_round": round_data,
            }

    def _ensure_spec_locked(self, state: dict[str, Any]) -> bool:
        current_version = int(state["config_version"])
        new_version = self.spec.config.config_version
        if new_version < current_version:
            raise BusinessError(
                "E_CONFIG_VERSION_REGRESSION",
                "configuration version would move backwards",
                {
                    "current_config_version": current_version,
                    "requested_config_version": new_version,
                },
            )
        if new_version == current_version:
            if state["spec_digest"] != self.spec.digest:
                raise BusinessError(
                    "E_CONFIG_VERSION_CONFLICT",
                    "configuration version was reused with different content",
                    {
                        "config_version": new_version,
                        "current_spec_digest": state["spec_digest"],
                        "requested_spec_digest": self.spec.digest,
                    },
                )
            return False
        if active_pending_count(state):
            raise BusinessError(
                "E_UNSAFE_SPEC_CHANGE",
                "cannot change the specification while a round is pending",
                {"pending_rounds": sorted(state["pending_rounds"])},
            )
        old_spec = get_spec(state)
        topology_changed = (
            {name: item.to_dict() for name, item in old_spec.services.items()}
            != {name: item.to_dict() for name, item in self.spec.services.items()}
            or {name: item.to_dict() for name, item in old_spec.nodes.items()}
            != {name: item.to_dict() for name, item in self.spec.nodes.items()}
        )
        active_replicas = [
            replica
            for replica in state["replicas"].values()
            if replica["phase"] != "RECYCLED"
        ]
        if topology_changed and active_replicas:
            raise BusinessError(
                "E_UNSAFE_SPEC_CHANGE",
                "service or node topology changed while replicas are active",
                {"active_replicas": len(active_replicas)},
            )
        state["spec"] = self.spec.to_dict()
        state["config_version"] = new_version
        state["spec_digest"] = self.spec.digest
        if topology_changed:
            state["nodes"] = make_initial_ledger(self.spec.nodes)
            state["replicas"] = {}
            state["services"] = {}
            state["next_replica_seq"] = {
                name: 1 for name in sorted(self.spec.services)
            }
            for name in sorted(self.spec.services):
                state["services"][name] = {
                    "target_replicas": self.spec.services[name].min_replicas,
                    "available_replicas": 0,
                    "cold_starting_replicas": 0,
                    "pending_replicas": 0,
                    "draining_replicas": 0,
                    "launch_failure_streak": 0,
                    "health_failure_streak": 0,
                    "last_conversion": None,
                    "last_round_outcome": None,
                }
        validate_state(state)
        return True

    def _check_clock(
        self, state: dict[str, Any], requested: int, spec: ScalerSpec
    ) -> tuple[int, dict[str, Any] | None]:
        if requested < 0:
            raise BusinessError(
                "E_INPUT_INVALID",
                "logical clock must be non-negative",
                {"logical_clock_ms": requested},
            )
        watermark = int(state.get("logical_clock_ms", 0))
        pending_clocks = [
            int(item["logical_clock_ms"])
            for item in state.get("pending_rounds", {}).values()
        ]
        if pending_clocks:
            watermark = max(watermark, *pending_clocks)
        tolerance = spec.config.clock_regression_tolerance_ms
        if requested + tolerance < watermark:
            raise BusinessError(
                "E_CLOCK_REGRESSION",
                "logical clock moved backwards beyond the configured tolerance",
                {
                    "requested_clock_ms": requested,
                    "watermark_ms": watermark,
                    "tolerance_ms": tolerance,
                    "rollback_ms": watermark - requested,
                    "reason": "CLOCK_REGRESSION_BEYOND_TOLERANCE",
                },
            )
        if requested < watermark:
            return watermark, {
                "requested_clock_ms": requested,
                "effective_clock_ms": watermark,
                "rollback_ms": watermark - requested,
                "tolerance_ms": tolerance,
                "reason": "CLOCK_REGRESSION_WITHIN_TOLERANCE_CLAMPED",
            }
        return requested, None

    def _recycle_finished_drains(
        self, state: dict[str, Any], *, round_no: int, clock_ms: int
    ) -> list[dict[str, Any]]:
        transitions: list[dict[str, Any]] = []
        for replica in sorted_replica_list(state):
            if replica["phase"] != "DRAINING":
                continue
            due = replica.get("drain_due_ms")
            if due is None or int(due) > clock_ms:
                continue
            release_replica_allocation(state, replica)
            replica["drain_due_ms"] = None
            replica["recycled_round"] = round_no
            set_replica_phase(replica, "RECYCLED", round_no=round_no, clock_ms=clock_ms)
            transitions.append(
                {
                    "replica_id": replica["replica_id"],
                    "from": "DRAINING",
                    "to": "RECYCLED",
                    "reason": "DRAIN_COMPLETE",
                    "clock_ms": clock_ms,
                }
            )
        return transitions

    def _convert_services(
        self,
        state: dict[str, Any],
        *,
        selected: list[str],
        round_no: int,
        clock_ms: int,
    ) -> list[TargetConversion]:
        spec = get_spec(state)
        conversions = []
        for name in selected:
            observation = spec.trace.observation_at(clock_ms, name)
            if observation is None:
                raise BusinessError(
                    "E_NO_OBSERVATION",
                    "no workload observation exists for the service at or before the logical clock",
                    {
                        "service": name,
                        "logical_clock_ms": clock_ms,
                        "reason": "OBSERVATION_NOT_AVAILABLE",
                    },
                )
            conversions.append(convert_target(spec.services[name], observation, clock_ms))
        return conversions

    def _reconcile_target_replica_counts(
        self,
        state: dict[str, Any],
        *,
        selected: list[str],
        round_no: int,
        clock_ms: int,
    ) -> list[dict[str, Any]]:
        spec = get_spec(state)
        transitions: list[dict[str, Any]] = []
        for service_name in selected:
            service_state = state["services"][service_name]
            target = int(service_state["target_replicas"])
            replicas = [
                replica
                for replica in state["replicas"].values()
                if replica["service"] == service_name
            ]
            replicas.sort(key=lambda item: int(item["ordinal"]))
            active = [
                replica
                for replica in replicas
                if replica["phase"] in {"PENDING", "COLD_STARTING", "READY"}
            ]
            if len(active) > target:
                removable = [
                    replica
                    for replica in reversed(replicas)
                    if replica["phase"] == "PENDING"
                ]
                removable.extend(
                    replica
                    for replica in reversed(replicas)
                    if replica["phase"] == "READY"
                )
                for replica in removable:
                    if len(active) <= target:
                        break
                    if replica not in active:
                        continue
                    if replica["phase"] == "PENDING":
                        replica["recycled_round"] = round_no
                        set_replica_phase(
                            replica,
                            "RECYCLED",
                            round_no=round_no,
                            clock_ms=clock_ms,
                        )
                        transitions.append(
                            {
                                "replica_id": replica["replica_id"],
                                "from": "PENDING",
                                "to": "RECYCLED",
                                "reason": "SCALE_DOWN_CANCELLED_PENDING",
                                "clock_ms": clock_ms,
                            }
                        )
                    else:
                        replica["drain_due_ms"] = (
                            clock_ms + spec.config.drain_duration_ms
                        )
                        set_replica_phase(
                            replica,
                            "DRAINING",
                            round_no=round_no,
                            clock_ms=clock_ms,
                        )
                        transitions.append(
                            {
                                "replica_id": replica["replica_id"],
                                "from": "READY",
                                "to": "DRAINING",
                                "reason": "SCALE_DOWN_TARGET_LOWER",
                                "drain_due_ms": replica["drain_due_ms"],
                                "clock_ms": clock_ms,
                            }
                        )
                    active.remove(replica)
            elif len(active) < target:
                for _ in range(target - len(active)):
                    replica = self._create_pending_replica(
                        state,
                        service_name=service_name,
                        round_no=round_no,
                        clock_ms=clock_ms,
                    )
                    active.append(replica)
                    transitions.append(
                        {
                            "replica_id": replica["replica_id"],
                            "from": None,
                            "to": "PENDING",
                            "reason": "TARGET_INCREASE",
                            "clock_ms": clock_ms,
                        }
                    )
        return transitions

    def _create_pending_replica(
        self,
        state: dict[str, Any],
        *,
        service_name: str,
        round_no: int,
        clock_ms: int,
    ) -> dict[str, Any]:
        spec = get_spec(state)
        ordinal = int(state["next_replica_seq"][service_name])
        state["next_replica_seq"][service_name] = ordinal + 1
        replica_id = f"{service_name}-{ordinal:06d}"
        service = spec.services[service_name]
        replica = {
            "replica_id": replica_id,
            "service": service_name,
            "model_version": service.model_version,
            "ordinal": ordinal,
            "phase": "PENDING",
            "node": None,
            "gpu_index": None,
            "memory_mib": service.replica_memory_mib,
            "compute_units": service.replica_compute_units,
            "cold_start_budget_ms": service.cold_start_ms,
            "launch_failure_streak": 0,
            "health_failure_streak": 0,
            "backoff_until_ms": 0,
            "backoff_evidence": {},
            "last_failure_kind": None,
            "last_error_code": None,
            "last_launch_result": None,
            "last_health_result": None,
            "launch_operation_id": None,
            "ready_clock_ms": None,
            "drain_due_ms": None,
            "created_round": round_no,
            "phase_round": round_no,
            "phase_clock_ms": clock_ms,
            "recycled_round": None,
        }
        state["replicas"][replica_id] = replica
        return replica

    def _plan_launches(
        self,
        state: dict[str, Any],
        *,
        round_data: dict[str, Any],
        selected: list[str],
        round_no: int,
        clock_ms: int,
    ) -> None:
        spec = get_spec(state)
        candidates = []
        for replica in state["replicas"].values():
            if replica["service"] not in selected or replica["phase"] != "PENDING":
                continue
            due = int(replica.get("backoff_until_ms") or 0)
            if due > clock_ms:
                round_data["deferred_backoff"].append(
                    {
                        "replica_id": replica["replica_id"],
                        "service": replica["service"],
                        "retry_at_ms": due,
                        "remaining_ms": due - clock_ms,
                        "reason": "BACKOFF_NOT_DUE",
                        "failure_streak": replica["launch_failure_streak"],
                    }
                )
                continue
            service = spec.services[replica["service"]]
            candidates.append((service, replica))
        candidates.sort(
            key=lambda item: (
                -item[0].priority,
                item[0].name,
                int(item[1]["ordinal"]),
            )
        )
        round_data["budget"]["candidate_order"] = [
            {
                "replica_id": replica["replica_id"],
                "service": service.name,
                "priority": service.priority,
                "stable_order": index,
            }
            for index, (service, replica) in enumerate(candidates)
        ]
        remaining = int(round_data["budget"]["total_ms"])
        reserved_by_service = round_data["budget"]["reserved_by_service"]

        for index, (service, replica) in enumerate(candidates):
            cost = service.cold_start_ms
            if remaining < cost:
                round_data["budget"]["exhausted"] = True
                round_data["budget"]["first_exhausted_replica"] = replica["replica_id"]
                for deferred_service, deferred_replica in candidates[index:]:
                    round_data["budget"]["deferred"].append(
                        {
                            "replica_id": deferred_replica["replica_id"],
                            "service": deferred_service.name,
                            "priority": deferred_service.priority,
                            "required_ms": deferred_service.cold_start_ms,
                            "remaining_ms": remaining,
                            "reason": "ROUND_BUDGET_EXHAUSTED",
                            "classification": "ACTIVELY_ABANDONED",
                        }
                    )
                    round_data["dispositions"].append(
                        {
                            "replica_id": deferred_replica["replica_id"],
                            "service": deferred_service.name,
                            "classification": "ACTIVELY_ABANDONED",
                            "reason_code": "ROUND_BUDGET_EXHAUSTED",
                            "detail": (
                                "not started because the round logical time budget "
                                "was exhausted; no request was sent"
                            ),
                        }
                    )
                break

            placement_key = f"{round_no}:{replica['replica_id']}"
            prior_decision = state["placement_decisions"].get(placement_key)
            prior_placement = (
                prior_decision.get("placement")
                if isinstance(prior_decision, dict)
                else None
            )
            placement = choose_placement(
                service=service,
                replica_id=replica["replica_id"],
                node_specs=spec.nodes,
                node_ledger=state["nodes"],
                existing=prior_placement,
            )
            state["placement_decisions"][placement_key] = placement.to_dict()
            round_data["placements"].append(
                {"round_no": round_no, **placement.to_dict()}
            )
            if placement.placement is None:
                round_data["dispositions"].append(
                    {
                        "replica_id": replica["replica_id"],
                        "service": service.name,
                        "classification": "WITHIN_BUDGET_NOT_STARTED",
                        "reason_code": placement.first_constraint
                        or "NO_FEASIBLE_NODE",
                        "detail": "dual memory/concurrency placement rejected atomically",
                        "eliminated": placement.eliminated,
                    }
                )
                continue

            remaining -= cost
            reserved_by_service[service.name] = (
                int(reserved_by_service.get(service.name, 0)) + cost
            )
            operation_id = f"launch-r{round_no:06d}-{replica['replica_id']}"
            placement_value = placement.placement
            replica["node"] = placement_value.node
            replica["gpu_index"] = placement_value.gpu_index
            replica["launch_operation_id"] = operation_id
            replica["last_launch_result"] = None
            set_replica_phase(
                replica, "COLD_STARTING", round_no=round_no, clock_ms=clock_ms
            )
            round_data["operations"][operation_id] = {
                "operation_id": operation_id,
                "type": "launch",
                "service": service.name,
                "replica_id": replica["replica_id"],
                "model_version": service.model_version,
                "cold_start_budget_ms": service.cold_start_ms,
                "reserved_ms": cost,
                "status": "RESERVED",
                "result": None,
            }
        round_data["budget"]["reserved_ms"] = (
            int(round_data["budget"]["total_ms"]) - remaining
        )
        round_data["budget"]["remaining_ms"] = remaining
        self._trim_placement_decisions(state)

    def _trim_placement_decisions(self, state: dict[str, Any]) -> None:
        decisions = state["placement_decisions"]
        if len(decisions) <= PLACEMENT_DECISION_LIMIT:
            return
        keep = sorted(decisions)[-PLACEMENT_DECISION_LIMIT:]
        state["placement_decisions"] = {key: decisions[key] for key in keep}

    def _plan_health_checks(
        self,
        state: dict[str, Any],
        *,
        round_data: dict[str, Any],
        selected: list[str],
        round_no: int,
        clock_ms: int,
    ) -> None:
        spec = get_spec(state)
        if not spec.config.health_probe_enabled:
            return
        for replica in sorted_replica_list(state):
            if replica["service"] not in selected or replica["phase"] != "READY":
                continue
            target_operation_id = replica.get("launch_operation_id")
            if not target_operation_id:
                continue
            operation_id = f"health-r{round_no:06d}-{replica['replica_id']}"
            round_data["operations"][operation_id] = {
                "operation_id": operation_id,
                "type": "health",
                "target_operation_id": target_operation_id,
                "service": replica["service"],
                "replica_id": replica["replica_id"],
                "reserved_ms": 0,
                "status": "RESERVED",
                "result": None,
                "phase": replica["phase"],
            }

    def _dispatch(
        self,
        pending: dict[str, Any],
        *,
        fault_round: int | None,
        fault_after_dispatch: int | None,
    ) -> dict[str, OperationResult]:
        state, _ = self.store.load()
        spec = get_spec(state)
        client = LaunchClient(spec.config.launch_target_url)
        operations = list(pending["operations"].values())
        operations.sort(key=lambda item: (item["service"], item["type"], item["operation_id"]))
        results: dict[str, OperationResult] = {}
        results_lock = threading.Lock()
        dispatched_count = 0
        should_fault = (
            fault_round is not None
            and int(pending["round_no"]) == int(fault_round)
            and fault_after_dispatch is not None
            and fault_after_dispatch > 0
        )

        def execute(operation: dict[str, Any]) -> tuple[str, OperationResult]:
            nonlocal dispatched_count
            try:
                if operation["type"] == "launch":
                    result = client.launch(
                        operation_id=operation["operation_id"],
                        service=operation["service"],
                        replica_id=operation["replica_id"],
                        model_version=operation["model_version"],
                        cold_start_budget_ms=operation["cold_start_budget_ms"],
                        round_no=int(pending["round_no"]),
                    )
                else:
                    result = client.health(operation["target_operation_id"])
                    result.operation_id = operation["operation_id"]
            except BusinessError as exc:
                result = OperationResult(
                    operation_id=operation["operation_id"],
                    ok=False,
                    code=exc.code,
                    message=exc.message,
                    source="exception",
                )
            except Exception as exc:  # defensive classification
                result = OperationResult(
                    operation_id=operation["operation_id"],
                    ok=False,
                    code="E_LAUNCH_HTTP_ERROR",
                    message=f"unhandled launch client error: {exc}",
                    source="exception",
                )
            with results_lock:
                dispatched_count += 1
                if should_fault and dispatched_count == fault_after_dispatch:
                    force_terminate()
            return operation["operation_id"], result

        groups: dict[str, list[dict[str, Any]]] = {}
        for operation in operations:
            groups.setdefault(operation["service"], []).append(operation)

        def run_group(group_operations: list[dict[str, Any]]) -> None:
            worker_count = max(1, min(len(group_operations), spec.config.per_service_launch_limit))
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix=f"launch-{group_operations[0]['service']}",
            ) as executor:
                futures = [executor.submit(execute, operation) for operation in group_operations]
                for future in as_completed(futures):
                    operation_id, result = future.result()
                    with results_lock:
                        results[operation_id] = result

        group_threads = [
            threading.Thread(
                target=run_group,
                args=(groups[service],),
                name=f"service-group-{service}",
            )
            for service in sorted(groups)
        ]
        for thread in group_threads:
            thread.start()
        for thread in group_threads:
            thread.join()
        return results

    def _finalize_round(
        self,
        *,
        round_no: int,
        results: dict[str, OperationResult],
        recovery_decision: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with self.store.process_lock():
            state, snapshot_id = self.store.load_unlocked()
            pending = state["pending_rounds"].get(str(round_no))
            if pending is None:
                existing = next(
                    (
                        item
                        for item in state.get("round_history", [])
                        if int(item["round_no"]) == round_no
                    ),
                    None,
                )
                if existing is not None:
                    return {
                        "ok": True,
                        "idempotent": True,
                        "round_no": round_no,
                        "round": existing,
                    }
                raise BusinessError(
                    "E_STORAGE_ERROR",
                    "pending round disappeared before finalization",
                    {"round_no": round_no},
                )
            clock_ms = int(pending["logical_clock_ms"])
            missing = []
            for operation_id in sorted(pending["operations"]):
                operation = pending["operations"][operation_id]
                if operation.get("result") is None and operation_id not in results:
                    missing.append(operation_id)
            if missing:
                raise BusinessError(
                    "E_STORAGE_ERROR",
                    "cannot finalize a round before every started operation completes",
                    {"round_no": round_no, "missing_operation_ids": missing},
                )

            for operation_id in sorted(pending["operations"]):
                operation = pending["operations"][operation_id]
                result = results.get(operation_id)
                if result is None:
                    result_data = operation.get("result") or {}
                    result = OperationResult(
                        operation_id=operation_id,
                        ok=bool(result_data.get("ok")),
                        code=result_data.get("code"),
                        message=str(result_data.get("message", "")),
                        duration_ms=result_data.get("duration_ms"),
                        response=result_data.get("response", {}),
                    )
                stable_result = _stable_operation_result(result)
                operation["result"] = stable_result
                operation["status"] = "DONE"
                if operation["type"] == "launch":
                    self._apply_launch_result(
                        state,
                        pending,
                        operation,
                        result,
                        round_no=round_no,
                        clock_ms=clock_ms,
                    )
                else:
                    self._apply_health_result(
                        state,
                        pending,
                        operation,
                        result,
                        round_no=round_no,
                        clock_ms=clock_ms,
                    )

            refresh_service_counters(state)
            for service_name in pending["selected_services"]:
                service_replicas = [
                    replica
                    for replica in state["replicas"].values()
                    if replica["service"] == service_name
                ]
                service_state = state["services"][service_name]
                service_state["launch_failure_streak"] = max(
                    [
                        int(replica["launch_failure_streak"])
                        for replica in service_replicas
                    ]
                    or [0]
                )
                service_state["health_failure_streak"] = max(
                    [
                        int(replica["health_failure_streak"])
                        for replica in service_replicas
                    ]
                    or [0]
                )
                service_state["last_round_outcome"] = self._service_outcome(
                    pending, service_name
                )

            pending["status"] = "completed"
            pending["completed_clock_ms"] = clock_ms
            pending["phase_counts"] = self._phase_counts(state)
            pending["error_codes"] = sorted(
                {
                    item["reason_code"]
                    for item in pending["dispositions"]
                    if item.get("reason_code")
                }
            )
            pending["failed_outcome_counts"] = {
                category: sum(
                    1
                    for item in pending["dispositions"]
                    if item["classification"] == category
                )
                for category in (
                    "ACTIVELY_ABANDONED",
                    "WITHIN_BUDGET_NOT_STARTED",
                    "LAUNCH_FAILED",
                )
            }
            pending["backoff_plan"] = self._build_backoff_plan(
                state, pending, clock_ms
            )
            pending["ledger_after"] = {
                name: {
                    "gpu_remaining_mib": list(node["gpu_remaining_mib"]),
                    "used_instances": int(node["used_instances"]),
                }
                for name, node in sorted(state["nodes"].items())
            }
            state["pending_rounds"].pop(str(round_no), None)
            state["logical_clock_ms"] = max(
                int(state.get("logical_clock_ms", 0)), clock_ms
            )
            state["round_watermark"] = max(
                int(state.get("round_watermark", 0)), round_no
            )
            state["state_revision"] = int(state.get("state_revision", 0)) + 1
            state["round_history"].append(pending)
            state["round_history"].sort(key=lambda item: int(item["round_no"]))
            if len(state["round_history"]) > ROUND_HISTORY_LIMIT:
                state["round_history"] = state["round_history"][
                    -ROUND_HISTORY_LIMIT:
                ]
            request_id = pending.get("request_id")
            if request_id and request_id in state["requests"]:
                state["requests"][request_id]["status"] = "completed"
                state["requests"][request_id]["summary"] = pending
                state["requests"][request_id]["logical_clock_ms"] = clock_ms
            validate_state(state)
            events = [
                {
                    "kind": "round.completed",
                    "round_no": round_no,
                    "service": None,
                    "data": pending,
                }
            ]
            if recovery_decision is not None:
                events.append(
                    {
                        "kind": "recovery.applied",
                        "round_no": round_no,
                        "service": None,
                        "data": recovery_decision,
                    }
                )
            self.store.commit_unlocked(
                state,
                expected_snapshot_id=snapshot_id,
                reason=f"finalize_round_{round_no}",
                events=events,
            )
            if recovery_decision is not None:
                self.store.record_recovery_audit(round_no, recovery_decision)
            return {
                "ok": True,
                "idempotent": False,
                "round_no": round_no,
                "logical_clock_ms": clock_ms,
                "round": pending,
            }

    def _apply_launch_result(
        self,
        state: dict[str, Any],
        pending: dict[str, Any],
        operation: dict[str, Any],
        result: OperationResult,
        *,
        round_no: int,
        clock_ms: int,
    ) -> None:
        replica = state["replicas"][operation["replica_id"]]
        if replica["phase"] != "COLD_STARTING":
            raise BusinessError(
                "E_STORAGE_ERROR",
                "launch result cannot be applied to a replica outside COLD_STARTING",
                {
                    "replica_id": replica["replica_id"],
                    "phase": replica["phase"],
                    "round_no": round_no,
                },
            )
        if result.ok:
            set_replica_phase(replica, "READY", round_no=round_no, clock_ms=clock_ms)
            replica["ready_clock_ms"] = clock_ms
            replica["launch_failure_streak"] = 0
            replica["last_failure_kind"] = None
            replica["last_error_code"] = None
            replica["backoff_until_ms"] = 0
            replica["backoff_evidence"] = {}
            replica["last_launch_result"] = _stable_operation_result(result)
            pending["dispositions"].append(
                {
                    "replica_id": replica["replica_id"],
                    "service": replica["service"],
                    "classification": "STARTED_SUCCEEDED",
                    "reason_code": None,
                    "detail": "cold start completed and replica is now READY",
                    "duration_ms": result.duration_ms,
                }
            )
            return

        release_replica_allocation(state, replica)
        replica["launch_failure_streak"] = (
            int(replica["launch_failure_streak"]) + 1
        )
        backoff = deterministic_backoff(
            service=replica["service"],
            replica_id=replica["replica_id"],
            failure_kind="LAUNCH",
            streak=int(replica["launch_failure_streak"]),
            now_ms=clock_ms,
            config=get_spec(state).config,
        )
        replica["backoff_until_ms"] = backoff.retry_at_ms
        replica["backoff_evidence"] = backoff.evidence
        replica["last_failure_kind"] = "LAUNCH"
        replica["last_error_code"] = result.code or "E_LAUNCH_HTTP_ERROR"
        replica["last_launch_result"] = _stable_operation_result(result)
        replica["drain_due_ms"] = None
        set_replica_phase(replica, "PENDING", round_no=round_no, clock_ms=clock_ms)
        pending["dispositions"].append(
            {
                "replica_id": replica["replica_id"],
                "service": replica["service"],
                "classification": "LAUNCH_FAILED",
                "reason_code": replica["last_error_code"],
                "detail": result.message,
                "duration_ms": result.duration_ms,
                "retry_at_ms": backoff.retry_at_ms,
                "failure_streak": replica["launch_failure_streak"],
            }
        )

    def _apply_health_result(
        self,
        state: dict[str, Any],
        pending: dict[str, Any],
        operation: dict[str, Any],
        result: OperationResult,
        *,
        round_no: int,
        clock_ms: int,
    ) -> None:
        replica = state["replicas"].get(operation["replica_id"])
        if replica is None:
            return
        if replica["phase"] != "READY":
            return
        if result.ok:
            replica["health_failure_streak"] = 0
            replica["last_health_result"] = _stable_operation_result(result)
            pending["dispositions"].append(
                {
                    "replica_id": replica["replica_id"],
                    "service": replica["service"],
                    "classification": "HEALTH_PROBE_SUCCEEDED",
                    "reason_code": None,
                    "detail": "health probe succeeded",
                }
            )
            return
        replica["health_failure_streak"] = (
            int(replica["health_failure_streak"]) + 1
        )
        replica["last_health_result"] = _stable_operation_result(result)
        replica["last_error_code"] = result.code or "E_HEALTH_PROBE_FAILED"
        threshold = get_spec(state).config.failure_threshold
        classification = "HEALTH_PROBE_FAILED"
        if int(replica["health_failure_streak"]) >= threshold:
            replica["last_failure_kind"] = "HEALTH"
            replica["drain_due_ms"] = (
                clock_ms + get_spec(state).config.drain_duration_ms
            )
            set_replica_phase(replica, "DRAINING", round_no=round_no, clock_ms=clock_ms)
            classification = "HEALTH_FAILURE_DRAINING"
        pending["dispositions"].append(
            {
                "replica_id": replica["replica_id"],
                "service": replica["service"],
                "classification": classification,
                "reason_code": replica["last_error_code"],
                "detail": result.message,
                "failure_streak": replica["health_failure_streak"],
                "failure_threshold": threshold,
                "drain_due_ms": replica["drain_due_ms"],
            }
        )

    def _service_outcome(
        self, pending: dict[str, Any], service_name: str
    ) -> dict[str, Any]:
        dispositions = [
            item
            for item in pending["dispositions"]
            if item["service"] == service_name
        ]
        counts: dict[str, int] = {}
        for item in dispositions:
            counts[item["classification"]] = (
                counts.get(item["classification"], 0) + 1
            )
        return {
            "round_no": int(pending["round_no"]),
            "target_replicas": int(pending["targets"].get(service_name, 0)),
            "classification_counts": dict(sorted(counts.items())),
            "budget_reserved_ms": int(
                pending["budget"]["reserved_by_service"].get(service_name, 0)
            ),
        }

    def _phase_counts(self, state: dict[str, Any]) -> dict[str, int]:
        counts = {phase: 0 for phase in ["PENDING", "COLD_STARTING", "READY", "DRAINING", "RECYCLED"]}
        for replica in state["replicas"].values():
            counts[replica["phase"]] = counts.get(replica["phase"], 0) + 1
        return counts

    def _build_backoff_plan(
        self, state: dict[str, Any], pending: dict[str, Any], clock_ms: int
    ) -> list[dict[str, Any]]:
        plan = []
        for replica in sorted_replica_list(state):
            if replica["service"] not in pending["selected_services"]:
                continue
            if replica["phase"] == "PENDING" and int(replica["backoff_until_ms"]) > clock_ms:
                plan.append(
                    {
                        "replica_id": replica["replica_id"],
                        "service": replica["service"],
                        "retry_at_ms": int(replica["backoff_until_ms"]),
                        "remaining_ms": int(replica["backoff_until_ms"]) - clock_ms,
                        "failure_streak": int(replica["launch_failure_streak"]),
                        "failure_kind": replica["last_failure_kind"],
                        "evidence": replica["backoff_evidence"],
                    }
                )
        return plan

    def status_dict(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        if state is None:
            state, _ = self.store.load()
        spec = get_spec(state)
        services = []
        for name in sorted(spec.services):
            item = dict(state["services"][name])
            item["spec"] = spec.services[name].to_dict()
            services.append(item)
        nodes = []
        for name in sorted(spec.nodes):
            ledger = state["nodes"][name]
            node = spec.nodes[name]
            nodes.append(
                {
                    "name": name,
                    "topology_domain": node.topology_domain,
                    "gpu_count": node.gpu_count,
                    "gpu_capacity_mib": node.gpu_memory_mib,
                    "gpu_remaining_mib": list(ledger["gpu_remaining_mib"]),
                    "gpu_used_mib": [
                        node.gpu_memory_mib - value
                        for value in ledger["gpu_remaining_mib"]
                    ],
                    "used_instances": int(ledger["used_instances"]),
                    "max_instances": node.max_instances,
                }
            )
        replicas = []
        for replica in sorted_replica_list(state):
            replicas.append(
                {
                    "replica_id": replica["replica_id"],
                    "service": replica["service"],
                    "model_version": replica["model_version"],
                    "phase": replica["phase"],
                    "node": replica["node"],
                    "gpu_index": replica["gpu_index"],
                    "memory_mib": replica["memory_mib"],
                    "launch_failure_streak": replica["launch_failure_streak"],
                    "health_failure_streak": replica["health_failure_streak"],
                    "backoff_until_ms": replica["backoff_until_ms"],
                    "last_error_code": replica["last_error_code"],
                    "drain_due_ms": replica["drain_due_ms"],
                    "ready_clock_ms": replica["ready_clock_ms"],
                    "phase_round": replica["phase_round"],
                }
            )
        pending_rounds = [
            {
                "round_no": int(item["round_no"]),
                "logical_clock_ms": int(item["logical_clock_ms"]),
                "status": item["status"],
                "operation_count": len(item["operations"]),
                "budget": item["budget"],
            }
            for _, item in sorted(
                state.get("pending_rounds", {}).items(),
                key=lambda pair: int(pair[0]),
            )
        ]
        return {
            "ok": True,
            "schema_version": state["schema_version"],
            "config_version": state["config_version"],
            "spec_digest": state["spec_digest"],
            "logical_clock_ms": state["logical_clock_ms"],
            "round_watermark": state["round_watermark"],
            "state_revision": state["state_revision"],
            "services": services,
            "nodes": nodes,
            "replicas": replicas,
            "pending_rounds": pending_rounds,
            "recent_rounds": state.get("round_history", [])[-10:],
            "phase_counts": self._phase_counts(state),
            "ledger_conserved": True,
        }


def _stable_operation_result(result: OperationResult) -> dict[str, Any]:
    return {
        "ok": bool(result.ok),
        "code": result.code,
        "message": result.message,
        "duration_ms": result.duration_ms,
        "response": result.response,
    }


def force_terminate() -> None:
    if os.name == "nt":  # pragma: no cover - Windows drill fallback
        os._exit(137)
    os.kill(os.getpid(), signal.SIGKILL)
