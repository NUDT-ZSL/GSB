from __future__ import annotations

import concurrent.futures
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .demand import choose_target_replicas
from .errors import BusinessError, ErrorCode
from .launcher import Launcher, LifecycleRequest
from .models import InputBundle, ServiceSpec, canonical_json, digest_value
from .packing import PackingState, place_replica
from .policy import Policy
from .store import Store

PHASES = {"pending", "cold_starting", "ready", "draining", "reclaimed"}


@dataclass(frozen=True)
class BackoffDecision:
    failure_count: int
    exponent: int
    nominal_ticks: int
    jitter_fraction_numerator: int
    jitter_ticks: int
    delay_ticks: int
    cap_ticks: int
    next_retry_tick: int
    stable_seed: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_count": self.failure_count,
            "exponent": self.exponent,
            "nominal_ticks": self.nominal_ticks,
            "jitter_fraction_numerator": self.jitter_fraction_numerator,
            "jitter_ticks": self.jitter_ticks,
            "delay_ticks": self.delay_ticks,
            "cap_ticks": self.cap_ticks,
            "next_retry_tick": self.next_retry_tick,
            "stable_seed": self.stable_seed,
            "rule": (
                "min(cap, base*2^(failure_count-1) + "
                "floor(stable_fraction*(jitter_ticks+1)))"
            ),
        }


def plan_backoff(
    *,
    replica_id: str,
    failure_count: int,
    tick: int,
    policy: Policy,
) -> BackoffDecision:
    exponent = min(max(0, failure_count - 1), 30)
    nominal = min(
        policy.max_backoff_ticks,
        policy.base_backoff_ticks * (2**exponent),
    )
    seed_material = f"{replica_id}|{failure_count}|{tick}|{policy.digest}"
    digest = hashlib.sha256(seed_material.encode("utf-8")).digest()
    numerator = int.from_bytes(digest[:8], "big")
    jitter_span = policy.jitter_ticks + 1
    jitter = (numerator * jitter_span) // (2**64)
    delay = min(policy.max_backoff_ticks, nominal + jitter)
    return BackoffDecision(
        failure_count=failure_count,
        exponent=exponent,
        nominal_ticks=nominal,
        jitter_fraction_numerator=numerator,
        jitter_ticks=jitter,
        delay_ticks=delay,
        cap_ticks=policy.max_backoff_ticks,
        next_retry_tick=tick + delay,
        stable_seed=seed_material,
    )


@dataclass
class _LaunchCandidate:
    service: ServiceSpec
    replica: dict[str, Any] | None
    replica_id: str
    ordinal: int
    priority: int
    retry: bool
    priority_key: tuple[Any, ...]
    order_key: str
    backoff: BackoffDecision | None


class Controller:
    def __init__(
        self,
        *,
        bundle: InputBundle,
        policy: Policy,
        store: Store,
        launcher: Launcher,
    ) -> None:
        self.bundle = bundle
        self.policy = policy
        self.store = store
        self.launcher = launcher
        self.services = bundle.service_map()

    def _selected_services(self, service_filter: Sequence[str] | None) -> list[ServiceSpec]:
        if not service_filter:
            return sorted(self.bundle.services, key=lambda service: service.name)
        unknown = sorted(set(service_filter) - set(self.services))
        if unknown:
            raise BusinessError(
                ErrorCode.UNKNOWN_SERVICE,
                "round references unknown services",
                details={"services": unknown},
            )
        return [self.services[name] for name in sorted(set(service_filter))]

    def _request_digest(
        self,
        *,
        round_id: str,
        tick: int,
        budget: int,
        service_filter: Sequence[str] | None,
    ) -> str:
        return digest_value(
            {
                "round_id": round_id,
                "tick": tick,
                "budget": budget,
                "service_filter": sorted(service_filter or []),
                "sample_digest": self.bundle.sample_digest,
                "policy_digest": self.policy.digest,
            }
        )

    def run_round(
        self,
        *,
        round_id: str,
        tick: int,
        service_filter: Sequence[str] | None = None,
        budget_ticks: int | None = None,
    ) -> dict[str, Any]:
        budget = (
            self.policy.logical_time_budget_ticks
            if budget_ticks is None
            else budget_ticks
        )
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise BusinessError(
                ErrorCode.INPUT_SCHEMA,
                "round budget must be a non-negative integer",
            )
        if isinstance(tick, bool) or not isinstance(tick, int) or tick < 0:
            raise BusinessError(
                ErrorCode.INPUT_SCHEMA,
                "logical tick must be a non-negative integer",
            )
        selected = self._selected_services(service_filter)
        service_names = [service.name for service in selected]
        request_digest = self._request_digest(
            round_id=round_id,
            tick=tick,
            budget=budget,
            service_filter=service_names if service_filter else None,
        )

        lock_scopes = [f"round:{round_id}"]
        lock_scopes.extend(f"service:{name}" for name in service_names)
        with self.store.locks(lock_scopes):
            existing = self.store.get_round(round_id)
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise BusinessError(
                        ErrorCode.ROUND_CONFLICT,
                        "round id was already used with different inputs",
                        details={
                            "round_id": round_id,
                            "existing_digest": existing["request_digest"],
                            "requested_digest": request_digest,
                        },
                    )
                if existing["status"] == "committed":
                    return json.loads(existing["result_json"])
            else:
                self._plan_round(
                    round_id=round_id,
                    tick=tick,
                    budget=budget,
                    request_digest=request_digest,
                    selected=selected,
                    service_filter=service_names if service_filter else None,
                )

            self._execute_unfinished_actions(round_id=round_id, tick=tick)
            result = self._build_result(round_id=round_id, tick=tick)
            committed = self.store.commit_round(
                round_id=round_id,
                tick=tick,
                result=result,
            )
            return committed

    def _plan_round(
        self,
        *,
        round_id: str,
        tick: int,
        budget: int,
        request_digest: str,
        selected: Sequence[ServiceSpec],
        service_filter: list[str] | None,
    ) -> None:
        with self.store.tx() as connection:
            meta = self.store.get_state_meta(connection)
            if tick < int(meta["watermark_tick"]):
                raise BusinessError(
                    ErrorCode.CLOCK_REGRESSION,
                    "logical clock moved backwards",
                    details={
                        "tick": tick,
                        "watermark_tick": int(meta["watermark_tick"]),
                    },
                )

            connection.execute(
                """
                INSERT INTO rounds(
                    round_id, tick, service_filter, input_digest,
                    policy_digest, request_digest, status,
                    budget_limit_ticks, budget_used_ticks,
                    version_observed
                ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, 0, ?)
                """,
                (
                    round_id,
                    tick,
                    canonical_json(service_filter) if service_filter else None,
                    self.bundle.sample_digest,
                    self.policy.digest,
                    request_digest,
                    budget,
                    meta["current_snapshot_version"],
                ),
            )
            connection.execute(
                """
                UPDATE state_meta
                SET watermark_tick = MAX(watermark_tick, ?)
                WHERE id = 1
                """,
                (tick,),
            )
            self.store.promote_ready_replicas(
                connection,
                tick=tick,
                round_id=round_id,
            )

            packing_state = PackingState.from_occupancies(
                self.bundle.node_pool,
                self.store.list_occupancies(connection),
            )
            open_action_replicas = {
                str(row["replica_id"])
                for row in connection.execute(
                    """
                    SELECT replica_id FROM actions
                    WHERE status IN ('reserved', 'in_flight')
                    """
                ).fetchall()
            }

            all_candidates: list[_LaunchCandidate] = []
            drain_actions: list[dict[str, Any]] = []
            budget_remaining = budget
            per_service_selected: dict[str, int] = {
                service.name: 0 for service in selected
            }

            for service in selected:
                window = self.bundle.trace.window_by_tick.get(tick)
                if window is None:
                    raise BusinessError(
                        ErrorCode.TRACE_WINDOW_MISSING,
                        "trace has no observation window for this logical tick",
                        details={"tick": tick, "service": service.name},
                    )
                demand = choose_target_replicas(service, window)
                replicas = self.store.list_replicas(connection, service.name)
                counts = self._phase_counts(replicas)
                projected = (
                    counts["ready"]
                    + counts["cold_starting"]
                    + counts["pending"]
                )
                drain_count = max(0, counts["ready"] - demand.target_replicas)
                launch_deficit = max(0, demand.target_replicas - projected)
                below_min = projected < service.min_replicas

                ready_replicas = sorted(
                    (row for row in replicas if row["phase"] == "ready"),
                    key=lambda row: int(row["ordinal"]),
                    reverse=True,
                )
                for replica in ready_replicas[:drain_count]:
                    drain_actions.append(
                        self._create_drain_action(
                            connection=connection,
                            round_id=round_id,
                            tick=tick,
                            service=service,
                            replica=replica,
                            priority=0,
                            order_key=f"0|{service.name}|drain|{int(replica['ordinal']):08d}",
                        )
                    )

                due_retries: list[dict[str, Any]] = []
                deferred: list[dict[str, Any]] = []
                for replica in sorted(
                    (row for row in replicas if row["phase"] == "draining"),
                    key=lambda row: int(row["ordinal"]),
                ):
                    if replica["replica_id"] in open_action_replicas:
                        deferred.append(
                            {
                                "replica_id": replica["replica_id"],
                                "reason": "unfinished_drain_action",
                            }
                        )
                        continue
                    drain_actions.append(
                        self._create_drain_action(
                            connection=connection,
                            round_id=round_id,
                            tick=tick,
                            service=service,
                            replica=replica,
                            priority=0,
                            order_key=(
                                f"0|{service.name}|drain-retry|"
                                f"{int(replica['ordinal']):08d}"
                            ),
                        )
                    )
                for replica in sorted(
                    (row for row in replicas if row["phase"] == "pending"),
                    key=lambda row: int(row["ordinal"]),
                ):
                    if replica["replica_id"] in open_action_replicas:
                        deferred.append(
                            {
                                "replica_id": replica["replica_id"],
                                "reason": "unfinished_action_in_another_round",
                            }
                        )
                        continue
                    next_retry = replica["next_retry_tick"]
                    if next_retry is not None and int(next_retry) > tick:
                        deferred.append(
                            {
                                "replica_id": replica["replica_id"],
                                "reason": "backoff_not_due",
                                "next_retry_tick": int(next_retry),
                            }
                        )
                        continue
                    due_retries.append(replica)

                retry_candidates = due_retries[:launch_deficit]
                new_count = max(0, launch_deficit - len(retry_candidates))
                for replica in retry_candidates:
                    backoff = (
                        plan_backoff(
                            replica_id=str(replica["replica_id"]),
                            failure_count=int(replica["failure_count"]),
                            tick=tick,
                            policy=self.policy,
                        )
                        if int(replica["failure_count"]) > 0
                        else None
                    )
                    key = (
                        0 if below_min else 1,
                        0,
                        service.name,
                        int(replica["failure_count"]),
                        str(replica["replica_id"]),
                    )
                    all_candidates.append(
                        _LaunchCandidate(
                            service=service,
                            replica=replica,
                            replica_id=str(replica["replica_id"]),
                            ordinal=int(replica["ordinal"]),
                            priority=0 if below_min else 1,
                            retry=True,
                            priority_key=key,
                            order_key=(
                                f"{0 if below_min else 1}|{service.name}|retry|"
                                f"{int(replica['failure_count']):03d}|"
                                f"{replica['replica_id']}"
                            ),
                            backoff=backoff,
                        )
                    )

                for new_index in range(new_count):
                    replica_id, ordinal = self.store.allocate_replica_id(
                        connection, service.name
                    )
                    key = (
                        0 if below_min else 1,
                        1,
                        service.name,
                        new_index,
                        replica_id,
                    )
                    all_candidates.append(
                        _LaunchCandidate(
                            service=service,
                            replica=None,
                            replica_id=replica_id,
                            ordinal=ordinal,
                            priority=0 if below_min else 1,
                            retry=False,
                            priority_key=key,
                            order_key=(
                                f"{0 if below_min else 1}|{service.name}|new|"
                                f"{ordinal:08d}"
                            ),
                            backoff=None,
                        )
                    )

                connection.execute(
                    """
                    INSERT INTO service_rounds(
                        round_id, service_name, demand_json, target_replicas,
                        projected_replicas, ready_replicas,
                        cold_starting_replicas, pending_replicas,
                        draining_replicas, launch_deficit, drain_count,
                        deferred_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        round_id,
                        service.name,
                        canonical_json(demand.to_dict()),
                        demand.target_replicas,
                        projected,
                        counts["ready"],
                        counts["cold_starting"],
                        counts["pending"],
                        counts["draining"],
                        launch_deficit,
                        drain_count,
                        canonical_json(deferred),
                    ),
                )

            all_candidates.sort(key=lambda candidate: candidate.priority_key)
            budget_exhausted = False
            for priority_rank, candidate in enumerate(all_candidates):
                service = candidate.service
                cost = self.policy.action_cost(service.cold_start_ticks)
                selected_in_round = per_service_selected[service.name]
                detail: dict[str, Any] = {
                    "priority": {
                        "rank": priority_rank,
                        "key": list(candidate.priority_key),
                        "order_key": candidate.order_key,
                        "below_min_replicas": candidate.priority == 0,
                        "retry": candidate.retry,
                    },
                    "planned_ticks": cost,
                }
                if candidate.backoff is not None:
                    detail["backoff"] = candidate.backoff.to_dict()

                if budget_exhausted or cost > budget_remaining:
                    budget_exhausted = True
                    self._insert_abandoned_decision(
                        connection=connection,
                        round_id=round_id,
                        service=service,
                        replica_id=candidate.replica_id,
                        kind="launch",
                        reason_code="budget_exhausted",
                        outcome_class="budget_not_started",
                        status="not_started_budget",
                        order_key=candidate.order_key,
                        detail={
                            **detail,
                            "budget_limit": budget,
                            "budget_remaining_before": budget_remaining,
                            "budget_remaining_after": budget_remaining,
                            "truncation_position": priority_rank,
                        },
                    )
                    continue
                if selected_in_round >= service.max_concurrent_launches:
                    self._insert_abandoned_decision(
                        connection=connection,
                        round_id=round_id,
                        service=service,
                        replica_id=candidate.replica_id,
                        kind="launch",
                        reason_code="service_concurrency_limit",
                        order_key=candidate.order_key,
                        detail={
                            **detail,
                            "limit": service.max_concurrent_launches,
                            "selected_before_rejection": selected_in_round,
                        },
                    )
                    continue
                placement_result = place_replica(
                    packing_state,
                    service,
                    candidate.replica_id,
                )
                if placement_result.placement is None:
                    self._insert_abandoned_decision(
                        connection=connection,
                        round_id=round_id,
                        service=service,
                        replica_id=candidate.replica_id,
                        kind="launch",
                        reason_code=str(
                            placement_result.report.get(
                                "first_constraint", "no_feasible_placement"
                            )
                        ),
                        order_key=candidate.order_key,
                        detail={**detail, "packing": placement_result.report},
                    )
                    continue

                placement = placement_result.placement
                if candidate.replica is None:
                    connection.execute(
                        """
                        INSERT INTO replicas(
                            replica_id, service_name, model_version, ordinal,
                            phase, failure_count, next_retry_tick, ready_tick,
                            cold_start_ticks, created_tick, updated_tick,
                            last_round_id
                        ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, ?, ?, ?)
                        """,
                        (
                            candidate.replica_id,
                            service.name,
                            service.model_version,
                            candidate.ordinal,
                            service.cold_start_ticks,
                            tick,
                            tick,
                            round_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE replicas
                        SET updated_tick = ?, last_round_id = ?
                        WHERE replica_id = ?
                        """,
                        (tick, round_id, candidate.replica_id),
                    )

                action_id = f"{round_id}:{candidate.replica_id}:launch"
                decision_id = action_id
                connection.execute(
                    """
                    INSERT INTO occupancies(
                        replica_id, service_name, node_name, gpu_index,
                        memory_mib, compute_units, round_id, reserved_tick
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.replica_id,
                        service.name,
                        placement.node_name,
                        placement.gpu_index,
                        placement.memory_mib,
                        placement.compute_units,
                        round_id,
                        tick,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO actions(
                        action_id, round_id, replica_id, service_name,
                        action, status, node_name, gpu_index, memory_mib,
                        compute_units, priority, order_key, planned_ticks,
                        charged_ticks, idempotency_key
                    ) VALUES (?, ?, ?, ?, 'launch', 'reserved', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        round_id,
                        candidate.replica_id,
                        service.name,
                        placement.node_name,
                        placement.gpu_index,
                        placement.memory_mib,
                        placement.compute_units,
                        priority_rank,
                        candidate.order_key,
                        cost,
                        cost,
                        hashlib.sha256(action_id.encode("utf-8")).hexdigest(),
                    ),
                )
                decision_detail = {
                    **detail,
                    "packing": placement_result.report,
                    "placement": placement.to_dict(),
                }
                connection.execute(
                    """
                    INSERT INTO round_decisions(
                        decision_id, round_id, service_name, replica_id,
                        kind, status, outcome_class, reason_code,
                        order_key, detail_json
                    ) VALUES (?, ?, ?, ?, 'launch', 'selected', 'started',
                              NULL, ?, ?)
                    """,
                    (
                        decision_id,
                        round_id,
                        service.name,
                        candidate.replica_id,
                        candidate.order_key,
                        canonical_json(decision_detail),
                    ),
                )
                per_service_selected[service.name] += 1
                budget_remaining -= cost

            connection.execute(
                """
                UPDATE rounds
                SET budget_used_ticks = ?
                WHERE round_id = ?
                """,
                (budget - budget_remaining, round_id),
            )
            connection.execute(
                "UPDATE rounds SET status = 'planned' WHERE round_id = ?",
                (round_id,),
            )

    @staticmethod
    def _phase_counts(replicas: Iterable[dict[str, Any]]) -> dict[str, int]:
        counts = {phase: 0 for phase in PHASES}
        for replica in replicas:
            phase = str(replica["phase"])
            if phase not in counts:
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "replica has unknown phase",
                    details={"replica_id": replica["replica_id"], "phase": phase},
                )
            counts[phase] += 1
        return counts

    def _create_drain_action(
        self,
        *,
        connection: Any,
        round_id: str,
        tick: int,
        service: ServiceSpec,
        replica: dict[str, Any],
        priority: int,
        order_key: str,
    ) -> dict[str, Any]:
        replica_id = str(replica["replica_id"])
        occupancy = connection.execute(
            "SELECT * FROM occupancies WHERE replica_id = ?",
            (replica_id,),
        ).fetchone()
        if occupancy is None:
            raise BusinessError(
                ErrorCode.STATE_CORRUPT,
                "ready replica has no capacity reservation",
                details={"replica_id": replica_id},
            )
        action_id = f"{round_id}:{replica_id}:drain"
        decision_id = action_id
        connection.execute(
            """
            INSERT INTO actions(
                action_id, round_id, replica_id, service_name, action,
                status, node_name, gpu_index, memory_mib, compute_units,
                priority, order_key, planned_ticks, charged_ticks,
                idempotency_key
            ) VALUES (?, ?, ?, ?, 'drain', 'reserved', ?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                action_id,
                round_id,
                replica_id,
                service.name,
                occupancy["node_name"],
                occupancy["gpu_index"],
                occupancy["memory_mib"],
                occupancy["compute_units"],
                priority,
                order_key,
                hashlib.sha256(action_id.encode("utf-8")).hexdigest(),
            ),
        )
        from_phase = str(replica["phase"])
        if from_phase == "ready":
            connection.execute(
                """
                UPDATE replicas
                SET phase = 'draining', updated_tick = ?, last_round_id = ?
                WHERE replica_id = ?
                """,
                (tick, round_id, replica_id),
            )
            transition_reason = "scale_down_decision"
        elif from_phase == "draining":
            connection.execute(
                """
                UPDATE replicas
                SET updated_tick = ?, last_round_id = ?
                WHERE replica_id = ?
                """,
                (tick, round_id, replica_id),
            )
            transition_reason = "drain_retry"
        else:
            raise BusinessError(
                ErrorCode.STATE_CORRUPT,
                "only ready or draining replicas can be drained",
                details={"replica_id": replica_id, "phase": from_phase},
            )
        self.store.record_transition(
            connection,
            replica_id=replica_id,
            from_phase=from_phase,
            to_phase="draining",
            tick=tick,
            round_id=round_id,
            reason=transition_reason,
        )
        detail = {
            "reason": transition_reason,
            "target_from_demand": True,
            "occupancy": dict(occupancy),
        }
        connection.execute(
            """
            INSERT INTO round_decisions(
                decision_id, round_id, service_name, replica_id,
                kind, status, outcome_class, reason_code,
                order_key, detail_json
            ) VALUES (?, ?, ?, ?, 'drain', 'selected', 'started',
                      NULL, ?, ?)
            """,
            (
                decision_id,
                round_id,
                service.name,
                replica_id,
                order_key,
                canonical_json(detail),
            ),
        )
        return {
            "action_id": action_id,
            "replica_id": replica_id,
            "order_key": order_key,
        }

    @staticmethod
    def _insert_abandoned_decision(
        *,
        connection: Any,
        round_id: str,
        service: ServiceSpec,
        replica_id: str,
        kind: str,
        reason_code: str,
        order_key: str,
        detail: dict[str, Any],
        outcome_class: str = "abandoned",
        status: str = "abandoned",
    ) -> None:
        decision_id = f"{round_id}:{replica_id}:{kind}"
        connection.execute(
            """
            INSERT INTO round_decisions(
                decision_id, round_id, service_name, replica_id,
                kind, status, outcome_class, reason_code,
                order_key, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                round_id,
                service.name,
                replica_id,
                kind,
                status,
                outcome_class,
                reason_code,
                order_key,
                canonical_json(detail),
            ),
        )

    def _execute_unfinished_actions(self, *, round_id: str, tick: int) -> None:
        with self.store.read() as connection:
            actions = [
                action
                for action in self.store.list_actions(connection, round_id)
                if action["status"] in {"reserved", "in_flight"}
            ]
        if not actions:
            return
        action_ids = [str(action["action_id"]) for action in actions]
        self.store.set_in_flight(round_id, action_ids, tick)

        results: dict[str, tuple[str, dict[str, Any]]] = {}
        max_workers = max(1, min(len(actions), 32))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    self._execute_one,
                    action=action,
                    tick=tick,
                ): action
                for action in actions
            }
            for future in concurrent.futures.as_completed(future_map):
                action = future_map[future]
                action_id = str(action["action_id"])
                try:
                    results[action_id] = future.result()
                except Exception as exc:
                    results[action_id] = (
                        "error",
                        {
                            "code": ErrorCode.LAUNCH_INTERNAL.value,
                            "message": "action worker failed unexpectedly",
                            "details": {"exception_type": type(exc).__name__},
                        },
                    )

        for action in actions:
            action_id = str(action["action_id"])
            outcome, detail = results[action_id]
            service = self.services[str(action["service_name"])]
            if action["action"] == "launch":
                if outcome == "ok":
                    self.store.complete_launch_success(
                        round_id=round_id,
                        action_id=action_id,
                        tick=tick,
                        ready_tick=tick + service.cold_start_ticks,
                        detail=detail,
                    )
                else:
                    replica = self._replica_state(str(action["replica_id"]))
                    failure_count = int(replica["failure_count"]) + 1
                    backoff = plan_backoff(
                        replica_id=str(action["replica_id"]),
                        failure_count=failure_count,
                        tick=tick,
                        policy=self.policy,
                    )
                    self.store.complete_launch_failure(
                        round_id=round_id,
                        action_id=action_id,
                        tick=tick,
                        error_code=str(detail.get("code", ErrorCode.LAUNCH_INTERNAL)),
                        detail=detail,
                        failure_count=failure_count,
                        next_retry_tick=backoff.next_retry_tick,
                        backoff=backoff.to_dict(),
                    )
            else:
                if outcome == "ok":
                    self.store.complete_drain_success(
                        round_id=round_id,
                        action_id=action_id,
                        tick=tick,
                        detail=detail,
                    )
                else:
                    self.store.complete_drain_failure(
                        round_id=round_id,
                        action_id=action_id,
                        tick=tick,
                        error_code=str(detail.get("code", ErrorCode.LAUNCH_INTERNAL)),
                        detail=detail,
                    )

    def _execute_one(
        self,
        *,
        action: dict[str, Any],
        tick: int,
    ) -> tuple[str, dict[str, Any]]:
        service = self.services[str(action["service_name"])]
        request = LifecycleRequest(
            action=str(action["action"]),
            replica_id=str(action["replica_id"]),
            service_name=service.name,
            model_version=service.model_version,
            sample_digest=self.bundle.sample_digest,
            tick=tick,
            round_id=str(action["round_id"]),
            idempotency_key=str(action["idempotency_key"]),
            node_name=str(action["node_name"]),
            gpu_index=int(action["gpu_index"]),
            metadata={
                "memory_mib": int(action["memory_mib"]),
                "compute_units": int(action["compute_units"]),
                "cold_start_ticks": service.cold_start_ticks,
            },
        )
        try:
            response = self.launcher.execute(
                request,
                self.policy.action_timeout_seconds,
            )
            return "ok", {"response": response}
        except BusinessError as exc:
            return "error", {
                "code": exc.code.value,
                "message": exc.message,
                "details": exc.details,
            }
        except Exception as exc:
            return "error", {
                "code": ErrorCode.LAUNCH_INTERNAL.value,
                "message": "lifecycle action raised an unexpected exception",
                "details": {"exception_type": type(exc).__name__},
            }

    def _replica_state(self, replica_id: str) -> dict[str, Any]:
        with self.store.read() as connection:
            row = connection.execute(
                "SELECT * FROM replicas WHERE replica_id = ?",
                (replica_id,),
            ).fetchone()
            if row is None:
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "action references a missing replica",
                    details={"replica_id": replica_id},
                )
            return dict(row)

    def _build_result(self, *, round_id: str, tick: int) -> dict[str, Any]:
        with self.store.read() as connection:
            round_row = dict(
                connection.execute(
                    "SELECT * FROM rounds WHERE round_id = ?",
                    (round_id,),
                ).fetchone()
            )
            service_rows = self.store.list_service_rounds(connection, round_id)
            decisions = self.store.list_decisions(connection, round_id)
            actions = self.store.list_actions(connection, round_id)
            occupancies = self.store.list_occupancies(connection)
            replicas = self.store.list_replicas(connection)

            for item in decisions:
                item["detail"] = json.loads(item.pop("detail_json"))
            for item in actions:
                item["detail"] = (
                    json.loads(item.pop("detail_json"))
                    if item.get("detail_json")
                    else None
                )

        decisions_by_service: dict[str, list[dict[str, Any]]] = {}
        for decision in decisions:
            decisions_by_service.setdefault(decision["service_name"], []).append(
                decision
            )
        actions_by_service: dict[str, list[dict[str, Any]]] = {}
        for action in actions:
            actions_by_service.setdefault(action["service_name"], []).append(action)

        services_result: list[dict[str, Any]] = []
        for service_row in service_rows:
            name = service_row["service_name"]
            services_result.append(
                {
                    "service_name": name,
                    "demand": service_row["demand"],
                    "state_before_round": {
                        "target_replicas": service_row["target_replicas"],
                        "projected_replicas": service_row["projected_replicas"],
                        "ready_replicas": service_row["ready_replicas"],
                        "cold_starting_replicas": service_row[
                            "cold_starting_replicas"
                        ],
                        "pending_replicas": service_row["pending_replicas"],
                        "draining_replicas": service_row["draining_replicas"],
                        "launch_deficit": service_row["launch_deficit"],
                        "drain_count": service_row["drain_count"],
                    },
                    "deferred": service_row["deferred"],
                    "decisions": decisions_by_service.get(name, []),
                    "actions": actions_by_service.get(name, []),
                }
            )

        outcome_counts = {
            "started": 0,
            "launch_failed": 0,
            "budget_not_started": 0,
            "abandoned": 0,
            "drain_succeeded": 0,
            "drain_failed": 0,
        }
        for decision in decisions:
            outcome = decision["outcome_class"]
            if outcome == "started" and decision["kind"] == "drain":
                continue
            if outcome in outcome_counts:
                outcome_counts[outcome] += 1
        for action in actions:
            if action["action"] == "drain" and action["status"] == "succeeded":
                outcome_counts["drain_succeeded"] += 1
            elif action["action"] == "drain" and action["status"] == "failed":
                outcome_counts["drain_failed"] += 1

        packing_state = PackingState.from_occupancies(
            self.bundle.node_pool, occupancies
        )
        result = {
            "ok": True,
            "round_id": round_id,
            "tick": tick,
            "status": "committed",
            "sample_digest": self.bundle.sample_digest,
            "service_filter": (
                json.loads(round_row["service_filter"])
                if round_row["service_filter"]
                else None
            ),
            "budget": {
                "limit_ticks": int(round_row["budget_limit_ticks"]),
                "used_ticks": int(round_row["budget_used_ticks"]),
                "remaining_ticks": int(round_row["budget_limit_ticks"])
                - int(round_row["budget_used_ticks"]),
            },
            "services": services_result,
            "outcome_counts": outcome_counts,
            "capacity_after": {
                "gpu_free_mib": packing_state.free_mib_by_node(),
                "node_instances_used": packing_state.node_instance_used,
                "compute_units_used": packing_state.compute_used,
                "compute_units_capacity": (
                    self.bundle.node_pool.compute_units_capacity
                ),
            },
            "replica_phases": self._phase_summary(replicas),
        }
        return result

    @staticmethod
    def _phase_summary(replicas: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for replica in sorted(
            replicas,
            key=lambda row: (str(row["service_name"]), int(row["ordinal"])),
        ):
            service_name = str(replica["service_name"])
            phase = str(replica["phase"])
            summary.setdefault(service_name, {})
            summary[service_name][phase] = summary[service_name].get(phase, 0) + 1
        return summary

