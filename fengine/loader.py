"""Parsing and validation of graph / events / config artifacts (requirement 1).

All validation failures raise BizError with a stable business error code and
a details dict that keeps the offending fields for diagnosis.
"""
import hashlib
import json

from . import errors
from .errors import BizError

OP_TYPES = ("aggregate", "filter", "window", "scale")
FILTER_OPS = ("gt", "lt", "ge", "le", "eq")
AGG_FUNCS = ("sum", "mean", "min", "max", "count")
WINDOW_FUNCS = ("sum", "mean")

CONFIG_SCHEMA = {
    "memory_budget_bytes": "pos_int",
    "op_memory_estimate_bytes": "pos_int",
    "parallel_slots": "pos_int",
    "round_time_budget_ms": "pos_int",
    "fixed_scale": "pos_int",
    "tolerance": "pos_num",
    "clock_step_ms": "pos_int",
    "window_ms": "pos_int",
}


def graph_hash(graph: dict) -> str:
    canonical = json.dumps(graph, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def event_digest(seq: int, ts_ms: int, features: dict) -> str:
    canonical = json.dumps(
        {"seq": seq, "ts_ms": ts_ms, "features": features},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate_graph(graph: dict) -> dict:
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise BizError(errors.E_GRAPH_EMPTY, "graph must be an object with a nodes list",
                       {"got_type": type(graph).__name__})
    nodes = graph["nodes"]
    if not nodes:
        raise BizError(errors.E_GRAPH_EMPTY, "graph has no nodes", {})

    seen = {}
    for node in nodes:
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            raise BizError(errors.E_OP_PARAM_MISMATCH, "node id must be a non-empty string",
                           {"node": node})
        if nid in seen:
            raise BizError(errors.E_GRAPH_DUPLICATE_NODE, "duplicate node id",
                           {"node_id": nid})
        seen[nid] = node

        op = node.get("op")
        if op not in OP_TYPES:
            raise BizError(errors.E_OP_TYPE_INVALID, "unknown operator type",
                           {"node_id": nid, "op": op, "legal": list(OP_TYPES)})
        deps = node.get("deps")
        if not isinstance(deps, list) or len(deps) > 1:
            raise BizError(errors.E_OP_PARAM_MISMATCH,
                           "deps must be a list of at most one upstream node",
                           {"node_id": nid, "deps": deps})
        params = node.get("params")
        if not isinstance(params, dict):
            raise BizError(errors.E_OP_PARAM_MISMATCH, "params must be an object",
                           {"node_id": nid})
        _validate_params(nid, op, params, node.get("output_type"), is_root=not deps)
        mem = node.get("memory_bytes")
        if mem is not None and (not isinstance(mem, int) or mem <= 0):
            raise BizError(errors.E_CONFIG_INVALID, "node memory_bytes must be a positive int",
                           {"node_id": nid, "memory_bytes": mem})

    for node in nodes:
        for dep in node["deps"]:
            if dep not in seen:
                raise BizError(errors.E_GRAPH_UNKNOWN_DEP, "dependency reference is not closed",
                               {"node_id": node["id"], "missing_dep": dep})

    _check_acyclic(nodes)
    return graph


def _validate_params(nid, op, params, output_type, is_root):
    def need(key, kinds):
        if key not in params:
            raise BizError(errors.E_OP_PARAM_MISMATCH, "missing required param",
                           {"node_id": nid, "missing": key})
        if not kinds(params[key]):
            raise BizError(errors.E_OP_PARAM_MISMATCH, "param has wrong type",
                           {"node_id": nid, "param": key, "value": params[key]})

    if is_root:
        need("column", lambda v: isinstance(v, str) and bool(v))
    if op == "filter":
        need("op", lambda v: v in FILTER_OPS)
        need("threshold", _is_number)
        expected_out = "stream"
    elif op == "scale":
        need("factor", _is_number)
        need("offset", _is_number)
        expected_out = "stream"
    elif op == "aggregate":
        need("func", lambda v: v in AGG_FUNCS)
        expected_out = "scalar"
    else:  # window
        need("size", lambda v: isinstance(v, int) and v > 0)
        need("func", lambda v: v in WINDOW_FUNCS)
        expected_out = "stream"
    if output_type != expected_out:
        raise BizError(errors.E_OP_OUTPUT_TYPE_MISMATCH,
                       "output_type does not match operator type",
                       {"node_id": nid, "op": op,
                        "expected": expected_out, "got": output_type})


def _check_acyclic(nodes):
    indeg = {n["id"]: 0 for n in nodes}
    children = {n["id"]: [] for n in nodes}
    for n in nodes:
        for d in n["deps"]:
            indeg[n["id"]] += 1
            children[d].append(n["id"])
    queue = sorted([nid for nid, dg in indeg.items() if dg == 0])
    visited = 0
    while queue:
        nid = queue.pop(0)
        visited += 1
        for c in sorted(children[nid]):
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    if visited != len(nodes):
        remaining = sorted(nid for nid, dg in indeg.items() if dg > 0)
        raise BizError(errors.E_GRAPH_CYCLE, "dependency graph has a cycle",
                       {"nodes_in_cycle": remaining})


def validate_events(events) -> list:
    if not isinstance(events, list) or not events:
        raise BizError(errors.E_EVENT_FIELD_INVALID, "events must be a non-empty list", {})
    prev_seq = None
    for ev in events:
        seq = ev.get("seq")
        ts = ev.get("ts_ms")
        feats = ev.get("features")
        digest = ev.get("digest")
        if not isinstance(seq, int) or not isinstance(ts, int) or ts < 0 \
                or not isinstance(feats, dict) \
                or not all(_is_number(v) for v in feats.values()):
            raise BizError(errors.E_EVENT_FIELD_INVALID, "event fields are invalid",
                           {"event": ev})
        if prev_seq is not None and seq <= prev_seq:
            raise BizError(errors.E_EVENT_SEQ_ORDER, "event seq must be strictly increasing",
                           {"prev_seq": prev_seq, "seq": seq})
        if event_digest(seq, ts, feats) != digest:
            raise BizError(errors.E_EVENT_DIGEST_MISMATCH, "event content digest mismatch",
                           {"seq": seq, "expected": event_digest(seq, ts, feats),
                            "got": digest})
        prev_seq = seq
    return events


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise BizError(errors.E_CONFIG_INVALID, "config must be an object", {})
    for key, kind in CONFIG_SCHEMA.items():
        if key not in config:
            raise BizError(errors.E_CONFIG_INVALID, "missing config key", {"missing": key})
        val = config[key]
        if kind == "pos_int" and (not isinstance(val, int) or isinstance(val, bool) or val <= 0):
            raise BizError(errors.E_CONFIG_INVALID, "config value must be a positive integer",
                           {"key": key, "value": val})
        if kind == "pos_num" and (not _is_number(val) or val <= 0):
            raise BizError(errors.E_CONFIG_INVALID, "config value must be a positive number",
                           {"key": key, "value": val})
    return config


def cross_validate(graph: dict, events: list) -> None:
    columns = set()
    for ev in events:
        columns.update(ev["features"].keys())
    for node in graph["nodes"]:
        if not node["deps"]:
            col = node["params"]["column"]
            if col not in columns:
                raise BizError(errors.E_OP_UNKNOWN_COLUMN,
                               "root node references unknown feature column",
                               {"node_id": node["id"], "column": col,
                                "known_columns": sorted(columns)})


def load_json_file(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_artifacts(graph_path: str, events_path: str, config_path: str):
    graph = validate_graph(load_json_file(graph_path))
    events = validate_events(load_json_file(events_path))
    config = validate_config(load_json_file(config_path))
    cross_validate(graph, events)
    return graph, events, config
