"""Score plugins. Each returns a PluginResult dict:

    status: "ok" | "unavailable"
    score:  int in [0, 100]        (only when status == "ok")
    reason: why unavailable        (only when status == "unavailable")
    inputs: deterministic snapshot of the statistics used

A plugin whose required statistics are missing, or whose node is
restricted (e.g. no headroom left), reports ``unavailable`` instead of
defaulting to zero; its weight is then excluded from normalization.
"""

PLUGIN_ORDER = ("node_affinity_soft", "pod_anti_affinity_soft",
                "resource_balance", "image_locality")


def _ok(score, inputs):
    return {"status": "ok", "score": int(score), "inputs": inputs}


def _na(reason, inputs):
    return {"status": "unavailable", "reason": reason, "inputs": inputs}


def node_affinity_soft(pod, node):
    terms = pod.node_affinity_preferred
    if not terms:
        return _na("no preferred node-affinity terms configured", {})
    total = sum(t["weight"] for t in terms)
    matched = sum(t["weight"] for t in terms if t["term"].matches(node.labels))
    score = (matched * 100) // total
    return _ok(score, {"matched_weight": matched, "total_weight": total})


def pod_anti_affinity_soft(pod, node):
    terms = pod.anti_affinity_preferred
    if not terms:
        return _na("no preferred pod anti-affinity terms configured", {})
    conflicts = 0
    for t in terms:
        sel = t["term"]["selector"]
        conflicts += sum(
            1 for ep in node.existing_pods
            if all(ep.get("labels", {}).get(k) == v for k, v in sel.items()))
    score = 100 // (1 + conflicts)
    return _ok(score, {"conflicting_pods": conflicts,
                       "term_count": len(terms)})


def resource_balance(pod, node):
    cpu_after = node.allocatable["cpu_millis"] - node.allocated["cpu_millis"] \
        - pod.request["cpu_millis"]
    mem_after = node.allocatable["memory_mib"] \
        - node.allocated["memory_mib"] - pod.request["memory_mib"]
    if cpu_after <= 0 or mem_after <= 0:
        return _na("node restricted: no allocatable headroom left after "
                   "placement", {"cpu_headroom": cpu_after,
                                 "memory_headroom": mem_after})
    cpu_util = ((node.allocated["cpu_millis"] + pod.request["cpu_millis"])
                * 100) // node.allocatable["cpu_millis"]
    mem_util = ((node.allocated["memory_mib"] + pod.request["memory_mib"])
                * 100) // node.allocatable["memory_mib"]
    score = 100 - abs(cpu_util - mem_util)
    return _ok(score, {"cpu_util_pct": cpu_util, "mem_util_pct": mem_util})


def image_locality(pod, node):
    if node.images is None:
        return _na("image statistics missing on node", {})
    sizes = {img["name"]: img["size_mb"] for img in node.images}
    size = sizes.get(pod.image)
    if size is None:
        return _ok(0, {"image": pod.image, "present": False})
    score = min(100, (size * 100) // 1024)
    return _ok(score, {"image": pod.image, "present": True,
                       "size_mb": size})


PLUGINS = {
    "node_affinity_soft": node_affinity_soft,
    "pod_anti_affinity_soft": pod_anti_affinity_soft,
    "resource_balance": resource_balance,
    "image_locality": image_locality,
}
