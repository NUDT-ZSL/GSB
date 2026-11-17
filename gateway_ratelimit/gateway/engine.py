"""Gateway engine: authenticate first, then run all three limiter paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .auth import Authenticator
from .errors import E_AUTH, E_LIMIT, E_OK
from .limiters import LimiterSet
from .models import (
    LIMIT_PATHS,
    PATH_ORDER,
    Request,
    Route,
    TokenTable,
)
from .selector import Selection, select_path


@dataclass
class VersionState:
    policy_version: tuple[int, int]
    routes: list[Route]
    limiter_sets: dict[tuple[str, str], LimiterSet]
    authenticator: Authenticator
    trace: list[dict[str, Any]]
    cache_cleared: int = 0


class GatewayEngine:
    def __init__(self, routes: list[Route], routes_version: int, token_table: TokenTable) -> None:
        self.routes_by_name = {route.name: route for route in routes}
        self.current_version = (routes_version, token_table.version)
        self.authenticator = Authenticator(token_table, routes_version)
        self.limiter_sets: dict[tuple[str, str], LimiterSet] = {}
        self.trace: list[dict[str, Any]] = []
        self.history: list[VersionState] = []
        self.routes = routes
        self._observed_max_log: dict[str, int] = {route.name: 0 for route in routes}
        self._client_seen: dict[str, set[str]] = {route.name: set() for route in routes}
        self._span: dict[str, tuple[int, int]] = {route.name: (0, 0) for route in routes}

    # ------------------------------------------------------------------ policy
    def replace_policy(
        self, routes: list[Route], routes_version: int, token_table: TokenTable
    ) -> dict[str, Any]:
        new_version = (routes_version, token_table.version)
        if new_version <= self.current_version:
            raise ValueError(
                f"policy version must be monotonic: {new_version} <= {self.current_version}"
            )
        old_by_name = {old.name: old for old in self.routes}
        new_by_name = {new.name: new for new in routes}
        affected = set(old_by_name) | set(new_by_name)
        version_changed = new_version[0] != self.current_version[0]
        affected_routes = sorted(
            name
            for name in affected
            if old_by_name.get(name) != new_by_name.get(name)
            or (version_changed and name in new_by_name)
        )
        self.history.append(
            VersionState(
                policy_version=self.current_version,
                routes=self.routes,
                limiter_sets=self.limiter_sets,
                authenticator=self.authenticator,
                trace=self.trace,
                cache_cleared=self.authenticator.cache_size(),
            )
        )
        cleared = self.authenticator.replace_policy(token_table, routes_version)
        self.routes = routes
        self.routes_by_name = {route.name: route for route in routes}
        self.limiter_sets = {}
        self.trace = []
        self.current_version = new_version
        self._observed_max_log = {route.name: 0 for route in routes}
        self._client_seen = {route.name: set() for route in routes}
        self._span = {route.name: (0, 0) for route in routes}
        return {
            "new_version": list(new_version),
            "replaced_version": list(self.history[-1].policy_version),
            "affected_routes": affected_routes,
            "cache_entries_cleared": cleared,
            "limiter_state": "wholesale_replaced",
        }

    # ----------------------------------------------------------------- request
    def process(self, request: Request) -> dict[str, Any]:
        route = self.routes_by_name[request.route]
        auth = self.authenticator.verify(route, request)
        row: dict[str, Any] = {
            "seq": request.seq,
            "source_line": request.source_line,
            "route": request.route,
            "client_id": request.client_id,
            "ts_ms": request.ts_ms,
            "policy_version": list(self.current_version),
            "auth": {"code": auth.code, "reason": auth.reason, "cache_hit": auth.cache_hit},
            "paths": {},
        }
        if not auth.ok:
            # Auth failures never consume rate-limit quota.
            for path in LIMIT_PATHS:
                row["paths"][path] = {
                    "allow": False,
                    "remaining": None,
                    "reason": "skipped_after_auth_failure",
                    "detail": "",
                }
            row["decision"] = E_AUTH
            row["remaining_quota"] = None
        else:
            key = (request.route, request.client_id)
            limiter_set = self.limiter_sets.get(key)
            if limiter_set is None:
                limiter_set = LimiterSet(route)
                self.limiter_sets[key] = limiter_set
            decisions = limiter_set.decide(request.ts_ms)
            for path in sorted(decisions, key=lambda name: PATH_ORDER[name]):
                decision = decisions[path]
                row["paths"][path] = {
                    "allow": decision.allow,
                    "remaining": decision.remaining,
                    "reason": decision.reason,
                    "detail": decision.detail,
                }
            selection = self.selection_for(route)
            effective = decisions[selection.path]
            row["selected_path"] = selection.path
            row["decision"] = E_OK if effective.allow else E_LIMIT
            row["remaining_quota"] = effective.remaining
            self._track_scale(route, request, limiter_set)
        self.trace.append(row)
        return row

    def _track_scale(self, route: Route, request: Request, limiter_set: LimiterSet) -> None:
        self._client_seen[route.name].add(request.client_id)
        start, _ = self._span[route.name]
        if not self._span[route.name][1]:
            start = request.ts_ms
        self._span[route.name] = (start, request.ts_ms)
        self._observed_max_log[route.name] = max(
            self._observed_max_log[route.name], len(limiter_set.log.log)
        )

    # --------------------------------------------------------------- selection
    def selection_for(self, route: Route) -> Selection:
        start, end = self._span[route.name]
        span_ms = max(end - start, route.window_ms)
        return select_path(
            route,
            client_count=max(len(self._client_seen[route.name]), 1),
            span_ms=span_ms,
            observed_max_log=self._observed_max_log[route.name],
        )

    def selection_report(self) -> list[dict[str, Any]]:
        report = []
        for route in sorted(self.routes, key=lambda item: item.name):
            selection = self.selection_for(route)
            report.append(
                {
                    "route": route.name,
                    "selected_path": selection.path,
                    "tie_broken": selection.tie_broken,
                    "reasons": selection.reasons,
                    "scale": selection.scale,
                    "workload_estimates": selection.estimates,
                }
            )
        return report

    # ------------------------------------------------------------- summaries
    def state_summary(self) -> dict[str, Any]:
        return {
            "policy_version": list(self.current_version),
            "limiter_sets": {
                f"{route_name}|{client_id}": limiter_set.snapshot()
                for (route_name, client_id), limiter_set in sorted(self.limiter_sets.items())
            },
            "auth_cache": self.authenticator.cache_snapshot(),
        }

    def restore_state(self, summary: dict[str, Any]) -> None:
        self.limiter_sets = {}
        for composite, snapshot in summary["limiter_sets"].items():
            route_name, client_id = composite.split("|", 1)
            route = self.routes_by_name[route_name]
            limiter_set = LimiterSet(route)
            limiter_set.restore(snapshot)
            self.limiter_sets[(route_name, client_id)] = limiter_set

    def decision_codes(self) -> dict[str, int]:
        counts = {E_OK: 0, E_AUTH: 0, E_LIMIT: 0}
        for row in self.trace:
            counts[row["decision"]] = counts.get(row["decision"], 0) + 1
        return counts

    def versioned_summary(self) -> dict[str, Any]:
        """Current-version totals plus stale rows retained for audit only.

        Rows whose policy version differs from the active version are flagged
        stale and excluded from the current summary aggregates.
        """
        current = list(self.current_version)
        current_rows = [row for row in self.trace if row["policy_version"] == current]
        stale_rows = []
        for state in self.history:
            for row in state.trace:
                if row["policy_version"] != current:
                    marked = dict(row)
                    marked["stale"] = True
                    stale_rows.append(marked)
        current_counts = {E_OK: 0, E_AUTH: 0, E_LIMIT: 0}
        for row in current_rows:
            current_counts[row["decision"]] = current_counts.get(row["decision"], 0) + 1
        return {
            "current_policy_version": current,
            "current_decision_counts": current_counts,
            "current_request_count": len(current_rows),
            "stale_request_count": len(stale_rows),
            "stale_versions": sorted(
                {tuple(row["policy_version"]) for row in stale_rows},
                key=lambda item: list(item),
            ),
            "stale_rows_excluded_from_summary": True,
        }
