"""Parse and validate the three JSON samples (services, nodes, trace).

Validation happens before any persistent state is created; invalid input
raises GatewayError with an E_xxx code and nothing is written to disk.
"""
import json
import math
import os

from . import errors
from .errors import GatewayError
from .models import (Arrival, EngineConfig, FaultSpec, NodeSpec, ServiceSpec,
                     Trace, TraceWindow)

_FAULT_KINDS = ("conn_refused", "budget_exceeded", "digest_mismatch")


def _is_pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _is_nonneg_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_nonneg_finite(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v >= 0)


def _read_json(path, code):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise GatewayError(code, "sample file not found", path=path)
    except json.JSONDecodeError as exc:
        raise GatewayError(code, "sample file is not valid JSON",
                           path=path, detail=str(exc))


def _require(cond, code, message, **fields):
    if not cond:
        raise GatewayError(code, message, **fields)


def load_services(path):
    raw = _read_json(path, errors.E_SCHEMA_SERVICE)
    items = raw.get("services") if isinstance(raw, dict) else None
    _require(isinstance(items, list) and items,
             errors.E_SCHEMA_SERVICE, "services must be a non-empty list")
    services = {}
    for item in items:
        _require(isinstance(item, dict), errors.E_SCHEMA_SERVICE,
                 "service entry must be an object")
        name = item.get("name")
        _require(isinstance(name, str) and name,
                 errors.E_SCHEMA_SERVICE, "service name must be a string")
        _require(name not in services, errors.E_DUPLICATE_SERVICE,
                 "duplicate service name", service=name)
        for key in ("input_tokens", "output_tokens", "compute_units",
                    "vram_mib", "batch_window_ms"):
            _require(_is_pos_int(item.get(key)), errors.E_SCHEMA_SERVICE,
                     f"{key} must be a positive integer",
                     service=name, value=item.get(key))
        _require(_is_nonneg_int(item.get("cold_start_ms")),
                 errors.E_SCHEMA_SERVICE,
                 "cold_start_ms must be a non-negative integer", service=name)
        _require(_is_pos_int(item.get("min_replicas"))
                 and _is_pos_int(item.get("max_replicas")),
                 errors.E_SCHEMA_SERVICE,
                 "min_replicas/max_replicas must be positive integers",
                 service=name)
        _require(item["min_replicas"] <= item["max_replicas"],
                 errors.E_SCHEMA_SERVICE,
                 "min_replicas must not exceed max_replicas", service=name,
                 min_replicas=item["min_replicas"],
                 max_replicas=item["max_replicas"])
        domain = item.get("topology_domain", "any")
        _require(isinstance(domain, str) and domain,
                 errors.E_SCHEMA_SERVICE,
                 "topology_domain must be a string", service=name)
        services[name] = ServiceSpec(
            name=name,
            model_version=str(item.get("model_version", "")),
            input_tokens=item["input_tokens"],
            output_tokens=item["output_tokens"],
            compute_units=item["compute_units"],
            vram_mib=item["vram_mib"],
            min_replicas=item["min_replicas"],
            max_replicas=item["max_replicas"],
            batch_window_ms=item["batch_window_ms"],
            cold_start_ms=item["cold_start_ms"],
            topology_domain=domain,
        )
    return services


def load_nodes(path):
    raw = _read_json(path, errors.E_SCHEMA_NODE)
    items = raw.get("nodes") if isinstance(raw, dict) else None
    _require(isinstance(items, list) and items,
             errors.E_SCHEMA_NODE, "nodes must be a non-empty list")
    nodes = {}
    for item in items:
        _require(isinstance(item, dict), errors.E_SCHEMA_NODE,
                 "node entry must be an object")
        name = item.get("name")
        _require(isinstance(name, str) and name,
                 errors.E_SCHEMA_NODE, "node name must be a string")
        _require(name not in nodes, errors.E_DUPLICATE_NODE,
                 "duplicate node name", node=name)
        for key in ("gpu_cards", "vram_per_card_mib", "max_instances"):
            _require(_is_pos_int(item.get(key)), errors.E_SCHEMA_NODE,
                     f"{key} must be a positive integer",
                     node=name, value=item.get(key))
        domain = item.get("topology_domain")
        _require(isinstance(domain, str) and domain,
                 errors.E_SCHEMA_NODE,
                 "topology_domain must be a string", node=name)
        nodes[name] = NodeSpec(
            name=name,
            gpu_cards=item["gpu_cards"],
            vram_per_card_mib=item["vram_per_card_mib"],
            max_instances=item["max_instances"],
            topology_domain=domain,
        )
    return nodes


def _load_fault(raw, service, window_index):
    if raw is None:
        return None
    _require(isinstance(raw, dict), errors.E_SCHEMA_TRACE,
             "fault must be an object", service=service, window=window_index)
    kind = raw.get("kind")
    _require(kind in _FAULT_KINDS, errors.E_SCHEMA_TRACE,
             "fault kind is not supported", service=service,
             window=window_index, kind=kind)
    every = raw.get("every")
    _require(_is_pos_int(every), errors.E_SCHEMA_TRACE,
             "fault every must be a positive integer", service=service,
             window=window_index)
    transient = raw.get("transient", False)
    _require(isinstance(transient, bool), errors.E_SCHEMA_TRACE,
             "fault transient must be a boolean", service=service,
             window=window_index)
    return FaultSpec(kind=kind, every=every, transient=transient)


def load_trace(path, services):
    raw = _read_json(path, errors.E_SCHEMA_TRACE)
    _require(isinstance(raw, dict), errors.E_SCHEMA_TRACE,
             "trace must be an object")
    cfg_raw = raw.get("config")
    _require(isinstance(cfg_raw, dict), errors.E_SCHEMA_TRACE,
             "trace.config must be an object")
    _require(_is_nonneg_finite(cfg_raw.get("switch_load_threshold"))
             and cfg_raw["switch_load_threshold"] > 0,
             errors.E_SCHEMA_TRACE,
             "switch_load_threshold must be a positive finite number")
    for key in ("round_time_budget_ms", "per_request_budget_ms",
                "retry_backoff_base_ms", "per_service_concurrency_cap",
                "batch_overhead_ms"):
        _require(_is_pos_int(cfg_raw.get(key)), errors.E_SCHEMA_TRACE,
                 f"config.{key} must be a positive integer")
    _require(_is_nonneg_int(cfg_raw.get("clock_rollback_threshold_ms")),
             errors.E_SCHEMA_TRACE,
             "clock_rollback_threshold_ms must be a non-negative integer")
    _require(_is_nonneg_int(cfg_raw.get("max_retries")),
             errors.E_SCHEMA_TRACE,
             "max_retries must be a non-negative integer")
    config = EngineConfig(
        switch_load_threshold=float(cfg_raw["switch_load_threshold"]),
        round_time_budget_ms=cfg_raw["round_time_budget_ms"],
        per_request_budget_ms=cfg_raw["per_request_budget_ms"],
        clock_rollback_threshold_ms=cfg_raw["clock_rollback_threshold_ms"],
        max_retries=cfg_raw["max_retries"],
        retry_backoff_base_ms=cfg_raw["retry_backoff_base_ms"],
        per_service_concurrency_cap=cfg_raw["per_service_concurrency_cap"],
        batch_overhead_ms=cfg_raw["batch_overhead_ms"],
    )
    windows_raw = raw.get("windows")
    _require(isinstance(windows_raw, list) and windows_raw,
             errors.E_SCHEMA_TRACE, "windows must be a non-empty list")
    windows = []
    prev_start = -1
    for pos, w in enumerate(windows_raw):
        _require(isinstance(w, dict), errors.E_SCHEMA_TRACE,
                 "window must be an object", position=pos)
        index = w.get("index")
        _require(_is_nonneg_int(index), errors.E_SCHEMA_TRACE,
                 "window index must be a non-negative integer", position=pos)
        _require(index == len(windows), errors.E_SCHEMA_TRACE,
                 "window indexes must be consecutive from 0",
                 window=index, expected=len(windows))
        _require(_is_nonneg_int(w.get("start_ms"))
                 and _is_pos_int(w.get("window_ms")),
                 errors.E_SCHEMA_TRACE,
                 "start_ms/window_ms must be non-negative/positive integers",
                 window=index)
        _require(w["start_ms"] >= prev_start, errors.E_SCHEMA_TRACE,
                 "window start_ms must be non-decreasing", window=index)
        prev_start = w["start_ms"]
        arrivals_raw = w.get("arrivals")
        _require(isinstance(arrivals_raw, list) and arrivals_raw,
                 errors.E_SCHEMA_TRACE,
                 "window arrivals must be a non-empty list", window=index)
        arrivals = []
        seen = set()
        for a in arrivals_raw:
            _require(isinstance(a, dict), errors.E_SCHEMA_TRACE,
                     "arrival must be an object", window=index)
            svc = a.get("service")
            _require(svc in services, errors.E_UNKNOWN_SERVICE,
                     "arrival references unknown service",
                     window=index, service=svc)
            _require(svc not in seen, errors.E_SCHEMA_TRACE,
                     "duplicate arrival for service in window",
                     window=index, service=svc)
            seen.add(svc)
            _require(_is_nonneg_finite(a.get("arrival_rate")),
                     errors.E_SCHEMA_TRACE,
                     "arrival_rate must be a non-negative finite number",
                     window=index, service=svc)
            _require(_is_nonneg_finite(a.get("avg_seq_len")),
                     errors.E_SCHEMA_TRACE,
                     "avg_seq_len must be a non-negative finite number",
                     window=index, service=svc)
            _require(_is_nonneg_finite(a.get("expected_concurrency")),
                     errors.E_SCHEMA_TRACE,
                     "expected_concurrency must be non-negative finite",
                     window=index, service=svc)
            _require(_is_pos_int(a.get("token_budget")),
                     errors.E_SCHEMA_TRACE,
                     "token_budget must be a positive integer",
                     window=index, service=svc)
            arrivals.append(Arrival(
                service=svc,
                arrival_rate=float(a["arrival_rate"]),
                avg_seq_len=float(a["avg_seq_len"]),
                expected_concurrency=float(a["expected_concurrency"]),
                token_budget=a["token_budget"],
                fault=_load_fault(a.get("fault"), svc, index),
            ))
        arrivals.sort(key=lambda x: x.service)
        windows.append(TraceWindow(
            index=index, start_ms=w["start_ms"], window_ms=w["window_ms"],
            arrivals=tuple(arrivals)))
    return Trace(config=config, windows=tuple(windows))


def load_samples(samples_dir):
    """Load and validate all three samples. Raises GatewayError on any
    violation; callers must not create persistent state before this
    returns successfully."""
    services = load_services(os.path.join(samples_dir, "services.json"))
    nodes = load_nodes(os.path.join(samples_dir, "nodes.json"))
    trace = load_trace(os.path.join(samples_dir, "trace.json"), services)
    return services, nodes, trace
