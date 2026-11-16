"""Deterministic local demo, executed in a fixed order:

  PHASE 1: normal first-distribution session (commit v1, replay OK)
  PHASE 2: fault injection (node crash) + simulated interrupt + recovery
  PHASE 3: version upgrade with whole-snapshot replacement + replay checks

Run: python demo.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from configdist.replay import replay_check
from configdist.session import Session, SimulatedInterrupt
from configdist.store import Store
from configdist.verify import verify_db

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "out")
REGISTRY = os.path.join(ROOT, "samples", "registry.json")
MANIFEST_V1 = os.path.join(ROOT, "samples", "manifest_v1.json")
MANIFEST_V2 = os.path.join(ROOT, "samples", "manifest_v2.json")


def emit(title, obj):
    print("=== %s ===" % title)
    print(json.dumps(obj, indent=2, sort_keys=True))
    print()


def phase1():
    db = os.path.join(OUT, "normal.db")
    store = Store(db)
    session = Session(store, REGISTRY)
    prep = session.prepare(MANIFEST_V1)
    result = session.distribute(concurrency=3, timeout=2.0, max_retries=1)
    replay = replay_check(store)
    store.close()
    verify = verify_db(db)
    emit("PHASE 1 normal session (v1 full)", {
        "prepared_version": prep["version"],
        "snapshot_replaced": prep["snapshot_replaced"],
        "distribute_ok": result["ok"],
        "committed_version": result["commit"]["version"],
        "acked_nodes": result["convergence"]["acked"],
        "replay_ok": replay["ok"],
        "verify_ok": verify["ok"],
    })


def phase2():
    db = os.path.join(OUT, "fault.db")
    store = Store(db)
    session = Session(store, REGISTRY)
    session.prepare(MANIFEST_V1)
    interrupted = None
    try:
        session.distribute(fail_modes={"node-b": "crash"},
                           concurrency=3, timeout=2.0, max_retries=1,
                           interrupt_after_dispatch=True)
    except SimulatedInterrupt as exc:
        interrupted = str(exc)
    store.close()  # simulate platform process death; snapshot persists

    store2 = Store(db)
    session2 = Session(store2, REGISTRY)
    view = session2.recover()
    resumed = session2.resume(concurrency=3, timeout=2.0, max_retries=1)
    committed = store2.committed_version()
    store2.close()
    verify = verify_db(db)
    emit("PHASE 2 fault injection + interrupt + recovery (v1)", {
        "interrupt": interrupted,
        "recovered_state": view["state"],
        "recovered_node_status": view["node_status"],
        "resume_ok": resumed["ok"],
        "resume_code": resumed.get("code"),
        "commit_blocked_reason": resumed["commit"].get("reason"),
        "deviations": resumed["convergence"].get("deviations"),
        "committed_version_after_recovery": committed,
        "verify_ok": verify["ok"],
        "verify_codes": sorted({c["code"] for c in verify["checks"]
                                if c["code"]}),
    })


def phase3():
    db = os.path.join(OUT, "fault.db")  # continue on the recovered database
    store = Store(db)
    session = Session(store, REGISTRY)
    prep = session.prepare(MANIFEST_V2)
    result = session.distribute(concurrency=2, timeout=2.0, max_retries=1)
    idempotent = session.sm.commit(2, [], True, [])
    no_regress = store.set_committed_version(1)
    verify_after_commit = verify_db(db)

    store.set_node_status("node-c", "degraded")  # post-commit fault
    replay_degraded = replay_check(store)
    replay_missing = replay_check(store, version=1)
    committed = store.committed_version()
    store.close()
    emit("PHASE 3 upgrade to v2 (shard) + replay checks", {
        "prepared_version": prep["version"],
        "snapshot_replaced": prep["snapshot_replaced"],
        "distribute_ok": result["ok"],
        "committed_version": committed,
        "idempotent_recommit": idempotent,
        "committed_version_regress_allowed": no_regress,
        "verify_ok_after_commit": verify_after_commit["ok"],
        "replay_v2_with_degraded_node": {
            "ok": replay_degraded["ok"],
            "code": replay_degraded.get("code"),
            "problems": replay_degraded.get("problems"),
        },
        "replay_v1_after_snapshot_replace": {
            "ok": replay_missing["ok"],
            "code": replay_missing.get("code"),
            "problems": replay_missing.get("problems"),
        },
    })


def main():
    os.makedirs(OUT, exist_ok=True)
    for name in ("normal.db", "fault.db"):
        path = os.path.join(OUT, name)
        if os.path.exists(path):
            os.remove(path)
    phase1()
    phase2()
    phase3()
    print("demo finished: summaries emitted in fixed order "
          "(phase1 -> phase2 -> phase3)")


if __name__ == "__main__":
    main()
