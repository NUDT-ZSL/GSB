"""Graph version replacement with invalidation-closure adjudication.

A patch is validated, hash-compared against all known versions (any exact
resubmission of an existing or older graph is a rollback and is rejected
with E_VERSION_ROLLBACK), then structurally diffed. The affected closure is
changed nodes plus all transitive dependents. Cached results and ledgers of
unaffected nodes are reused verbatim; in-flight operators stay bound to
the graph version they started with (op_units are keyed by graph_version).
"""
import json

from .errors import BizError, E_VERSION_ROLLBACK
from .loader import cross_validate, graph_hash, validate_graph
from .planner import build_plan


def _node_key(node):
    return json.dumps(
        {"op": node["op"], "deps": node["deps"], "params": node["params"],
         "output_type": node["output_type"]},
        sort_keys=True, separators=(",", ":"))


def _descendants(graph, roots):
    children = {}
    for n in graph["nodes"]:
        for d in n["deps"]:
            children.setdefault(d, []).append(n["id"])
    closure = set(roots)
    stack = list(roots)
    while stack:
        nid = stack.pop()
        for c in children.get(nid, []):
            if c not in closure:
                closure.add(c)
                stack.append(c)
    return closure


def apply_patch(store, new_graph: dict, target_version: int | None = None) -> dict:
    validate_graph(new_graph)
    events = store.get_events()
    cross_validate(new_graph, events)

    current = store.current_version()
    if target_version is not None and target_version <= current:
        raise BizError(E_VERSION_ROLLBACK,
                       "explicit rollback to an older version is rejected",
                       {"target_version": target_version,
                        "current_version": current})
    new_hash = graph_hash(new_graph)
    rows = store.conn.execute(
        "SELECT version, graph_hash FROM graph_versions").fetchall()
    for r in rows:
        if r["graph_hash"] == new_hash:
            raise BizError(E_VERSION_ROLLBACK,
                           "patched graph is identical to a known version",
                           {"matching_version": r["version"],
                            "current_version": current})

    old_graph = store.get_graph(current)
    old_nodes = {n["id"]: n for n in old_graph["nodes"]}
    new_nodes = {n["id"]: n for n in new_graph["nodes"]}
    changed = set()
    for nid in set(old_nodes) | set(new_nodes):
        if nid not in old_nodes or nid not in new_nodes:
            changed.add(nid)
        elif _node_key(old_nodes[nid]) != _node_key(new_nodes[nid]):
            changed.add(nid)
    closure = _descendants(new_graph, changed & set(new_nodes))
    closure |= _descendants(old_graph, changed & set(old_nodes))
    closure &= set(new_nodes)  # removed nodes have no units in the new version

    config = store.get_config()
    plan = build_plan(new_graph, events, config)
    new_version = current + 1
    reused = 0
    invalidated = 0
    with store.txn() as c:
        clock = int(store.get_meta("clock", "0"))
        c.execute(
            "INSERT INTO graph_versions(version, graph_hash, graph_json,"
            " created_clock) VALUES(?,?,?,?)",
            (new_version, new_hash, json.dumps(new_graph, sort_keys=True), clock))
        for nid in sorted(set(new_nodes) - closure):
            done_rows = c.execute(
                "SELECT window_id, layer, outputs_json, ledger_json,"
                " finished_clock FROM op_units"
                " WHERE node_id=? AND graph_version=? AND status='done'",
                (nid, current)).fetchall()
            for r in done_rows:
                c.execute(
                    "INSERT OR REPLACE INTO op_units"
                    "(node_id, graph_version, window_id, layer, status,"
                    " attempt, finished_clock, reused, outputs_json, ledger_json)"
                    " VALUES(?,?,?,?, 'done', 0, ?, 1, ?, ?)",
                    (nid, new_version, r["window_id"], r["layer"],
                     r["finished_clock"], r["outputs_json"], r["ledger_json"]))
                reused += 1
        for u in plan["units"]:
            if u["node_id"] in closure:
                c.execute(
                    "INSERT OR IGNORE INTO op_units"
                    "(node_id, graph_version, window_id, layer, status)"
                    " VALUES(?,?,?,?,'pending')",
                    (u["node_id"], new_version, u["window_id"], u["layer"]))
                invalidated += 1
        store.set_meta("current_version", str(new_version), c)
        store.set_meta("plan_summary", plan["plan_summary"], c)
    return {
        "new_version": new_version,
        "changed_nodes": sorted(changed),
        "invalidation_closure": sorted(closure),
        "reused_units": reused,
        "invalidated_units": invalidated,
        "plan_summary": plan["plan_summary"],
    }
