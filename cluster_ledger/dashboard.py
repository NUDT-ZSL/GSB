"""Fast cached dashboard queries, separate from authoritative recomputation."""

from __future__ import annotations

import copy
import threading
from typing import Any

from .clock import (
    CLIENT_CLOCK_BASIS,
    fixed_ms_from_ns,
    mono_ms,
    mono_ns,
)
from .config import Declaration
from .deterministic import canonical_json, digest_text
from .errors import E_LATENCY
from .ledger import Ledger
from .logging_utils import EventLogger
from .metrics import compute_latency_stats


class Dashboard:
    """Serves a complete, immutable view per ledger revision.

    A revision change rebuilds the whole view. Version transitions never merge
    old and new cluster epochs into the same cached payload.
    """

    def __init__(self, declaration: Declaration, ledger: Ledger,
                 logger: EventLogger | None = None, *, query_budget_ms: int | None = None) -> None:
        self.declaration = declaration
        self.ledger = ledger
        self.logger = logger or EventLogger(None)
        self.query_budget_ms = (
            declaration.limits.query_budget_ms if query_budget_ms is None else query_budget_ms
        )
        self._cache: dict[str, Any] | None = None
        self._cache_revision: int | None = None
        self._generation = 0
        self._lock = threading.RLock()

    def _build_view(self, state: dict[str, Any]) -> dict[str, Any]:
        task_map = self.declaration.task_map()
        cluster_map = self.declaration.cluster_map()
        declared_versions = {cid: cluster.config_version for cid, cluster in cluster_map.items()}
        current_results = [
            result
            for result in state["results"]
            if not result["is_stale"]
            and result["task_id"] in task_map
            and result["cluster_id"] in cluster_map
            and result["config_version"] == declared_versions[result["cluster_id"]]
        ]
        result_index = {(row["task_id"], row["cluster_id"]): row for row in current_results}
        unknown_index = {
            (row["task_id"], row["cluster_id"]): row
            for row in state["recovery"]
            if row["status"] == "unknown"
            and row["cluster_id"] in cluster_map
            and row["config_version"] == declared_versions[row["cluster_id"]]
        }

        tasks_view: list[dict[str, Any]] = []
        for task in self.declaration.tasks:
            units: list[dict[str, Any]] = []
            complete = 0
            unknown = 0
            for cluster_id in task.target_clusters:
                cluster = cluster_map[cluster_id]
                row = result_index.get((task.task_id, cluster_id))
                unknown_row = unknown_index.get((task.task_id, cluster_id))
                if row is not None:
                    status = "complete"
                    complete += 1
                    summary = row["summary"]
                elif unknown_row is not None:
                    status = "unknown"
                    unknown += 1
                    summary = None
                else:
                    status = "pending"
                    summary = None
                units.append(
                    {
                        "cluster_id": cluster_id,
                        "config_version": cluster.config_version,
                        "status": status,
                        "summary": summary,
                        "rerun_required": status == "unknown",
                    }
                )
            total = len(task.target_clusters)
            tasks_view.append(
                {
                    "task_id": task.task_id,
                    "config_keys": list(task.config_keys),
                    "aggregation_mode": task.aggregation_mode,
                    "timeout_ms": task.timeout_ms,
                    "retries": task.retries,
                    "completed_units": complete,
                    "unknown_units": unknown,
                    "total_units": total,
                    "progress_fixed": (complete * 1000 // total) if total else 1000,
                    "units": units,
                }
            )

        clusters_view: list[dict[str, Any]] = []
        for cluster in self.declaration.clusters:
            task_results = [
                {
                    "task_id": row["task_id"],
                    "summary": row["summary"],
                    "components": row["components"],
                    "calculation_duration_ms": row["calculation_duration_ms"],
                }
                for row in current_results
                if row["cluster_id"] == cluster.cluster_id
            ]
            task_results.sort(key=lambda item: item["task_id"])
            clusters_view.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "host": cluster.host,
                    "port": cluster.port,
                    "nodes": list(cluster.nodes),
                    "config_version": cluster.config_version,
                    "results": task_results,
                }
            )

        transitions = [
            {
                "cluster_id": row["cluster_id"],
                "old_config_version": int(row["old_config_version"]),
                "new_config_version": int(row["new_config_version"]),
                "declaration_version": row["declaration_version"],
                "revision": int(row["revision"]),
            }
            for row in state["transitions"]
            if row["declaration_version"] == self.declaration.declaration_version
        ]
        transitions.sort(key=lambda item: (item["cluster_id"], item["revision"]))

        visible_results = sorted(
            [
                {
                    "task_id": row["task_id"],
                    "cluster_id": row["cluster_id"],
                    "config_version": row["config_version"],
                    "aggregation_mode": row["aggregation_mode"],
                    "summary": row["summary"],
                    "components": row["components"],
                    "calculation_duration_ms": row["calculation_duration_ms"],
                    "dispatch_started_mono_ms": row["dispatch_started_mono_ms"],
                    "dispatch_finished_mono_ms": row["dispatch_finished_mono_ms"],
                }
                for row in current_results
            ],
            key=lambda row: (row["task_id"], row["cluster_id"]),
        )
        stale_count = sum(1 for result in state["results"] if result["is_stale"])
        data_version = f"{self.declaration.declaration_version}:r{state['revision']}"
        base = {
            "declaration_version": self.declaration.declaration_version,
            "data_version": data_version,
            "ledger_revision": state["revision"],
            "tasks": tasks_view,
            "clusters": clusters_view,
            "results": visible_results,
            "stale_result_count": stale_count,
            "changed_clusters": [
                {
                    "cluster_id": row["cluster_id"],
                    "old_config_version": row["old_config_version"],
                    "new_config_version": row["new_config_version"],
                }
                for row in transitions
                if row["cluster_id"] in cluster_map
                and row["new_config_version"] == cluster_map[row["cluster_id"]].config_version
            ],
            "clock_basis": CLIENT_CLOCK_BASIS,
        }
        base["view_id"] = "view:" + digest_text(canonical_json(base))
        return base

    def query_view(self) -> dict[str, Any]:
        started_ns = mono_ns()
        started_ms = started_ns // 1_000_000
        revision = self.ledger.revision()
        state = None
        old_cache_revision: int | None = None
        cache_invalidated = False
        with self._lock:
            cache_hit = self._cache is not None and self._cache_revision == revision
            cache_replaced = not cache_hit
            if cache_hit:
                base = copy.deepcopy(self._cache)
                generation = self._generation
            else:
                old_cache_revision = self._cache_revision
                cache_invalidated = (
                    self._cache is not None and self._cache_revision != revision
                )
        if not cache_hit:
            # Only a cache miss pays for the full consistent ledger read.
            state = self.ledger.read_consistent_state()
            with self._lock:
                if self._cache is not None and self._cache_revision == state["revision"]:
                    base = copy.deepcopy(self._cache)
                    cache_hit = True
                    cache_replaced = False
                    cache_invalidated = False
                    old_cache_revision = None
                    generation = self._generation
                else:
                    old_cache_revision = self._cache_revision
                    cache_invalidated = (
                        self._cache is not None and self._cache_revision != state["revision"]
                    )
                    base = self._build_view(state)
                    self._cache = copy.deepcopy(base)
                    self._cache_revision = state["revision"]
                    self._generation += 1
                    generation = self._generation
        ended_ns = mono_ns()
        ended_ms = ended_ns // 1_000_000
        exact_elapsed_ns = ended_ns - started_ns
        exceeded = exact_elapsed_ns > self.query_budget_ms * 1_000_000
        latency = {
            "started_mono_ms": started_ms,
            "ended_mono_ms": ended_ms,
            "started_mono_ns": started_ns,
            "ended_mono_ns": ended_ns,
            "elapsed_fixed": fixed_ms_from_ns(exact_elapsed_ns),
            "fixed_scale": 1000,
            "quantization_error_upper_bound_ms": 0.001,
            "budget_ms": self.query_budget_ms,
            "exceeded_budget": exceeded,
            "clock_basis": CLIENT_CLOCK_BASIS,
        }
        event_payload: dict[str, Any] = {}
        if exceeded:
            event_payload = self.logger.emit(
                "query_latency_budget_exceeded",
                code=E_LATENCY,
                started_mono_ms=started_ms,
                ended_mono_ms=ended_ms,
                started_mono_ns=started_ns,
                ended_mono_ns=ended_ns,
                elapsed_fixed=latency["elapsed_fixed"],
                fixed_scale=1000,
                budget_ms=self.query_budget_ms,
                data_version=base["data_version"],
                cache_hit=cache_hit,
            )
        self.ledger.record_interaction(
            kind="query",
            task_id=None,
            cluster_id=None,
            config_version=None,
            started_mono_ms=started_ms,
            ended_mono_ms=ended_ms,
            started_mono_ns=started_ns,
            ended_mono_ns=ended_ns,
            data_version=base["data_version"],
            status="ok",
            business_code=E_LATENCY if exceeded else None,
            exceeded_budget=exceeded,
            detail={
                "cache_hit": cache_hit,
                "cache_replaced": cache_replaced,
                "view_id": base["view_id"],
                "budget_ms": self.query_budget_ms,
            },
        )
        result = copy.deepcopy(base)
        result.update(
            {
                "cache_hit": cache_hit,
                "cache_replaced": cache_replaced,
                "cache_invalidation": (
                    {
                        "cache_stale_marked": True,
                        "old_revision": old_cache_revision,
                        "new_revision": base["ledger_revision"],
                        "replacement_mode": "whole-view",
                    }
                    if cache_invalidated
                    else None
                ),
                "view_generation": generation,
                "query_latency": latency,
                "latency_budget_exceeded": exceeded,
                "latency_event": event_payload or None,
            }
        )
        return result

    def latency_stats(self) -> dict[str, Any]:
        interactions = self.ledger.fetch_interactions()
        current = [row for row in interactions if not row.get("is_stale", False)]
        return {
            "current": compute_latency_stats(current),
            "history": compute_latency_stats(interactions),
            "stale_interaction_count": len(interactions) - len(current),
            "caliber_version": self.ledger.latency_caliber_version(),
        }
