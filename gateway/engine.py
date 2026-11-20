"""Routing engine: orchestrates windows, adjudication, placement and
dispatch, and commits each round atomically. Recovery is implicit: rounds
already committed are skipped, so an interrupted process replays only the
rounds that were never committed and never re-charges their budgets.
"""
import os

from . import placement, router, scheduler
from .clock import LogicalClock
from .upstream import SimulatedUpstream


class Engine:
    def __init__(self, services, nodes, trace, store, upstream=None,
                 force_mode=None, crash_after_requests=None):
        self.services = services
        self.nodes = nodes
        self.trace = trace
        self.store = store
        self.upstream = upstream if upstream is not None else SimulatedUpstream()
        self.force_mode = force_mode
        self.clock = LogicalClock(trace.config.clock_rollback_threshold_ms)
        self._crash_after = crash_after_requests

    def _crash_hook(self):
        if self._crash_after is None:
            return
        self._crash_after -= 1
        if self._crash_after < 0:
            # Simulate a hard kill mid-round, before the round commit.
            os._exit(3)

    def run(self):
        for window in self.trace.windows:
            if self.store.is_committed(window.index):
                continue
            self.clock.advance_to(window.start_ms)
            round_data = self._run_window(window)
            self.store.commit_round(window.index, round_data)
        return self.report()

    def _run_window(self, window):
        config = self.trace.config
        node_states = placement.fresh_node_states(self.nodes)
        round_budget = scheduler.RoundBudget(config.round_time_budget_ms)
        prev_nodes = self.store.placement_nodes_by_service(window.index - 1)
        decisions, placements, requests = [], [], []
        budgets, replica_busy = {}, {}
        for arrival in window.arrivals:
            service = self.services[arrival.service]
            decision = router.adjudicate_mode(
                arrival, service, window, config,
                force_mode=self.force_mode)
            decisions.append(decision)
            replica_count = placement.plan_replica_count(
                service, decision["demand_tokens"], window.window_ms)
            placement_result = placement.place_service(
                service, replica_count, node_states,
                prev_nodes.get(service.name, set()))
            placement_result = dict(placement_result,
                                    replica_count=replica_count)
            placements.append(placement_result)
            result = scheduler.dispatch_service_window(
                service, arrival, window, config, placement_result,
                decision["mode"], round_budget, self.upstream,
                crash_hook=self._crash_hook)
            requests.extend(result["requests"])
            budgets[service.name] = result["tokens_consumed"]
            replica_busy[service.name] = result["replica_busy_ms"]
        return {
            "version": window.index,
            "clock_ms": window.start_ms,
            "window_ms": window.window_ms,
            "decisions": decisions,
            "placements": placements,
            "requests": requests,
            "budgets": budgets,
            "replica_busy_ms": replica_busy,
            "node_states": [st.snapshot() for st in node_states.values()],
            "round_time_consumed_ms": round_budget.consumed_ms,
        }

    def report(self):
        rounds = self.store.committed_rounds()
        outcomes = {}
        total_tokens = 0
        for rnd in rounds:
            for req in rnd["requests"]:
                outcomes[req["outcome"]] = outcomes.get(req["outcome"], 0) + 1
            total_tokens += sum(rnd["budgets"].values())
        return {
            "schema": "gateway-run/v1",
            "rounds": rounds,
            "summary": {
                "rounds_committed": len(rounds),
                "requests_by_outcome": outcomes,
                "tokens_consumed": total_tokens,
                "clock_watermark_ms": rounds[-1]["clock_ms"] if rounds else 0,
            },
        }
