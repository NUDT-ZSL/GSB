"""Layered scheduler under memory-budget and parallel-slot constraints.

Each round claims candidate units from the lowest unfinished layer in stable
(node_id, window_id) order. A unit starts only if the remaining memory
budget admits it; units that do not fit are abandoned with a recorded
reason, strictly distinguishing "never started within budget" from
"started then failed". Started operators always run to completion (no
thread timeouts). Every completion commits operator state, memory
accounting, error ledger and the logical-clock watermark in ONE SQLite
transaction, so a killed process resumes from a consistent checkpoint.
"""
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from .operators import compute_node
from .planner import build_plan


def estimate_memory(node, n_window_events, config):
    base = node.get("memory_bytes") or config["op_memory_estimate_bytes"]
    return base + 16 * n_window_events


def _assemble_upstream(store, node, version):
    """Merge a dependency's done outputs across all windows into one stream."""
    upstream = {}
    for dep in node["deps"]:
        rows = store.conn.execute(
            "SELECT outputs_json FROM op_units"
            " WHERE node_id=? AND graph_version=? AND status='done'",
            (dep, version)).fetchall()
        items = []
        for r in rows:
            out = json.loads(r["outputs_json"])
            if out["kind"] == "stream":
                items.extend(tuple(i) for i in out["items"])
        items.sort(key=lambda iv: iv[0])
        upstream[dep] = {"kind": "stream", "items": items}
    return upstream


def _claim_batch(store, version, config, nodes_by_id, window_seqs,
                 slots, window_lo, window_hi, worker_id):
    """Claim up to `slots` units from the lowest unfinished layer."""
    step = config["clock_step_ms"]
    time_budget = config["round_time_budget_ms"]
    budget = config["memory_budget_bytes"]
    picked = []
    with store.txn() as c:
        clock = int(store.get_meta("clock", "0"))
        busy = c.execute(
            "SELECT COUNT(*) AS n FROM op_units"
            " WHERE graph_version=? AND status='running'", (version,)).fetchone()["n"]
        free_slots = max(0, slots - busy)
        if free_slots == 0:
            return []
        row = c.execute(
            "SELECT MIN(layer) AS l FROM op_units"
            " WHERE graph_version=? AND status='pending'"
            " AND window_id>=? AND window_id<?",
            (version, window_lo, window_hi)).fetchone()
        if row["l"] is None:
            return []
        layer = row["l"]
        blocked = c.execute(
            "SELECT COUNT(*) AS n FROM op_units"
            " WHERE graph_version=? AND layer<? AND status IN ('pending','running')",
            (version, layer)).fetchone()["n"]
        if blocked:
            return []
        candidates = c.execute(
            "SELECT node_id, window_id FROM op_units"
            " WHERE graph_version=? AND layer=? AND status='pending'"
            " AND window_id>=? AND window_id<?"
            " ORDER BY node_id, window_id",
            (version, layer, window_lo, window_hi)).fetchall()
        balance = store.memory_balance(c)
        time_used = 0
        for cand in candidates:
            if len(picked) >= free_slots:
                break
            if time_used + step > time_budget:
                break
            node = nodes_by_id[cand["node_id"]]
            est = estimate_memory(node, len(window_seqs[cand["window_id"]]), config)
            if balance + est > budget:
                c.execute(
                    "UPDATE op_units SET status='abandoned',"
                    " abandon_reason=?, memory_bytes=?, worker=?"
                    " WHERE node_id=? AND graph_version=? AND window_id=?",
                    ("E_MEMORY_BUDGET_EXHAUSTED", est, worker_id,
                     cand["node_id"], version, cand["window_id"]))
                continue
            clock += step
            time_used += step
            balance += est
            c.execute(
                "UPDATE op_units SET status='running', attempt=attempt+1,"
                " memory_bytes=?, started_clock=?, worker=?"
                " WHERE node_id=? AND graph_version=? AND window_id=?"
                " AND status='pending'",
                (est, clock, worker_id, cand["node_id"], version,
                 cand["window_id"]))
            c.execute(
                "INSERT INTO memory_ledger"
                "(node_id, graph_version, window_id, delta, balance_after,"
                " clock, note) VALUES(?,?,?,?,?,?, 'reserve')",
                (cand["node_id"], version, cand["window_id"], est,
                 balance, clock))
            picked.append({"node_id": cand["node_id"],
                           "window_id": cand["window_id"],
                           "memory_bytes": est, "started_clock": clock})
        store.set_meta("clock", str(clock), c)
    return picked


def _commit_unit(store, version, unit, result, events_by_seq_window):
    """Atomically commit one finished operator unit."""
    with store.txn() as c:
        clock = int(store.get_meta("clock", "0"))
        balance = store.memory_balance(c) - unit["memory_bytes"]
        c.execute(
            "UPDATE op_units SET status='done', finished_clock=?,"
            " outputs_json=?, ledger_json=?"
            " WHERE node_id=? AND graph_version=? AND window_id=?",
            (clock, json.dumps(result["outputs"], sort_keys=True),
             json.dumps(result["ledger"], sort_keys=True),
             unit["node_id"], version, unit["window_id"]))
        c.execute(
            "INSERT INTO memory_ledger"
            "(node_id, graph_version, window_id, delta, balance_after,"
            " clock, note) VALUES(?,?,?,?,?,?, 'release')",
            (unit["node_id"], version, unit["window_id"],
             -unit["memory_bytes"], balance, clock))
        remaining = c.execute(
            "SELECT COUNT(*) AS n FROM op_units"
            " WHERE graph_version=? AND window_id=?"
            " AND status IN ('pending','running')",
            (version, unit["window_id"])).fetchone()["n"]
        if remaining == 0:
            rows = c.execute(
                "SELECT node_id, outputs_json FROM op_units"
                " WHERE graph_version=? AND window_id=? AND status='done'"
                " ORDER BY node_id", (version, unit["window_id"])).fetchall()
            digest = hashlib.sha256(
                "|".join(f"{r['node_id']}:{r['outputs_json']}" for r in rows)
                .encode()).hexdigest()
            c.execute(
                "INSERT OR IGNORE INTO window_commits"
                "(window_id, graph_version, commit_hash, clock)"
                " VALUES(?,?,?,?)",
                (unit["window_id"], version, digest, clock))


def run(store, worker_id="w0", slots=None, window_lo=-2**62, window_hi=2**62,
        crash_after=None, resume=False):
    if not store.is_loaded():
        from .errors import BizError, E_NOT_LOADED
        raise BizError(E_NOT_LOADED, "artifacts not loaded into this database", {})
    config = store.get_config()
    version = store.current_version()
    graph = store.get_graph(version)
    events = store.get_events()
    plan = build_plan(graph, events, config)
    store.ensure_units(plan["units"], version)
    if resume:
        store.reset_running(version)

    nodes_by_id = {n["id"]: n for n in graph["nodes"]}
    scale = config["fixed_scale"]
    slots = slots or config["parallel_slots"]
    stats = {"committed": 0, "abandoned": 0, "reused": 0}
    crash_env = os.environ.get("FENGINE_CRASH_AFTER")
    crash_after = crash_after if crash_after is not None else \
        (int(crash_env) if crash_env else None)

    executor = ThreadPoolExecutor(max_workers=max(1, slots))
    try:
        while True:
            batch = _claim_batch(store, version, config, nodes_by_id,
                                 plan["window_seqs"], slots,
                                 window_lo, window_hi, worker_id)
            if not batch:
                row = store.conn.execute(
                    "SELECT COUNT(*) AS n FROM op_units"
                    " WHERE graph_version=? AND status IN ('pending','running')"
                    " AND window_id>=? AND window_id<?",
                    (version, window_lo, window_hi)).fetchone()
                if row["n"] == 0:
                    break
                time.sleep(0.01)  # wait for other workers' running units
                continue
            futures = []
            for unit in batch:
                node = nodes_by_id[unit["node_id"]]
                upstream = _assemble_upstream(store, node, version)
                wseqs = set(plan["window_seqs"][unit["window_id"]])
                fut = executor.submit(compute_node, node, upstream, events,
                                      wseqs, scale)
                futures.append((unit, fut))
            for unit, fut in futures:
                result = fut.result()  # started ops always run to completion
                _commit_unit(store, version, unit, result, None)
                stats["committed"] += 1
                if crash_after is not None and stats["committed"] >= crash_after:
                    executor.shutdown(wait=False)
                    os._exit(3)  # simulate hard kill: no cleanup, no finally
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM op_units"
            " WHERE graph_version=? AND status='abandoned'",
            (version,)).fetchone()
        stats["abandoned"] = row["n"]
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM op_units"
            " WHERE graph_version=? AND reused=1", (version,)).fetchone()
        stats["reused"] = row["n"]
    finally:
        executor.shutdown(wait=True)
    return stats
