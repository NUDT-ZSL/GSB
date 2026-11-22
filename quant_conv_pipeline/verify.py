"""Result verification tool and cross-module consistency self-check.

Checks, for a snapshot:
  1. every node cache key independently replays to the stored artifact;
  2. the four nodes processed identical job_id sets;
  3. recompute and reuse sets are disjoint and their union covers the
     affected (node, job) set.
Any mismatch raises E_INCONSISTENT naming the node and job_id.
"""
from __future__ import annotations

from . import nodes
from .errors import Code, PipelineError


def _pairs(entries):
    return {(e["node"], e["job_id"]) for e in entries}


def verify_snapshot(pipeline, snap_id):
    snap = pipeline.load_snapshot(snap_id)
    lineage = snap["lineage"]
    invalidation = snap["invalidation_report"]
    failures = []

    # 1. every recorded node key replays to its stored artifact
    replayed = 0
    for jid, record in sorted(lineage.items()):
        for node in nodes.NODE_ORDER:
            key = record["node_keys"].get(node)
            if key is None:
                failures.append({"node": node, "job_id": jid,
                                 "reason": "missing node key in lineage"})
                continue
            try:
                _, ok = nodes.replay(pipeline.cache, key)
            except PipelineError as exc:
                failures.append({"node": node, "job_id": jid,
                                 "reason": exc.code})
                continue
            if not ok:
                failures.append({"node": node, "job_id": jid,
                                 "reason": "replayed artifact hash mismatch"})
            else:
                replayed += 1

    # 2. the four nodes saw identical job_id sets
    per_node = {node: set() for node in nodes.NODE_ORDER}
    for jid, record in lineage.items():
        for node, key in record["node_keys"].items():
            if node in per_node:
                per_node[node].add(jid)
    ref_set = per_node[nodes.NODE_ORDER[0]]
    for node in nodes.NODE_ORDER[1:]:
        if per_node[node] != ref_set:
            for jid in sorted(per_node[node] ^ ref_set):
                failures.append({"node": node, "job_id": jid,
                                 "reason": "job set differs across nodes"})

    # 3. recompute / reuse sets: disjoint, union covers affected set
    recompute = _pairs(invalidation.get("recompute", []))
    reuse = _pairs(invalidation.get("reuse", []))
    overlap = recompute & reuse
    for node, jid in sorted(overlap):
        failures.append({"node": node, "job_id": jid,
                         "reason": "entry in both recompute and reuse sets"})
    affected = {(node, jid)
                for node in invalidation.get("affected_nodes", [])
                for jid in invalidation.get("affected_jobs", [])}
    uncovered = affected - recompute - reuse
    for node, jid in sorted(uncovered):
        failures.append({"node": node, "job_id": jid,
                         "reason": "affected entry covered by neither "
                                   "recompute nor reuse"})

    report = {
        "snapshot_id": snap_id,
        "consistent": not failures,
        "entries_replayed_ok": replayed,
        "node_job_sets": {n: sorted(s) for n, s in per_node.items()},
        "recompute_count": len(recompute),
        "reuse_count": len(reuse),
        "affected_count": len(affected),
        "failures": failures,
        "invalidation_report": invalidation,
        "deviation_spectrum": snap["deviation_spectrum"],
        "validation_errors": snap["validation_errors"],
    }
    if failures:
        first = failures[0]
        err = PipelineError(Code.E_INCONSISTENT,
                            f"consistency check failed at node "
                            f"'{first['node']}' job '{first['job_id']}': "
                            f"{first['reason']}",
                            job_id=first["job_id"], node=first["node"],
                            failures=failures)
        err.report = report
        raise err
    return report
