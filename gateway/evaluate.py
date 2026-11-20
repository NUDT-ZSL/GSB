"""Algorithm trade-off evaluation across arrival-rate sample points.

For each scale factor the same services are run twice - once forced into
per-request routing, once forced into window-batch routing - and
comparable metrics are emitted (completed count measured on the injected
logical clock, replica utilization, queue-wait quantiles, failure count).
The mode actually selected by the adjudicator and the rationale are
included, together with the observation/truth sources each estimate
relies on.
"""
import os

import numpy as np

from . import models
from .engine import Engine
from .models import Arrival, Trace, TraceWindow
from .router import adjudicate_mode
from .store import Store


def scale_trace(trace, factor):
    windows = []
    for w in trace.windows:
        arrivals = [Arrival(
            service=a.service,
            arrival_rate=round(a.arrival_rate * factor, 6),
            avg_seq_len=a.avg_seq_len,
            expected_concurrency=a.expected_concurrency,
            token_budget=a.token_budget,
            fault=a.fault) for a in w.arrivals]
        windows.append(TraceWindow(index=w.index, start_ms=w.start_ms,
                                   window_ms=w.window_ms,
                                   arrivals=tuple(arrivals)))
    return Trace(config=trace.config, windows=tuple(windows))


def _quantiles(values):
    if not values:
        return {"p50": 0.0, "p90": 0.0, "p99": 0.0}
    arr = np.array(sorted(values), dtype=np.float64)
    return {
        "p50": round(float(np.quantile(arr, 0.50)), 3),
        "p90": round(float(np.quantile(arr, 0.90)), 3),
        "p99": round(float(np.quantile(arr, 0.99)), 3),
    }


def compute_metrics(report):
    completed, failures, queue_waits = 0, 0, []
    busy, capacity = 0, 0
    last_finish = 0
    for rnd in report["rounds"]:
        window_ms = rnd["window_ms"]
        for req in rnd["requests"]:
            if req["outcome"] == models.OUTCOME_SUCCESS:
                completed += 1
                queue_waits.append(req["queue_wait_ms"])
                if req["finish_ms"] is not None:
                    last_finish = max(last_finish, req["finish_ms"])
            elif req["outcome"] == models.OUTCOME_FAILED_AFTER_START:
                failures += 1
        for busy_list in rnd["replica_busy_ms"].values():
            busy += sum(busy_list)
            capacity += len(busy_list) * window_ms
    utilization = round(busy / capacity, 6) if capacity else 0.0
    return {
        "completed": completed,
        "failures": failures,
        "last_finish_logical_ms": last_finish,
        "replica_utilization": utilization,
        "queue_wait_quantiles_ms": _quantiles(queue_waits),
    }


def evaluate(services, nodes, trace, workdir,
             scales=(0.25, 0.5, 1.0, 2.0, 4.0)):
    os.makedirs(workdir, exist_ok=True)
    results = []
    for scale in scales:
        scaled = scale_trace(trace, scale)
        row = {"scale": scale, "modes": {}}
        for mode in (models.MODE_PER_REQUEST, models.MODE_WINDOW_BATCH):
            db_path = os.path.join(workdir, f"eval_{scale}_{mode}.db")
            for suffix in ("", "-wal", "-shm", ".lock"):
                if os.path.exists(db_path + suffix):
                    os.remove(db_path + suffix)
            store = Store(db_path)
            try:
                engine = Engine(services, nodes, scaled, store,
                                force_mode=mode)
                report = engine.run()
            finally:
                store.close()
            row["modes"][mode] = compute_metrics(report)
        peak_ratio = 0.0
        for w in scaled.windows:
            for a in w.arrivals:
                decision = adjudicate_mode(a, services[a.service], w,
                                           scaled.config)
                peak_ratio = max(peak_ratio, decision["load_ratio"])
        threshold = scaled.config.switch_load_threshold
        selected = (models.MODE_PER_REQUEST if peak_ratio < threshold
                    else models.MODE_WINDOW_BATCH)
        other = (models.MODE_WINDOW_BATCH if selected
                 == models.MODE_PER_REQUEST else models.MODE_PER_REQUEST)
        row["selected_mode"] = selected
        row["peak_load_ratio"] = round(peak_ratio, 6)
        row["rationale"] = (
            f"peak load_ratio {round(peak_ratio, 6)} vs threshold "
            f"{threshold}; selected {selected}. Observed at this scale: "
            f"{selected} completed="
            f"{row['modes'][selected]['completed']} queue_p90="
            f"{row['modes'][selected]['queue_wait_quantiles_ms']['p90']}ms "
            f"vs rejected {other} completed="
            f"{row['modes'][other]['completed']} queue_p90="
            f"{row['modes'][other]['queue_wait_quantiles_ms']['p90']}ms")
        results.append(row)
    return {
        "schema": "gateway-eval/v1",
        "scales": list(scales),
        "results": results,
        "sources": {
            "truth": ("arrival rates, sequence lengths, concurrency and "
                      "token budgets are ground truth injected by the "
                      "trace sample (deterministic logical clock)"),
            "observation": ("completion counts, queue waits, utilization "
                            "and failures are observed from engine runs "
                            "against the simulated upstream contract"),
        },
    }
