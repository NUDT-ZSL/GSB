"""Score stage orchestration: fixed-point weighted sums, error-bound
tie marking, deterministic tie-breaking, and queue scheduling.
"""

from . import fixedpoint
from .filters import filter_feasible
from .plugins import PLUGINS, PLUGIN_ORDER

DEFAULT_WEIGHTS = {
    "node_affinity_soft": 3,
    "pod_anti_affinity_soft": 2,
    "resource_balance": 1,
    "image_locality": 1,
}


def score_node(pod, node, weights):
    """Score one pod on one node. Returns a deterministic detail dict."""
    contributions, unavailable, plugin_details = [], [], []
    for name in PLUGIN_ORDER:
        result = PLUGINS[name](pod, node)
        weight = int(weights.get(name, 0))
        entry = {"plugin": name, "weight": weight,
                 "status": result["status"], "inputs": result["inputs"]}
        if result["status"] == "ok":
            entry["raw"] = result["score"]
            if weight > 0:
                contributions.append({"plugin": name, "raw": result["score"],
                                      "weight": weight})
        else:
            entry["reason"] = result["reason"]
            unavailable.append({"plugin": name, "reason": result["reason"]})
        plugin_details.append(entry)

    total_units, bound_units, contrib_details = fixedpoint.weighted_sum(
        contributions)
    by_plugin = {d["plugin"]: d["contribution_units"]
                 for d in contrib_details}
    for entry in plugin_details:
        entry["contribution_units"] = by_plugin.get(entry["plugin"])
    return {
        "node": node.name,
        "score_units": total_units,
        "score_points": fixedpoint.units_to_points(total_units),
        "error_bound_units": bound_units,
        "error_bound_points": fixedpoint.units_to_points(bound_units),
        "grid": fixedpoint.GRID,
        "plugins": plugin_details,
        "unavailable_plugins": unavailable,
    }


def score_pod(pod, nodes, weights):
    """Filter + score one pod against all nodes, pick a winner."""
    feasible, rejected = filter_feasible(pod, nodes)
    scored = [score_node(pod, n, weights) for n in feasible]
    scored.sort(key=lambda s: (-s["score_units"], s["node"]))

    winner, tie_group, tie_break = None, [], None
    if scored:
        best = scored[0]
        tie_group = [s["node"] for s in scored[1:]
                     if fixedpoint.indistinguishable(
                         s["score_units"], s["error_bound_units"],
                         best["score_units"], best["error_bound_units"])]
        winner = best["node"]
        if tie_group:
            candidates = sorted([winner] + tie_group)
            winner = candidates[0]
            tie_break = ("indistinguishable tie within combined error "
                         "bound; winner chosen by stable node-name order "
                         f"among {candidates}")
        elif len(scored) > 1 and \
                scored[0]["score_units"] == scored[1]["score_units"]:
            tie_break = "exact score tie; winner chosen by node name"
        else:
            tie_break = "winner by strictly highest fixed-point score"

    return {
        "pod": pod.name,
        "feasible_nodes": [n.name for n in feasible],
        "rejected_nodes": rejected,
        "scores": scored,
        "tie_group": tie_group,
        "winner": winner,
        "tie_break_reason": tie_break,
    }


def schedule_queue(nodes, pods, weights, on_stats_missing=None):
    """Schedule pods in queue order, mutating node state as pods bind."""
    node_by_name = {n.name: n for n in nodes}
    reports, bindings = [], {}
    for pod in pods:
        report = score_pod(pod, nodes, weights)
        for s in report["scores"]:
            for na in s["unavailable_plugins"]:
                if "statistics missing" in na["reason"] and \
                        on_stats_missing is not None:
                    on_stats_missing(pod.name, s["node"], na["plugin"],
                                     na["reason"])
        reports.append(report)
        if report["winner"] is not None:
            node = node_by_name[report["winner"]]
            node.allocated["cpu_millis"] += pod.request["cpu_millis"]
            node.allocated["memory_mib"] += pod.request["memory_mib"]
            node.existing_pods.append({"name": pod.name,
                                       "labels": dict(pod.labels)})
            bindings[pod.name] = report["winner"]
    return {"reports": reports, "bindings": bindings}
