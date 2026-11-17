"""Compile a v3 manifest into a per-node distribution plan and recompute
expected derived digests. Shared by the platform, the node simulator and
the verification tool so all three compute identical expectations."""
import hashlib
import json


def derived_digest(entries, derive_rules):
    """entries: {key: [value, version]}; derive_rules: rules applying to one node."""
    h = hashlib.sha256()
    canon = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    h.update(b"slice:" + canon.encode("utf-8"))
    for rule in sorted(derive_rules, key=lambda r: (r["node"], tuple(r["from"]))):
        values = {k: entries[k] for k in sorted(rule["from"]) if k in entries}
        payload = json.dumps({"algorithm": rule["algorithm"], "from": sorted(rule["from"]),
                              "values": values}, sort_keys=True, separators=(",", ":"))
        h.update(b"|derive:" + payload.encode("utf-8"))
    return h.hexdigest()


def compile_plan(manifest, node_ids):
    """Deterministic plan: {node: {entries, derive, expected_derived, version}}."""
    nodes = sorted(node_ids)
    policy = manifest["policy"]
    plan = {}
    for node in nodes:
        rules = [r for r in manifest["derive"] if r["node"] in (node, "*")]
        plan[node] = {"entries": {}, "derive": rules}

    for entry in sorted(manifest["entries"], key=lambda e: e["key"]):
        if entry["targets"] == ["*"]:
            targets = list(nodes)
        else:
            targets = sorted(t for t in entry["targets"] if t in plan)
        if policy["strategy"] == "shard" and len(targets) > 1:
            idx = int(hashlib.sha256(entry["key"].encode("utf-8")).hexdigest(), 16) \
                % len(targets)
            targets = [targets[idx]]
        for node in targets:
            plan[node]["entries"][entry["key"]] = [entry["value"], entry["version"]]

    for node in nodes:
        slot = plan[node]
        slot["expected_derived"] = derived_digest(slot["entries"], slot["derive"])
        slot["version"] = max((v for _, v in slot["entries"].values()), default=0)
    return plan
