"""Catalog and strategy loading plus explicit validation."""

import json

from .errors import BatchError

FAILURE_MODES = ("none", "timeout", "http_5xx", "conn_refused", "rate_limited")
PERCENTILE_CHOICES = ("p50", "p95", "p99")


def _load_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise BatchError("E_INPUT_NOT_FOUND", f"{what} not found: {path}", path=path)
    except json.JSONDecodeError as exc:
        raise BatchError("E_INVALID_JSON", f"{what} is not valid JSON: {exc}", path=path)


class Catalog:
    def __init__(self, services):
        # services: {service_id: {"name","team","interfaces":[...],"depends_on":[...]}}
        self.services = services
        self.dependents = {sid: set() for sid in services}
        for sid, spec in services.items():
            for dep in spec["depends_on"]:
                self.dependents[dep].add(sid)

    def ancestors(self, service):
        seen = set()
        stack = [service]
        while stack:
            cur = stack.pop()
            for dep in self.services[cur]["depends_on"]:
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
        return seen

    def distance(self, src, dst):
        """Hop distance from src to dst along depends_on edges; None if unreachable."""
        if src == dst:
            return 0
        frontier = {src}
        seen = {src}
        hops = 0
        while frontier:
            hops += 1
            nxt = set()
            for cur in frontier:
                for dep in self.services[cur]["depends_on"]:
                    if dep == dst:
                        return hops
                    if dep not in seen:
                        seen.add(dep)
                        nxt.add(dep)
            frontier = nxt
        return None

    def dependents_with_distance(self, service):
        """BFS over reverse dependency edges -> {service: distance}."""
        dist = {}
        frontier = {service}
        hops = 0
        while frontier:
            hops += 1
            nxt = set()
            for cur in frontier:
                for dep in sorted(self.dependents[cur]):
                    if dep not in dist and dep != service:
                        dist[dep] = hops
                        nxt.add(dep)
            frontier = nxt
        return dist


def load_catalog(path):
    raw = _load_json(path, "catalog")
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), list):
        raise BatchError("E_CATALOG_INVALID", "catalog must contain a 'services' list")
    services = {}
    for idx, entry in enumerate(raw["services"]):
        ctx = {"service_index": idx}
        if not isinstance(entry, dict):
            raise BatchError("E_CATALOG_INVALID", "service entry must be an object", **ctx)
        sid = entry.get("service_id")
        if not isinstance(sid, str) or not sid:
            raise BatchError("E_CATALOG_INVALID", "service entry missing service_id", **ctx)
        if sid in services:
            raise BatchError("E_CATALOG_INVALID", f"duplicate service_id {sid}", **ctx)
        interfaces = entry.get("interfaces")
        if not isinstance(interfaces, list) or not interfaces or \
                any(not isinstance(i, str) or not i for i in interfaces):
            raise BatchError("E_CATALOG_INVALID", f"service {sid} has invalid interfaces", **ctx)
        if len(set(interfaces)) != len(interfaces):
            raise BatchError("E_CATALOG_INVALID", f"service {sid} has duplicate interfaces", **ctx)
        depends_on = entry.get("depends_on", [])
        if not isinstance(depends_on, list) or any(not isinstance(d, str) for d in depends_on):
            raise BatchError("E_CATALOG_INVALID", f"service {sid} has invalid depends_on", **ctx)
        if sid in depends_on:
            raise BatchError("E_CATALOG_INVALID", f"service {sid} depends on itself", **ctx)
        team = entry.get("team")
        if not isinstance(team, str) or not team:
            raise BatchError("E_CATALOG_INVALID", f"service {sid} missing team", **ctx)
        services[sid] = {
            "name": entry.get("name", sid),
            "team": team,
            "interfaces": list(interfaces),
            "depends_on": list(depends_on),
        }
    if not services:
        raise BatchError("E_CATALOG_INVALID", "catalog has no services")
    for sid, spec in services.items():
        for dep in spec["depends_on"]:
            if dep not in services:
                raise BatchError(
                    "E_CATALOG_INVALID",
                    f"service {sid} depends on unknown service {dep}",
                    service=sid, dependency=dep,
                )
    return Catalog(services)


def _require(cond, code, message, **details):
    if not cond:
        raise BatchError(code, message, **details)


def load_strategy(path):
    raw = _load_json(path, "strategy")
    if not isinstance(raw, dict):
        raise BatchError("E_STRATEGY_INVALID", "strategy must be a JSON object")

    def need_int(key, minimum):
        val = raw.get(key)
        _require(isinstance(val, int) and not isinstance(val, bool) and val >= minimum,
                 "E_STRATEGY_INVALID", f"strategy field {key} must be an integer >= {minimum}",
                 field=key, value=val)
        return val

    def need_float(key, lo, hi):
        val = raw.get(key)
        _require(isinstance(val, (int, float)) and not isinstance(val, bool) and lo < val <= hi,
                 "E_STRATEGY_INVALID", f"strategy field {key} must be in ({lo}, {hi}]",
                 field=key, value=val)
        return float(val)

    strategy = {
        "parameter_version": raw.get("parameter_version"),
        "probe_period_seconds": need_int("probe_period_seconds", 1),
        "tick_count": need_int("tick_count", 1),
        "node_count": need_int("node_count", 1),
        "window_length": need_int("window_length", 1),
        "consecutive_failure_threshold": need_int("consecutive_failure_threshold", 1),
        "latency_consecutive_threshold": need_int("latency_consecutive_threshold", 1),
        "failure_rate_threshold": need_float("failure_rate_threshold", 0.0, 1.0),
        "latency_threshold_ms": need_float("latency_threshold_ms", 0.0, float("inf")),
        "latency_percentile": raw.get("latency_percentile"),
        "scoring_weights": raw.get("scoring_weights"),
    }
    _require(isinstance(strategy["parameter_version"], str) and strategy["parameter_version"],
             "E_STRATEGY_INVALID", "strategy missing parameter_version")
    _require(strategy["latency_percentile"] in PERCENTILE_CHOICES,
             "E_STRATEGY_INVALID", "latency_percentile must be one of p50/p95/p99",
             value=strategy["latency_percentile"])
    window = strategy["window_length"]
    _require(strategy["consecutive_failure_threshold"] <= window,
             "E_THRESHOLD_EXCEEDS_WINDOW",
             "consecutive_failure_threshold exceeds window_length",
             consecutive_failure_threshold=strategy["consecutive_failure_threshold"],
             window_length=window)
    _require(strategy["latency_consecutive_threshold"] <= window,
             "E_THRESHOLD_EXCEEDS_WINDOW",
             "latency_consecutive_threshold exceeds window_length",
             latency_consecutive_threshold=strategy["latency_consecutive_threshold"],
             window_length=window)
    weights = strategy["scoring_weights"]
    _require(isinstance(weights, dict), "E_STRATEGY_INVALID", "scoring_weights must be an object")
    cleaned = {}
    for key in ("time_overlap", "dependency_proximity", "failure_mode_match"):
        val = weights.get(key)
        _require(isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0,
                 "E_STRATEGY_INVALID", f"scoring weight {key} must be a non-negative number",
                 field=key, value=val)
        cleaned[key] = float(val)
    strategy["scoring_weights"] = cleaned
    return strategy
