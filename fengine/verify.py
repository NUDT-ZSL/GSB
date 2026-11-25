"""Consistency verification against a single-process ground truth.

The ground truth is a full float64, event-by-event, single-process
computation of the whole graph. The scheduled fixed-point outputs are
compared per operator per window against it; every value must lie within
the declared tolerance. On mismatch the first divergent node (in layer
order), the input event range and the error-source chain (walked upstream
through the persisted ledgers) are reported.
"""
import json

from .operators import FILTER_PRED
from .planner import build_layers, build_plan


def _float_stream(node, upstream, events):
    params = node["params"]
    if node["deps"]:
        return list(upstream[node["deps"][0]])
    col = params["column"]
    return [(ev["seq"], float(ev["features"][col])) for ev in events]


def ground_truth(graph, events, config):
    """Full float64 computation. Returns {node_id: outputs} where outputs is
    {'kind':'stream','items':{seq:float}} or
    {'kind':'scalar','windows':{window_id:float}}."""
    layers = build_layers(graph)
    nodes = {n["id"]: n for n in graph["nodes"]}
    seq_to_window = {ev["seq"]: ev["ts_ms"] // config["window_ms"]
                     for ev in events}
    result = {}
    streams = {}
    for layer in layers:
        for nid in layer:
            node = nodes[nid]
            op = node["op"]
            params = node["params"]
            stream = _float_stream(node, streams, events)
            if op == "filter":
                pred = FILTER_PRED[params["op"]]
                thr = float(params["threshold"])
                items = {s: v for s, v in stream if pred(v, thr)}
                streams[nid] = sorted(items.items())
                result[nid] = {"kind": "stream", "items": items}
            elif op == "scale":
                f, o = float(params["factor"]), float(params["offset"])
                items = {s: v * f + o for s, v in stream}
                streams[nid] = sorted(items.items())
                result[nid] = {"kind": "stream", "items": items}
            elif op == "window":
                size, func = int(params["size"]), params["func"]
                ordered = sorted(stream)
                items = {}
                for i, (s, _) in enumerate(ordered):
                    lo = max(0, i - size + 1)
                    vals = [v for _, v in ordered[lo:i + 1]]
                    items[s] = sum(vals) if func == "sum" else sum(vals) / len(vals)
                streams[nid] = sorted(items.items())
                result[nid] = {"kind": "stream", "items": items}
            else:  # aggregate
                func = params["func"]
                by_window = {}
                for s, v in stream:
                    by_window.setdefault(seq_to_window[s], []).append(v)
                windows = {}
                for w, vals in by_window.items():
                    if func == "sum":
                        windows[w] = sum(vals)
                    elif func == "mean":
                        windows[w] = sum(vals) / len(vals) if vals else 0.0
                    elif func == "min":
                        windows[w] = min(vals) if vals else 0.0
                    elif func == "max":
                        windows[w] = max(vals) if vals else 0.0
                    else:
                        windows[w] = float(len(vals))
                streams[nid] = sorted(stream)
                result[nid] = {"kind": "scalar", "windows": windows}
    return result


def _error_chain(store, version, node_id, depth=0, seen=None):
    seen = seen or set()
    if node_id in seen or depth > 32:
        return []
    seen.add(node_id)
    rows = store.conn.execute(
        "SELECT ledger_json FROM op_units"
        " WHERE node_id=? AND graph_version=? AND status='done'"
        " ORDER BY window_id", (node_id, version)).fetchall()
    chain = []
    for r in rows:
        led = json.loads(r["ledger_json"])
        chain.append({
            "node_id": node_id,
            "accumulated_error_bound": led["accumulated_error_bound"],
            "event_seq_range": led["event_seq_range"],
            "upstream_nodes": led["upstream_nodes"],
        })
        for up in led["upstream_nodes"]:
            chain.extend(_error_chain(store, version, up, depth + 1, seen))
        break  # one ledger per node is enough for the chain skeleton
    return chain


def verify(store):
    config = store.get_config()
    version = store.current_version()
    graph = store.get_graph(version)
    events = store.get_events()
    tol = float(config["tolerance"])
    scale = config["fixed_scale"]
    plan = build_plan(graph, events, config)
    truth = ground_truth(graph, events, config)

    rows = store.conn.execute(
        "SELECT node_id, window_id, outputs_json, ledger_json FROM op_units"
        " WHERE graph_version=? AND status='done'"
        " ORDER BY node_id, window_id", (version,)).fetchall()
    got = {}
    ledgers = {}
    for r in rows:
        got.setdefault(r["node_id"], {})[r["window_id"]] = \
            json.loads(r["outputs_json"])
        ledgers.setdefault(r["node_id"], {})[r["window_id"]] = \
            json.loads(r["ledger_json"])

    divergences = []
    compared = 0
    max_abs_err = 0.0
    for layer in plan["layers"]:
        for nid in layer:
            t = truth[nid]
            node_got = got.get(nid, {})
            if t["kind"] == "stream":
                merged = {}
                for w in sorted(node_got):
                    for s, v in node_got[w]["items"]:
                        merged[s] = v
                truth_items = t["items"]
                if set(merged) != set(truth_items):
                    divergences.append({
                        "node_id": nid, "window_id": None,
                        "kind": "membership_mismatch",
                        "missing": sorted(set(truth_items) - set(merged))[:8],
                        "extra": sorted(set(merged) - set(truth_items))[:8],
                    })
                    continue
                for s in sorted(truth_items):
                    got_v = merged[s] / scale
                    err = abs(got_v - truth_items[s])
                    max_abs_err = max(max_abs_err, err)
                    compared += 1
                    if err > tol:
                        divergences.append({
                            "node_id": nid,
                            "window_id": plan["seq_to_window"][s],
                            "kind": "value_out_of_tolerance",
                            "event_seq": s, "got": got_v,
                            "truth": truth_items[s], "abs_err": err,
                        })
                        break
            else:
                for w in sorted(t["windows"]):
                    if w not in node_got:
                        divergences.append({
                            "node_id": nid, "window_id": w,
                            "kind": "missing_window_output"})
                        break
                    got_v = node_got[w]["value_fixed"] / scale
                    err = abs(got_v - t["windows"][w])
                    max_abs_err = max(max_abs_err, err)
                    compared += 1
                    if err > tol:
                        divergences.append({
                            "node_id": nid, "window_id": w,
                            "kind": "value_out_of_tolerance",
                            "got": got_v, "truth": t["windows"][w],
                            "abs_err": err})
                        break
            if divergences:
                break
        if divergences:
            break

    report = {
        "status": "ok" if not divergences else "divergent",
        "graph_version": version,
        "compared_values": compared,
        "max_abs_error": max_abs_err,
        "tolerance": tol,
    }
    if divergences:
        first = divergences[0]
        nid = first["node_id"]
        seqs = []
        for w, led in ledgers.get(nid, {}).items():
            if led.get("event_seq_range"):
                seqs.extend(led["event_seq_range"])
        report["first_divergence"] = {
            **first,
            "input_event_range": [min(seqs), max(seqs)] if seqs else None,
            "error_source_chain": _error_chain(store, version, nid),
        }
    return report
