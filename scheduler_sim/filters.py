"""Filter stage: hard constraints, evaluated in a fixed order.

Stages: NodeAffinity -> PodAntiAffinity -> TaintToleration -> Resources.
All failing reasons are collected (deterministically ordered) so the
rejected-node diagnostics are stable across repeated runs. Resource
checks use pure integer comparison.
"""

STAGES = ("NodeAffinity", "PodAntiAffinity", "TaintToleration", "Resources")


def _selector_matches(selector, labels):
    return all(labels.get(k) == v for k, v in selector.items())


def filter_node(pod, node):
    """Return a list of (stage, reason) rejections; empty == feasible."""
    rejections = []

    for term in pod.node_affinity_required:
        if not term.matches(node.labels):
            rejections.append((
                "NodeAffinity",
                f"required term not satisfied: {term.key} "
                f"{term.operator} {list(term.values)}"))

    for term in pod.anti_affinity_required:
        for ep in node.existing_pods:
            if _selector_matches(term["selector"], ep.get("labels", {})):
                rejections.append((
                    "PodAntiAffinity",
                    f"existing pod {ep.get('name', '?')} matches "
                    f"anti-affinity selector {term['selector']} "
                    f"on topology {term['topology_key']}"))
                break

    for taint in node.taints:
        if taint.effect not in ("NoSchedule", "NoExecute"):
            continue
        if not any(tol.tolerates(taint) for tol in pod.tolerations):
            rejections.append((
                "TaintToleration",
                f"untolerated taint {taint.key}={taint.value}:"
                f"{taint.effect}"))

    for res_key in ("cpu_millis", "memory_mib"):
        free = node.allocatable[res_key] - node.allocated[res_key]
        if pod.request[res_key] > free:  # pure integer comparison
            rejections.append((
                "Resources",
                f"insufficient {res_key}: request "
                f"{pod.request[res_key]} > free {free}"))

    return rejections


def filter_feasible(pod, nodes):
    """Split nodes into (feasible, rejected) with stable name order."""
    feasible, rejected = [], []
    for node in sorted(nodes, key=lambda n: n.name):
        reasons = filter_node(pod, node)
        if reasons:
            rejected.append({"node": node.name, "reasons": [
                {"stage": s, "reason": r} for s, r in reasons]})
        else:
            feasible.append(node)
    return feasible, rejected
