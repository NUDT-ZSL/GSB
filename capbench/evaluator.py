from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .probe import Probe
from .scenario import BatchPlan, GradientSpec, Scenario, Tenant
from .storage import Store
from .workload import build_window_requests, summarize_window


@dataclass(frozen=True)
class Candidate:
    tenant: str
    gradient: GradientSpec
    plan: BatchPlan
    tenant_config: Tenant
    window_index: int


def _candidate_rows(
    gradients: list[dict[str, Any]],
    windows: list[dict[str, Any]],
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    result: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in windows:
        result.setdefault(
            (row["tenant"], int(row["gradient_index"])),
            [],
        ).append(row)
    for values in result.values():
        values.sort(key=lambda item: int(item["window_index"]))
    return result


def select_candidates(store: Store, scenario: Scenario, run_id: str) -> list[Candidate]:
    gradient_rows = store.gradients(run_id)
    window_rows = store.windows(run_id)
    windows_by_gradient = _candidate_rows(gradient_rows, window_rows)
    rows_by_tenant: dict[str, list[dict[str, Any]]] = {}
    for row in gradient_rows:
        rows_by_tenant.setdefault(row["tenant"], []).append(row)
    for rows in rows_by_tenant.values():
        rows.sort(key=lambda item: int(item["gradient_index"]))

    candidates: list[Candidate] = []
    plans = scenario.plan_map
    tenant_map = scenario.tenant_map
    for tenant_name in sorted(rows_by_tenant):
        plan = plans[tenant_name]
        tenant_config = tenant_map[tenant_name]
        gradients_by_index = {item.index: item for item in plan.gradients}
        active_gradients = 0
        for row in rows_by_tenant[tenant_name]:
            states = [
                window["state"]
                for window in windows_by_gradient.get(
                    (tenant_name, int(row["gradient_index"])),
                    [],
                )
            ]
            if "running" in states:
                active_gradients += 1
                continue
            if row["state"] in (
                "terminated",
                "skipped_after_knee",
                "budget_abandoned",
                "failed",
                "incomplete",
            ):
                continue
            pending = [
                window
                for window in windows_by_gradient.get(
                    (tenant_name, int(row["gradient_index"])),
                    [],
                )
                if window["state"] == "pending"
            ]
            if not pending:
                continue
            if active_gradients >= tenant_config.max_parallel_gradients:
                continue
            gradient = gradients_by_index.get(int(row["gradient_index"]))
            if gradient is None:
                continue
            candidates.append(
                Candidate(
                    tenant=tenant_name,
                    gradient=gradient,
                    plan=plan,
                    tenant_config=tenant_config,
                    window_index=int(pending[0]["window_index"]),
                )
            )
            active_gradients += 1
    candidates.sort(
        key=lambda item: (item.tenant, item.gradient.index, item.window_index)
    )
    return candidates


class _CrashCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0

    def increment(self) -> int:
        with self._lock:
            self.count += 1
            return self.count


def _execute_candidate(
    *,
    store: Store,
    scenario: Scenario,
    run_id: str,
    seed: int,
    probe: Probe,
    candidate: Candidate,
    owner_token: str,
    crash_after_commits: int | None,
    commit_counter: _CrashCounter,
) -> dict[str, Any]:
    windows = store.windows(run_id)
    record = next(
        item
        for item in windows
        if item["tenant"] == candidate.tenant
        and int(item["gradient_index"]) == candidate.gradient.index
        and int(item["window_index"]) == candidate.window_index
    )
    requests = build_window_requests(
        candidate.gradient,
        candidate.window_index,
        scenario.clock,
        int(record["request_seq_start"]),
    )
    if len(requests) != int(record["planned_requests"]):
        raise RuntimeError(
            "deterministic request count changed after unit materialization"
        )
    service = scenario.tenant_service_map[candidate.tenant]
    outcomes = probe.probe_window(
        service=service,
        tenant=candidate.tenant,
        gradient=candidate.gradient,
        window_index=candidate.window_index,
        requests=requests,
        expected_status=service.expected_status,
        timeout_ms=candidate.gradient.request_timeout_ms,
        seed=seed,
        run_id=run_id,
    )
    summary = summarize_window(
        tenant=candidate.tenant,
        gradient=candidate.gradient,
        window_index=candidate.window_index,
        requests=requests,
        outcomes=outcomes,
        expected_status=service.expected_status,
    )
    result = store.commit_window(
        run_id=run_id,
        tenant=candidate.tenant,
        gradient_index=candidate.gradient.index,
        window_index=candidate.window_index,
        summary=summary,
        owner_token=owner_token,
        slo=candidate.tenant_config.slo,
        allow_early_termination=candidate.plan.allow_early_termination,
    )
    if crash_after_commits is not None:
        if commit_counter.increment() >= crash_after_commits:
            os._exit(137)
    return result


def _remaining_budget_cannot_fit(store: Store, run_id: str) -> bool:
    meta = store.meta(run_id)
    used = store.budget_used(run_id)
    remaining = int(meta["global_budget_ms"]) - used
    pending_spans = [
        int(row["clock_end_ms"]) - int(row["clock_start_ms"])
        for row in store.windows(run_id)
        if row["state"] == "pending"
    ]
    return bool(pending_spans) and min(pending_spans) > remaining


def run_evaluation(
    *,
    store: Store,
    scenario: Scenario,
    run_id: str,
    seed: int,
    probe: Probe,
    workers: int = 4,
    crash_after_commits: int | None = None,
    crash_after_claims: int | None = None,
    wait_for_peers: bool = True,
    peer_wait_timeout_s: float = 300.0,
) -> dict[str, Any]:
    if workers <= 0:
        raise ValueError("workers must be positive")
    recovered = store.recover_orphans(run_id)
    commit_counter = _CrashCounter()
    claim_count = 0
    peer_wait_started = time.monotonic()

    while True:
        candidates = select_candidates(store, scenario, run_id)
        claimed: list[tuple[Candidate, str]] = []
        budget_skips = 0
        busy = 0
        for candidate in candidates:
            token = store.new_owner_token()
            claim = store.reserve_and_claim(
                run_id=run_id,
                tenant=candidate.tenant,
                gradient_index=candidate.gradient.index,
                window_index=candidate.window_index,
                owner_token=token,
                max_parallel_gradients=candidate.tenant_config.max_parallel_gradients,
            )
            if claim.status == "claimed":
                claimed.append((candidate, token))
                claim_count += 1
                if (
                    crash_after_claims is not None
                    and claim_count >= crash_after_claims
                ):
                    os._exit(137)
            elif claim.status == "budget_unstarted":
                budget_skips += 1
            elif claim.status in ("busy", "tenant_budget_busy", "out_of_order"):
                busy += 1

        if claimed:
            futures: dict[Future[dict[str, Any]], tuple[Candidate, str]] = {}
            with ThreadPoolExecutor(
                max_workers=max(1, min(workers, len(claimed))),
                thread_name_prefix="capbench-window",
            ) as executor:
                for candidate, token in claimed:
                    future = executor.submit(
                        _execute_candidate,
                        store=store,
                        scenario=scenario,
                        run_id=run_id,
                        seed=seed,
                        probe=probe,
                        candidate=candidate,
                        owner_token=token,
                        crash_after_commits=crash_after_commits,
                        commit_counter=commit_counter,
                    )
                    futures[future] = (candidate, token)
                for future in as_completed(futures):
                    candidate, token = futures[future]
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001 - persisted as unit failure
                        store.fail_window(
                            run_id=run_id,
                            tenant=candidate.tenant,
                            gradient_index=candidate.gradient.index,
                            window_index=candidate.window_index,
                            owner_token=token,
                            error=f"{type(exc).__name__}: {exc}",
                        )
            continue

        pending_count = sum(
            1 for row in store.windows(run_id) if row["state"] == "pending"
        )
        running_count = sum(
            1 for row in store.windows(run_id) if row["state"] == "running"
        )
        if running_count:
            if wait_for_peers:
                if time.monotonic() - peer_wait_started > peer_wait_timeout_s:
                    break
                time.sleep(0.05)
                continue
            break
        if pending_count == 0:
            break
        if budget_skips and _remaining_budget_cannot_fit(store, run_id):
            store.mark_remaining_budget_unstarted(
                run_id,
                reason="global logical time budget cannot fit another window",
            )
            continue
        if busy and wait_for_peers:
            if time.monotonic() - peer_wait_started > peer_wait_timeout_s:
                break
            time.sleep(0.05)
            continue
        break

    pending_count = sum(
        1 for row in store.windows(run_id) if row["state"] == "pending"
    )
    if pending_count:
        if _remaining_budget_cannot_fit(store, run_id):
            store.mark_remaining_budget_unstarted(
                run_id,
                reason="global logical time budget exhausted",
            )
        else:
            store.mark_remaining_not_started(
                run_id,
                reason="blocked by a non-startable unit or terminal tenant state",
            )
    status = store.finalize_run(run_id)
    return {
        "run_id": run_id,
        "status": status,
        "recovered_orphans": recovered,
        "claimed_units": claim_count,
    }

