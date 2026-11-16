"""Post-recovery recomputation check (E_REPLAY).

On recovery, the distribution plan and per-node expected derived-state
digests of the last committed version are recomputed from the persisted
manifest and compared item-by-item against the stored plan and receipts.
Degraded nodes or a missing config version make the replay
non-reproducible and are reported with E_REPLAY.
"""

from .converge import expected_digests, latest_receipts
from .errors import E_REPLAY
from .manifest import Manifest
from .planner import compile_plan


def replay_check(store, version=None):
    version = store.committed_version() if version is None else version
    problems = []

    manifest_body = store.get_manifest(version)
    if manifest_body is None:
        return {
            "ok": False,
            "code": E_REPLAY,
            "version": version,
            "problems": [{"kind": "version_missing", "version": version,
                          "nodes": [], "items": []}],
        }

    stored_plan = store.get_plan(version)
    recomputed = compile_plan(Manifest.from_dict(manifest_body))
    if stored_plan != recomputed:
        problems.append({"kind": "plan_mismatch", "version": version,
                         "nodes": sorted(set(stored_plan["assignments"])
                                         ^ set(recomputed["assignments"])),
                         "items": []})

    statuses = store.node_statuses()
    assignments = recomputed["assignments"]
    degraded = sorted(n for n in assignments if statuses.get(n) == "degraded")
    for node in degraded:
        problems.append({
            "kind": "node_degraded",
            "version": version,
            "nodes": [node],
            "items": sorted(assignments[node]),
        })

    expected = expected_digests(recomputed)
    latest = latest_receipts(store.receipts(version))
    for node in sorted(assignments):
        if node in degraded:
            continue
        receipt = latest.get(node)
        if receipt is None:
            problems.append({"kind": "receipt_missing", "version": version,
                             "nodes": [node],
                             "items": sorted(assignments[node])})
        elif receipt["digest"] != expected[node]:
            problems.append({"kind": "digest_mismatch", "version": version,
                             "nodes": [node],
                             "items": sorted(assignments[node])})

    if problems:
        return {"ok": False, "code": E_REPLAY, "version": version,
                "problems": problems}
    return {"ok": True, "version": version,
            "nodes": sorted(assignments), "expected": expected}
