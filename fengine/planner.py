"""Layered execution-plan construction driven by the injected logical clock.

The graph is compiled into topologically ordered operator layers; every
sample event is assigned to exactly one execution window derived from the
injected logical clock (never the wall clock). The plan summary hash is
stable for identical graph + logical clock sequences.
"""
import hashlib
import json


def build_layers(graph: dict) -> list:
    """Kahn's algorithm by depth; nodes within a layer sorted by id."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    depth = {}

    def depth_of(nid):
        if nid in depth:
            return depth[nid]
        deps = nodes[nid]["deps"]
        depth[nid] = 0 if not deps else 1 + max(depth_of(d) for d in deps)
        return depth[nid]

    for nid in nodes:
        depth_of(nid)
    max_depth = max(depth.values())
    layers = []
    for d in range(max_depth + 1):
        layers.append(sorted(nid for nid, dd in depth.items() if dd == d))
    return layers


def assign_windows(events: list, window_ms: int) -> dict:
    """Map each event seq to its unique window id (ts_ms // window_ms)."""
    return {ev["seq"]: ev["ts_ms"] // window_ms for ev in events}


def build_plan(graph: dict, events: list, config: dict) -> dict:
    layers = build_layers(graph)
    seq_to_window = assign_windows(events, config["window_ms"])
    windows = sorted(set(seq_to_window.values()))
    window_seqs = {w: sorted(s for s, ww in seq_to_window.items() if ww == w)
                   for w in windows}
    units = []
    for layer_idx, layer in enumerate(layers):
        for nid in layer:
            for w in windows:
                units.append({"layer": layer_idx, "node_id": nid, "window_id": w})
    summary_payload = {
        "layers": layers,
        "windows": windows,
        "window_seqs": {str(k): v for k, v in window_seqs.items()},
        "clock_step_ms": config["clock_step_ms"],
        "window_ms": config["window_ms"],
        "graph_hash": hashlib.sha256(
            json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    summary = hashlib.sha256(
        json.dumps(summary_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "layers": layers,
        "windows": windows,
        "window_seqs": window_seqs,
        "seq_to_window": seq_to_window,
        "units": units,
        "plan_summary": summary,
    }
