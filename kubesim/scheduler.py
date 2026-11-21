from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .errors import ConflictError, EngineError, StorageError
from .filters import NodeFilterResult, evaluate_filters
from .jsonutil import digest
from .models import (
    SCORE_ORDER,
    Dataset,
    Injection,
    Pod,
    ScheduleConfig,
    parse_config,
    parse_pod,
    topology_domain_keys,
)
from .runtime import ClusterState
from .scoring import ScoreSelection, score_nodes
from .store import Store


ROUND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RETRYABLE_BIND_ERRORS = {
    "E_STALE_RESOURCE_VERSION",
    "E_ORDER_BLOCKED",
    "E_INSUFFICIENT_CPU",
    "E_INSUFFICIENT_MEMORY",
    "E_INSUFFICIENT_GPU",
    "E_ANTI_AFFINITY_CONFLICT",
    "E_TOPOLOGY_SKEW",
}


def _empty_scoring(tolerance: str) -> dict[str, Any]:
    return {
        "plugin_order": list(SCORE_ORDER),
        "node_scores": [],
        "selected_node": None,
        "precision_sensitive": False,
        "tolerance": tolerance,
        "top_two_difference": None,
        "error_sources": [],
    }


@dataclass
class PreparedDecision:
    pod: Pod
    filters: tuple[NodeFilterResult, ...]
    selection: ScoreSelection | None
    chain: dict[str, Any]
    filter_cost_ms: int
    score_cost_ms: int
    bind_cost_ms: int
    abandon_reason: str | None = None

    @property
    def has_feasible_node(self) -> bool:
        return any(result.passed for result in self.filters)

    @property
    def selected_node(self) -> str | None:
        return self.selection.selected_node if self.selection is not None else None


@dataclass
class ActiveJob:
    pod: Pod
    round_pod: dict[str, Any]
    started_at_ms: int
    next_wake_ms: int
    prepared: PreparedDecision
    retries_left: int
    needs_recompute: bool = False
    prestarted: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class Scheduler:
    def __init__(self, store: Store, dataset: Dataset, instance_id: str) -> None:
        self.store = store
        self.dataset = dataset
        self.instance_id = instance_id
        self._terminate_disabled_pods: set[str] = set()

    def schedule(
        self,
        *,
        round_id: str | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        batch_digest = self.dataset.digest
        existing = self.store.get_round_by_batch(batch_digest)
        if round_id is not None and not ROUND_ID_RE.fullmatch(round_id):
            raise EngineError(
                "E_BAD_ROUND_ID",
                "round id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                {"round_id": round_id},
            )
        if existing is not None:
            round_id = existing["round_id"]
        else:
            if round_id is None:
                round_id = f"round-{batch_digest[:16]}"
            cluster = self.store.cluster_state()
            bound_names = {pod.name for pod in cluster.bound_pods}
            pending_pods = [pod for pod in self.dataset.pods if pod.name not in bound_names]
            if not pending_pods:
                latest = self.store.latest_committed_report()
                if latest is not None:
                    return latest
                raise StorageError(
                    "E_NO_PENDING_PODS",
                    "all pods already have committed bindings",
                )
            versions = self.store.node_versions()
            input_digest = self.store.compute_input_digest(
                batch_digest,
                self.dataset.config,
                tuple(pending_pods),
                versions,
            )
            start_clock = self.store.next_clock()
            existing, _ = self.store.create_round(
                round_id=round_id,
                batch_digest=batch_digest,
                input_digest=input_digest,
                config=self.dataset.config,
                pods_in_order=sorted(
                    pending_pods,
                    key=lambda item: (-item.priority, item.name),
                ),
                versions=versions,
                start_clock_ms=start_clock,
            )
            round_id = existing["round_id"]

        with self.store.execution_lock(round_id):
            current = self.store.get_round(round_id)
            if current is None:
                raise StorageError("E_NOT_FOUND", "round disappeared")
            if current["status"] == "COMMITTED":
                report = self.store.get_report(round_id)
                if report is None:
                    raise StorageError("E_CORRUPT_STATE", "committed round lacks report")
                return report
            if current["status"] != "RUNNING":
                raise ConflictError(
                    "E_ROUND_NOT_RUNNING",
                    "round is not runnable",
                    {"status": current["status"]},
                )
            stored_config = parse_config(json.loads(current["config_json"]))
            if digest(stored_config.to_dict()) != current["config_digest"]:
                raise ConflictError(
                    "E_ROUND_DIGEST_MISMATCH",
                    "persisted configuration digest is inconsistent",
                )
            self._execute_round(current, stored_config, resume=resume)
            report = self.store.get_report(round_id)
            if report is None:
                raise StorageError("E_INCOMPLETE_ROUND", "round was not finalized")
            return report

    def _injections_for(
        self,
        pod_name: str,
        kind: str,
        phase: str | None = None,
        plugin: str | None = None,
    ) -> list[Injection]:
        matches = [
            item
            for item in self.dataset.injections
            if item.pod == pod_name and item.kind == kind
        ]
        if phase is not None:
            matches = [item for item in matches if item.phase == phase]
        if plugin is not None:
            matches = [
                item
                for item in matches
                if item.plugin is None or item.plugin == plugin
            ]
        return matches

    def _latency_for(
        self,
        pod_name: str,
        phase: str,
        plugin: str | None = None,
    ) -> int:
        return sum(
            item.ms
            for item in self._injections_for(pod_name, "latency", phase, plugin)
            if item.plugin == plugin
        )

    def _phase_latency(self, pod_name: str, phase: str) -> int:
        return sum(
            item.ms
            for item in self.dataset.injections
            if item.pod == pod_name and item.kind == "latency" and item.phase == phase
        )

    def _maybe_terminate(
        self,
        pod_name: str,
        phase: str,
        plugin: str | None,
    ) -> None:
        if pod_name in self._terminate_disabled_pods:
            return
        matches = self._injections_for(pod_name, "terminate", phase, plugin)
        exact = [item for item in matches if item.plugin == plugin]
        unnamed = [
            item for item in matches if item.plugin is None and plugin is None
        ]
        chosen = exact or unnamed
        if chosen:
            os._exit(chosen[0].exit_code)

    def _abandon_reason(self, pod_name: str, phase: str, plugin: str | None) -> str | None:
        matches = self._injections_for(pod_name, "abandon", phase)
        for item in matches:
            if item.plugin is None or item.plugin == plugin:
                return item.reason
        return None

    def _prepare_decision(
        self,
        *,
        round_id: str,
        round_pod: dict[str, Any],
        pod: Pod,
        state: ClusterState,
        config: ScheduleConfig,
        start_clock_ms: int,
    ) -> PreparedDecision:
        pod_name = pod.name
        version = pod.version
        clock = start_clock_ms
        self.store.update_stage(
            round_id=round_id,
            pod_name=pod_name,
            pod_version=version,
            stage="STARTED",
            clock_ms=clock,
        )
        self._maybe_terminate(pod_name, "filter", None)
        phase_level_filter_latency = sum(
            item.ms
            for item in self.dataset.injections
            if item.pod == pod_name
            and item.kind == "latency"
            and item.phase == "filter"
            and item.plugin is None
        )
        clock += phase_level_filter_latency
        for plugin in config.filters:
            clock += config.filter_costs[plugin]
            clock += self._latency_for(pod_name, "filter", plugin)
            self.store.update_stage(
                round_id=round_id,
                pod_name=pod_name,
                pod_version=version,
                stage=f"FILTERING:{plugin}",
                clock_ms=clock,
            )
            self._maybe_terminate(pod_name, "filter", plugin)
        filters = evaluate_filters(pod, state, config)
        filter_cost = clock - start_clock_ms
        draft = {
            "pod": pod_name,
            "pod_version": version,
            "filters": [result.to_dict() for result in filters],
        }
        self.store.update_stage(
            round_id=round_id,
            pod_name=pod_name,
            pod_version=version,
            stage="FILTERED",
            clock_ms=clock,
            chain=draft,
        )
        abandon_reason = self._abandon_reason(pod_name, "after_filter", None)

        selection: ScoreSelection | None = None
        score_clock = clock
        if any(result.passed for result in filters):
            phase_level_score_latency = sum(
                item.ms
                for item in self.dataset.injections
                if item.pod == pod_name
                and item.kind == "latency"
                and item.phase == "score"
                and item.plugin is None
            )
            score_clock += phase_level_score_latency
            for plugin in SCORE_ORDER:
                score_clock += config.score_costs[plugin]
                score_clock += self._latency_for(pod_name, "score", plugin)
                self.store.update_stage(
                    round_id=round_id,
                    pod_name=pod_name,
                    pod_version=version,
                    stage=f"SCORING:{plugin}",
                    clock_ms=score_clock,
                )
                self._maybe_terminate(pod_name, "score", plugin)
            selection = score_nodes(pod, filters, state, config)
            abandon_reason = abandon_reason or self._abandon_reason(
                pod_name, "score", None
            )
        score_cost = score_clock - clock
        scoring_payload = (
            selection.to_dict()
            if selection is not None
            else _empty_scoring(
                str(config.precision_tolerance),
            )
        )
        chain = {
            "pod": pod_name,
            "pod_version": version,
            "filters": draft["filters"],
            "scoring": scoring_payload,
            "selected_node": selection.selected_node if selection is not None else None,
            "precision_sensitive": (
                selection.precision_sensitive if selection is not None else False
            ),
        }
        if selection is not None:
            self.store.update_stage(
                round_id=round_id,
                pod_name=pod_name,
                pod_version=version,
                stage="SCORED",
                clock_ms=score_clock,
                chain=chain,
            )
        self._maybe_terminate(pod_name, "score", None)
        return PreparedDecision(
            pod=pod,
            filters=filters,
            selection=selection,
            chain=chain,
            filter_cost_ms=filter_cost,
            score_cost_ms=score_cost,
            bind_cost_ms=config.bind_cost_ms + self._phase_latency(pod_name, "bind"),
            abandon_reason=abandon_reason,
        )

    def _start_batch(
        self,
        *,
        round_id: str,
        candidates: list[dict[str, Any]],
        clock_ms: int,
        config: ScheduleConfig,
        executor: ThreadPoolExecutor,
        state: ClusterState,
    ) -> list[ActiveJob]:
        futures: list[tuple[dict[str, Any], Pod, Any]] = []
        for item in candidates:
            pod = item["pod"]
            self.store.update_stage(
                round_id=round_id,
                pod_name=pod.name,
                pod_version=pod.version,
                stage="STARTED",
                clock_ms=clock_ms,
            )
            future = executor.submit(
                self._prepare_decision,
                round_id=round_id,
                round_pod=item["row"],
                pod=pod,
                state=state,
                config=config,
                start_clock_ms=clock_ms,
            )
            futures.append((item, pod, future))
        jobs: list[ActiveJob] = []
        for item, pod, future in futures:
            prepared = future.result()
            if prepared.abandon_reason is not None:
                finish = clock_ms + prepared.filter_cost_ms + prepared.score_cost_ms
                outcome = "ACTIVE_ABANDON"
                reason_code = "E_INJECTED_ACTIVE_ABANDON"
                reason = prepared.abandon_reason
            elif not prepared.has_feasible_node:
                finish = clock_ms + prepared.filter_cost_ms
                outcome = "FILTER_FAILED"
                reason_code = "NO_FEASIBLE_NODES"
                reason = "all nodes were eliminated by the enabled filter chain"
            else:
                finish = (
                    clock_ms
                    + prepared.filter_cost_ms
                    + prepared.score_cost_ms
                    + prepared.bind_cost_ms
                )
                outcome = "BOUND"
                reason_code = ""
                reason = ""
            job = ActiveJob(
                pod=pod,
                round_pod=item["row"],
                started_at_ms=clock_ms,
                next_wake_ms=finish,
                prepared=prepared,
                retries_left=config.retry_budget,
                prestarted=bool(item.get("prestarted")),
                extra={
                    "pending_outcome": outcome,
                    "pending_reason_code": reason_code,
                    "pending_reason": reason,
                },
            )
            jobs.append(job)
        return jobs

    def _execute_round(
        self,
        round: dict[str, Any],
        config: ScheduleConfig,
        *,
        resume: bool,
    ) -> None:
        round_id = round["round_id"]
        start_clock = int(round["start_clock_ms"])
        deadline = start_clock + config.budget_ms
        clock = max(int(round["clock_ms"]), start_clock)
        pending: list[dict[str, Any]] = []
        for row in round["pods"]:
            if row["status"] == "TERMINAL":
                continue
            previous_stage = row["stage"]
            prestarted = row["status"] == "RUNNING" and previous_stage not in {
                "PENDING",
                "RECOVERED_REPLAY",
            }
            if row["status"] == "RUNNING":
                self.store.reset_pod_for_replay(
                    round_id=round_id,
                    pod_name=row["pod_name"],
                    pod_version=row["pod_version"],
                    previous_stage=previous_stage,
                )
            pod = parse_pod(json.loads(row["spec_json"]), 0)
            pending.append(
                {
                    "row": row,
                    "pod": pod,
                    "prestarted": prestarted,
                }
            )

        active: list[ActiveJob] = []
        self._terminate_disabled_pods = {
            item["pod"].name for item in pending if item["prestarted"]
        }
        with ThreadPoolExecutor(max_workers=config.max_parallel_pods) as executor:
            while pending or active:
                if clock >= deadline:
                    remaining: list[dict[str, Any]] = []
                    for item in pending:
                        if item["prestarted"]:
                            remaining.append(item)
                            continue
                        self.store.mark_unstarted(
                            round_id=round_id,
                            pod_name=item["pod"].name,
                            pod_version=item["pod"].version,
                            clock_ms=deadline,
                            reason_code="E_BUDGET_UNSTARTED",
                            reason=(
                                f"logical budget deadline {deadline} ms was reached "
                                "before this pod could start"
                            ),
                        )
                    pending = remaining

                while pending and len(active) < config.max_parallel_pods:
                    workload_counts: dict[str, int] = {}
                    for job in active:
                        workload_counts[job.pod.workload] = (
                            workload_counts.get(job.pod.workload, 0) + 1
                        )
                    selected_indices: list[int] = []
                    slots = config.max_parallel_pods - len(active)
                    for index, item in enumerate(pending):
                        if slots <= 0:
                            break
                        workload = item["pod"].workload
                        if (
                            workload_counts.get(workload, 0)
                            >= config.max_concurrent_per_workload
                        ):
                            continue
                        selected_indices.append(index)
                        slots -= 1
                        workload_counts[workload] = workload_counts.get(workload, 0) + 1
                    if not selected_indices:
                        break
                    candidates = [pending[index] for index in selected_indices]
                    for index in reversed(selected_indices):
                        pending.pop(index)
                    state = self.store.cluster_state()
                    active.extend(
                        self._start_batch(
                            round_id=round_id,
                            candidates=candidates,
                            clock_ms=clock,
                            config=config,
                            executor=executor,
                            state=state,
                        )
                    )

                if not active:
                    if pending:
                        for item in pending:
                            if not item["prestarted"]:
                                self.store.mark_unstarted(
                                    round_id=round_id,
                                    pod_name=item["pod"].name,
                                    pod_version=item["pod"].version,
                                    clock_ms=max(deadline, clock),
                                    reason_code="E_BUDGET_UNSTARTED",
                                    reason=(
                                        f"logical budget deadline {deadline} ms was reached "
                                        "before this pod could start"
                                    ),
                                )
                        pending = [item for item in pending if item["prestarted"]]
                        if not pending:
                            break
                    else:
                        break

                if not active:
                    break
                clock = max(clock, min(job.next_wake_ms for job in active))
                due = sorted(
                    (job for job in active if job.next_wake_ms == clock),
                    key=lambda job: int(job.round_pod["schedule_order"]),
                )
                for job in due:
                    if job.needs_recompute:
                        state = self.store.cluster_state()
                        job.prepared = self._prepare_decision(
                            round_id=round_id,
                            round_pod=job.round_pod,
                            pod=job.pod,
                            state=state,
                            config=config,
                            start_clock_ms=clock,
                        )
                        job.needs_recompute = False
                        if job.prepared.abandon_reason is not None:
                            job.extra["pending_outcome"] = "ACTIVE_ABANDON"
                            job.extra[
                                "pending_reason_code"
                            ] = "E_INJECTED_ACTIVE_ABANDON"
                            job.extra["pending_reason"] = job.prepared.abandon_reason
                            job.next_wake_ms = clock + job.prepared.filter_cost_ms
                            continue
                        if not job.prepared.has_feasible_node:
                            job.extra["pending_outcome"] = "FILTER_FAILED"
                            job.extra["pending_reason_code"] = "NO_FEASIBLE_NODES"
                            job.extra[
                                "pending_reason"
                            ] = "all nodes were eliminated by the enabled filter chain"
                            job.next_wake_ms = clock + job.prepared.filter_cost_ms
                            continue
                        job.extra["pending_outcome"] = "BOUND"
                        job.extra["pending_reason_code"] = ""
                        job.extra["pending_reason"] = ""
                        job.next_wake_ms = (
                            clock
                            + job.prepared.filter_cost_ms
                            + job.prepared.score_cost_ms
                            + job.prepared.bind_cost_ms
                        )
                        continue

                    outcome = job.extra["pending_outcome"]
                    reason_code = job.extra["pending_reason_code"]
                    reason = job.extra["pending_reason"]
                    if outcome != "BOUND":
                        self.store.finish_pod(
                            round_id=round_id,
                            round_input_digest=round["input_digest"],
                            pod_name=job.pod.name,
                            pod_version=job.pod.version,
                            outcome=outcome,
                            reason_code=reason_code,
                            reason=reason,
                            clock_ms=clock,
                            chain=job.prepared.chain,
                            started_at_ms=job.started_at_ms,
                        )
                        active.remove(job)
                        continue

                    selected_node = job.prepared.selected_node
                    if selected_node is None:
                        self.store.finish_pod(
                            round_id=round_id,
                            round_input_digest=round["input_digest"],
                            pod_name=job.pod.name,
                            pod_version=job.pod.version,
                            outcome="BIND_FAILED",
                            reason_code="NO_SELECTED_NODE",
                            reason="scoring did not select a feasible node",
                            clock_ms=clock,
                            chain=job.prepared.chain,
                            started_at_ms=job.started_at_ms,
                        )
                        active.remove(job)
                        continue

                    self.store.update_stage(
                        round_id=round_id,
                        pod_name=job.pod.name,
                        pod_version=job.pod.version,
                        stage="BINDING",
                        clock_ms=clock,
                        chain=job.prepared.chain,
                    )
                    self._maybe_terminate(job.pod.name, "bind", None)
                    state = self.store.cluster_state()
                    node = state.nodes_by_name.get(selected_node)
                    if node is None:
                        self.store.finish_pod(
                            round_id=round_id,
                            round_input_digest=round["input_digest"],
                            pod_name=job.pod.name,
                            pod_version=job.pod.version,
                            outcome="BIND_FAILED",
                            reason_code="E_NODE_MISSING",
                            reason="selected node disappeared",
                            clock_ms=clock,
                            chain=job.prepared.chain,
                            started_at_ms=job.started_at_ms,
                        )
                        active.remove(job)
                        continue
                    topology_domains = {
                        key: node.labels[key]
                        for key in set(job.pod.topology_keys)
                        | set(topology_domain_keys(node.labels))
                        if key in node.labels
                    }
                    try:
                        self.store.commit_binding(
                            round_id=round_id,
                            round_input_digest=round["input_digest"],
                            pod_name=job.pod.name,
                            pod_version=job.pod.version,
                            node_name=selected_node,
                            resources=job.pod.resources,
                            expected_node_version=node.resource_version,
                            topology_domains=topology_domains,
                            clock_ms=clock,
                            chain=job.prepared.chain,
                            started_at_ms=job.started_at_ms,
                        )
                    except ConflictError as exc:
                        if exc.code in RETRYABLE_BIND_ERRORS and job.retries_left > 0:
                            job.retries_left -= 1
                            job.needs_recompute = True
                            job.next_wake_ms = clock + max(1, config.retry_delay_ms)
                            continue
                        retry_code = (
                            "E_RETRY_BUDGET_EXHAUSTED"
                            if exc.code in RETRYABLE_BIND_ERRORS
                            else exc.code
                        )
                        self.store.finish_pod(
                            round_id=round_id,
                            round_input_digest=round["input_digest"],
                            pod_name=job.pod.name,
                            pod_version=job.pod.version,
                            outcome=(
                                "ACTIVE_ABANDON"
                                if retry_code == "E_RETRY_BUDGET_EXHAUSTED"
                                else "BIND_FAILED"
                            ),
                            reason_code=retry_code,
                            reason=exc.message,
                            clock_ms=clock,
                            chain=job.prepared.chain,
                            started_at_ms=job.started_at_ms,
                        )
                        active.remove(job)
                        continue
                    active.remove(job)

        current_round = self.store.get_round(round_id)
        if current_round is None:
            raise StorageError("E_NOT_FOUND", "round disappeared before finalization")
        end_clock = max(
            [clock]
            + [
                int(row["finished_at_ms"])
                for row in current_round["pods"]
                if row["finished_at_ms"] is not None
            ]
        )
        report = self._build_report(
            round=current_round,
            config=config,
            start_clock_ms=start_clock,
            end_clock_ms=end_clock,
            deadline_ms=deadline,
            watermark_before_ms=start_clock,
        )
        ledger = self.store.round_ledger(round_id)
        self.store.finalize_round(
            round_id=round_id,
            expected_input_digest=round["input_digest"],
            expected_ledger_digest=digest(ledger),
            report=report,
            end_clock_ms=end_clock,
        )

    def _build_report(
        self,
        *,
        round: dict[str, Any],
        config: ScheduleConfig,
        start_clock_ms: int,
        end_clock_ms: int,
        deadline_ms: int,
        watermark_before_ms: int,
    ) -> dict[str, Any]:
        state = self.store.cluster_state()
        topology = self.store.topology_state()
        tolerance = str(config.precision_tolerance)
        pod_reports: list[dict[str, Any]] = []
        summary = {
            "total": len(round["pods"]),
            "bound": 0,
            "filter_failed": 0,
            "budget_unstarted": 0,
            "active_abandon": 0,
            "bind_failed": 0,
        }
        recovery_replays: list[dict[str, Any]] = []
        for row in round["pods"]:
            chain = json.loads(row["chain_json"]) if row["chain_json"] else {}
            recovery = json.loads(row["recovery_json"]) if row["recovery_json"] else None
            if recovery:
                recovery_replays.append(
                    {"pod": row["pod_name"], **recovery}
                )
            filters = chain.get("filters", [])
            scoring = chain.get("scoring", _empty_scoring(tolerance))
            scoring.setdefault("precision_sensitive", False)
            scoring.setdefault("error_sources", [])
            outcome = row["outcome"] or "PENDING"
            if outcome == "BOUND":
                summary["bound"] += 1
            elif outcome == "FILTER_FAILED":
                summary["filter_failed"] += 1
            elif outcome == "BUDGET_UNSTARTED":
                summary["budget_unstarted"] += 1
            elif outcome == "ACTIVE_ABANDON":
                summary["active_abandon"] += 1
            elif outcome == "BIND_FAILED":
                summary["bind_failed"] += 1
            pod_reports.append(
                {
                    "pod": row["pod_name"],
                    "pod_version": row["pod_version"],
                    "priority": int(row["priority"]),
                    "workload": row["workload"],
                    "schedule_order": int(row["schedule_order"]),
                    "started": row["started_at_ms"] is not None,
                    "started_at_ms": row["started_at_ms"],
                    "finished_at_ms": row["finished_at_ms"],
                    "outcome": outcome,
                    "reason_code": row["reason_code"],
                    "reason": row["reason"],
                    "filters": filters,
                    "scoring": scoring,
                    "selected_node": row["selected_node"],
                    "precision_sensitive": bool(
                        scoring.get("precision_sensitive", False)
                    ),
                    "chain_digest": row["chain_digest"],
                    "decision_digest": row["decision_digest"],
                    "recovery": recovery,
                }
            )
        nodes_payload = [
            {
                "name": node.name,
                "capacity": node.capacity.to_dict(),
                "remaining": node.remaining.to_dict(),
                "resource_version": node.resource_version,
                "labels": dict(node.labels),
            }
            for node in state.nodes
        ]
        state_payload = {
            "nodes": nodes_payload,
            "topology_domains": topology,
        }
        ledger = self.store.round_ledger(round["round_id"])
        report = {
            "schema_version": 1,
            "round_id": round["round_id"],
            "round_number": int(round["round_number"]),
            "batch_digest": round["batch_digest"],
            "input_digest": round["input_digest"],
            "status": "COMMITTED",
            "config": config.to_dict(),
            "config_digest": round["config_digest"],
            "logical_clock": {
                "start_ms": start_clock_ms,
                "end_ms": end_clock_ms,
                "watermark_before_ms": watermark_before_ms,
                "budget_ms": config.budget_ms,
                "deadline_ms": deadline_ms,
                "overrun_ms": max(0, end_clock_ms - deadline_ms),
                "clock_source": "injected_logical_clock",
            },
            "nodes": nodes_payload,
            "topology_domains": topology,
            "pods": pod_reports,
            "summary": summary,
            "recovery": {
                "resumed": bool(recovery_replays),
                "replayed_pods": sorted(
                    recovery_replays, key=lambda item: item["pod"]
                ),
            },
            "state_digest": digest(state_payload),
            "decision_ledger_digest": digest(ledger),
        }
        return report
