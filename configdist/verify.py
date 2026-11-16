"""Result verification tool.

Reads a session database and checks the correspondence between version
evolution, distribution plan, receipts and final state; prints a
structured summary. CLI: python -m configdist.verify <db-path>
"""

import argparse
import json
import sys

from .converge import check_convergence
from .errors import E_CONVERGE, E_NODE, E_REPLAY
from .manifest import Manifest
from .planner import compile_plan
from .replay import replay_check
from .statemachine import ALLOWED
from .store import Store


def verify_db(db_path):
    store = Store(db_path)
    checks = []

    def add(name, ok, code=None, details=None):
        checks.append({"name": name, "ok": bool(ok),
                       "code": code, "details": details or {}})

    versions = store.manifest_versions()
    add("versions_monotonic_unique",
        versions == sorted(set(versions)) and len(versions) == len(set(versions)),
        details={"versions": versions})

    plan_ok, plan_details = True, {}
    for version in versions:
        manifest = store.get_manifest(version)
        plan = store.get_plan(version)
        if plan is None:
            plan_ok = False
            plan_details[str(version)] = "plan missing"
            continue
        recomputed = compile_plan(Manifest.from_dict(manifest))
        if recomputed["assignments"] != plan["assignments"]:
            plan_ok = False
            plan_details[str(version)] = "plan does not match manifest"
        if sorted(plan["assignments"]) != sorted(manifest["targets"]):
            plan_ok = False
            plan_details[str(version)] = "plan targets differ from manifest"
    add("plan_matches_manifest", plan_ok, details=plan_details)

    receipt_ok, receipt_details = True, {}
    for version in versions:
        plan = store.get_plan(version)
        if plan is None:
            continue
        for receipt in store.receipts(version):
            if receipt["node"] not in plan["assignments"]:
                receipt_ok = False
                receipt_details.setdefault(str(version), []).append(
                    "receipt from non-target node %s" % receipt["node"])
    add("receipts_match_plan", receipt_ok, details=receipt_details)

    committed = store.committed_version()
    final_ok, final_details, final_code = True, {}, None
    if committed:
        plan = store.get_plan(committed)
        if plan is None:
            final_ok = False
            final_details["reason"] = "committed plan missing"
        else:
            conv = check_convergence(plan, store.receipts(committed))
            final_ok = conv["ok"]
            if not conv["ok"]:
                final_code = E_CONVERGE
                final_details = {"missing": conv["missing"],
                                 "mismatched": conv["mismatched"]}
    add("final_state_converged", final_ok, code=final_code,
        details=final_details)

    transitions = store.transitions()
    path_ok, prev = True, "init"
    for tr in transitions:
        if tr["from"] != prev or (tr["from"], tr["to"]) not in ALLOWED:
            path_ok = False
            break
        prev = tr["to"]
    add("transitions_legal", path_ok,
        details={"path": ["%s->%s" % (t["from"], t["to"]) for t in transitions]})

    terminal_ok = not transitions or transitions[-1]["to"] == "committed"
    add("session_committed", terminal_ok,
        code=None if terminal_ok else E_CONVERGE,
        details={"final_state": prev if transitions else "init"})

    degraded = sorted(n for n, s in store.node_statuses().items()
                      if s == "degraded")
    add("node_degradation_recorded", True, code=E_NODE if degraded else None,
        details={"degraded": degraded})

    replay = replay_check(store) if committed else {"ok": True, "version": 0}
    add("replay_consistent", replay["ok"],
        code=None if replay["ok"] else E_REPLAY,
        details=replay if not replay["ok"] else {"version": replay["version"]})

    summary = {
        "db": db_path,
        "ok": all(c["ok"] for c in checks),
        "committed_version": committed,
        "snapshot_version": store.snapshot_version(),
        "checks": checks,
    }
    store.close()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="verify a session database")
    parser.add_argument("db")
    args = parser.parse_args(argv)
    summary = verify_db(args.db)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
