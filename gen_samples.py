#!/usr/bin/env python3
"""Fixed-seed deterministic sample generator for the feature-graph engine.

Generates three JSON artifacts plus a v2 graph patch:
  - graph.json    : feature computation graph description
  - events.json   : sample event stream (seq, logical ts, features, digest)
  - config.json   : diagnostics config (budgets, scale, tolerance, clock)
  - graph_v2.json : patched graph (window size changed) for versioning tests
"""
import argparse
import hashlib
import json
import os
import random

SEED = 20261126


def event_digest(seq: int, ts_ms: int, features: dict) -> str:
    canonical = json.dumps(
        {"seq": seq, "ts_ms": ts_ms, "features": features},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def build_graph() -> dict:
    nodes = [
        {"id": "n_scale_amt", "op": "scale", "deps": [],
         "params": {"column": "amount", "factor": 1.05, "offset": 0.5},
         "output_type": "stream"},
        {"id": "n_filt_score", "op": "filter", "deps": [],
         "params": {"column": "score", "op": "ge", "threshold": 0.55},
         "output_type": "stream"},
        {"id": "n_agg_amt_mean", "op": "aggregate", "deps": [],
         "params": {"column": "amount", "func": "mean"},
         "output_type": "scalar"},
        {"id": "n_win_sum", "op": "window", "deps": ["n_scale_amt"],
         "params": {"size": 5, "func": "sum"},
         "output_type": "stream"},
        {"id": "n_agg_filt_sum", "op": "aggregate", "deps": ["n_filt_score"],
         "params": {"func": "sum"},
         "output_type": "scalar"},
        {"id": "n_scale_win", "op": "scale", "deps": ["n_win_sum"],
         "params": {"factor": 0.9, "offset": -1.0},
         "output_type": "stream"},
        {"id": "n_agg_win_mean", "op": "aggregate", "deps": ["n_win_sum"],
         "params": {"func": "mean"},
         "output_type": "scalar"},
        {"id": "n_agg_s2_max", "op": "aggregate", "deps": ["n_scale_win"],
         "params": {"func": "max"},
         "output_type": "scalar"},
    ]
    return {"graph_name": "risk_features", "nodes": nodes}


def build_events(n: int = 120) -> list:
    rng = random.Random(SEED)
    events = []
    for seq in range(n):
        ts_ms = 1000 + seq * 40 + rng.randint(0, 15)
        features = {
            "amount": round(rng.uniform(1.0, 100.0), 6),
            "score": round(rng.uniform(0.0, 1.0), 6),
            "count": float(rng.randint(1, 20)),
        }
        events.append({
            "seq": seq,
            "ts_ms": ts_ms,
            "features": features,
            "digest": event_digest(seq, ts_ms, features),
        })
    return events


def build_config() -> dict:
    return {
        "memory_budget_bytes": 1048576,
        "op_memory_estimate_bytes": 4096,
        "parallel_slots": 4,
        "round_time_budget_ms": 50,
        "fixed_scale": 10000,
        "tolerance": 0.05,
        "clock_step_ms": 5,
        "window_ms": 200,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    graph = build_graph()
    events = build_events()
    config = build_config()

    graph_v2 = json.loads(json.dumps(graph))
    for node in graph_v2["nodes"]:
        if node["id"] == "n_win_sum":
            node["params"]["size"] = 8

    artifacts = {
        "graph.json": graph,
        "events.json": events,
        "config.json": config,
        "graph_v2.json": graph_v2,
    }
    for name, payload in artifacts.items():
        path = os.path.join(args.out, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        print("wrote", path)


if __name__ == "__main__":
    main()
