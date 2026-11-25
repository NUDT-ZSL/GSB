"""Fixed-point operator kernels: filter / scale / aggregate / window.

Every kernel is a pure function of (node spec, upstream outputs, events,
window assignment, scale) so results are bit-identical under any execution
order, parallelism level, or crash/resume replay. Each kernel also emits an
error ledger traceable to input event seqs and upstream nodes.
"""
from .fixedpoint import (
    accumulation_bound,
    div_round_half_even,
    ledger_entry,
    to_fixed,
)

FILTER_PRED = {
    "gt": lambda v, t: v > t,
    "lt": lambda v, t: v < t,
    "ge": lambda v, t: v >= t,
    "le": lambda v, t: v <= t,
    "eq": lambda v, t: v == t,
}


def root_stream(node: dict, events: list, scale: int):
    """Materialize a root node's input stream from raw event features."""
    col = node["params"]["column"]
    stream, entries = [], []
    for ev in events:
        raw = float(ev["features"][col])
        entries.append(ledger_entry(node["id"], ev["seq"], scale, raw))
        stream.append((ev["seq"], entries[-1]["fixed_value"]))
    return stream, entries


def upstream_stream(node: dict, upstream_outputs: dict):
    dep = node["deps"][0]
    out = upstream_outputs[dep]
    if out["kind"] == "scalar":
        raise ValueError(f"node {node['id']} depends on scalar output of {dep}")
    return [tuple(item) for item in out["items"]]


def _window_entries(node_id, seqs, scale, raw_by_seq):
    return [ledger_entry(node_id, seq, scale, raw_by_seq[seq]) for seq in seqs]


def compute_node(node: dict, upstream_outputs: dict, events: list,
                 window_seqs: set, scale: int) -> dict:
    """Compute one (node, window) unit. Returns {outputs, ledger}."""
    nid = node["id"]
    op = node["op"]
    params = node["params"]

    raw_by_seq = {}
    conv_entries = []
    if node["deps"]:
        stream = upstream_stream(node, upstream_outputs)
        for seq, fixed in stream:
            raw_by_seq[seq] = fixed / scale
    else:
        stream, conv_entries = root_stream(node, events, scale)
        for ev in events:
            raw_by_seq[ev["seq"]] = float(ev["features"][params["column"]])
    stream_by_seq = dict(stream)
    in_window = [seq for seq, _ in stream if seq in window_seqs]

    if op == "filter":
        thr = to_fixed(float(params["threshold"]), scale)
        pred = FILTER_PRED[params["op"]]
        items = [(s, stream_by_seq[s]) for s in in_window
                 if pred(stream_by_seq[s], thr)]
        if node["deps"]:
            conv_entries += _window_entries(nid, in_window, scale, raw_by_seq)
        bound = max((abs(e["quant_error"]) for e in conv_entries), default=0.0)
        outputs = {"kind": "stream", "items": items}

    elif op == "scale":
        factor_f = to_fixed(float(params["factor"]), scale)
        offset_f = to_fixed(float(params["offset"]), scale)
        items = []
        for s in in_window:
            v = stream_by_seq[s]
            items.append((s, div_round_half_even(v * factor_f, scale) + offset_f))
        if node["deps"]:
            conv_entries += _window_entries(nid, in_window, scale, raw_by_seq)
        errs = [e["quant_error"] for e in conv_entries]
        vals = [raw_by_seq[s] for s in in_window]
        bound = accumulation_bound(errs, vals) * abs(float(params["factor"]))
        outputs = {"kind": "stream", "items": items}

    elif op == "aggregate":
        func = params["func"]
        vals = [stream_by_seq[s] for s in in_window]
        n = len(vals)
        if func == "sum":
            value = sum(vals)
        elif func == "mean":
            value = div_round_half_even(sum(vals), n) if n else 0
        elif func == "min":
            value = min(vals) if vals else 0
        elif func == "max":
            value = max(vals) if vals else 0
        else:  # count
            value = n * scale
        if node["deps"]:
            conv_entries += _window_entries(nid, in_window, scale, raw_by_seq)
        errs = [e["quant_error"] for e in conv_entries]
        raws = [raw_by_seq[s] for s in in_window]
        bound = accumulation_bound(errs, raws)
        if func == "mean" and n:
            bound /= n
        outputs = {"kind": "scalar", "value_fixed": value, "n_inputs": n}

    else:  # window (sliding, count-based)
        size = int(params["size"])
        func = params["func"]
        ordered = sorted(stream)  # by seq, deterministic
        items = []
        bounds = []
        for i, (s, _) in enumerate(ordered):
            if s not in window_seqs:
                continue
            lo = max(0, i - size + 1)
            slice_pairs = ordered[lo:i + 1]
            slice_vals = [v for _, v in slice_pairs]
            total = sum(slice_vals)
            if func == "mean":
                items.append((s, div_round_half_even(total, len(slice_vals))))
            else:
                items.append((s, total))
            slice_errs = [abs(raw_by_seq[seq] - stream_by_seq[seq] / scale)
                          for seq, _ in slice_pairs]
            slice_raws = [raw_by_seq[seq] for seq, _ in slice_pairs]
            b = accumulation_bound(slice_errs, slice_raws)
            bounds.append(b / len(slice_vals) if func == "mean" else b)
        if node["deps"]:
            conv_entries += _window_entries(nid, in_window, scale, raw_by_seq)
        bound = max(bounds, default=0.0)
        outputs = {"kind": "stream", "items": items}

    if outputs["kind"] == "stream":
        seqs = [s for s, _ in outputs["items"]]
    else:
        seqs = sorted(in_window)
    ledger = {
        "node_id": nid,
        "scale": scale,
        "rounding": "half_even",
        "entries": conv_entries,
        "accumulated_error_bound": bound,
        "event_seq_range": [min(seqs), max(seqs)] if seqs else None,
        "upstream_nodes": list(node["deps"]),
    }
    return {"outputs": outputs, "ledger": ledger}
