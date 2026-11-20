"""Repeatable end-to-end demo of the graymesh middleware.

Runs the full scenario twice from a clean state and verifies both runs
produce byte-identical summaries:
  1. clean artifacts, start programmable loopback upstreams
  2. bootstrap rules v1, replay the fixed request trace
  3. apply patches p1..p3, replay the trace after each (version binding)
  4. reject invalid patches (missing ref, cycle) with business error codes
  5. idempotent patch replay + expected-version mismatch
  6. kill-style crash during commit and during mirroring, then recover
Usage: .venv/bin/python scripts/demo.py
"""

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from graymesh.clock import LogicalClock  # noqa: E402
from graymesh.controlplane import ControlPlane  # noqa: E402
from graymesh.dataplane import DataPlane  # noqa: E402
from graymesh.errors import BusinessError  # noqa: E402
from graymesh.store import RuleStore  # noqa: E402
from graymesh.upstream import ProgrammableUpstream  # noqa: E402

SAMPLES = os.path.join(ROOT, "samples")
ARTIFACTS = os.path.join(ROOT, "artifacts")


def load_sample(name):
    with open(os.path.join(SAMPLES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def run_crash_child(mode, db_path):
    env = dict(os.environ, PYTHONPATH=ROOT)
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "crash_child.py"), mode, db_path],
        env=env, capture_output=True, timeout=30)
    return proc.returncode


def run_scenario(workdir):
    os.makedirs(workdir, exist_ok=True)
    db_path = os.path.join(workdir, "graymesh.db")
    summary = {"phases": []}

    upstreams = [ProgrammableUpstream(u["host"], u["port"], u["routes"], u["name"]).start()
                 for u in load_sample("upstreams.json")["upstreams"]]
    try:
        clock = LogicalClock(start_ms=1_000_000, step_ms=7)
        store = RuleStore(db_path)
        dataplane = DataPlane(store, clock, timeout_ms=500)
        control = ControlPlane(store, clock, dataplane)

        rules = load_sample("rules_v1.json")
        bootstrap = {"patch_id": "bootstrap", "expected_version": 0,
                     "ops": [{"op": "add", "kind": o["kind"], "id": o["id"], "object": o}
                             for o in rules["objects"]]}
        result = control.submit_patch(bootstrap)
        summary["phases"].append({"phase": "bootstrap", "version": result["version"]})

        requests = load_sample("requests.json")["requests"]
        patches = load_sample("patches.json")["patches"]

        def replay(tag):
            chains = [dataplane.handle(req) for req in requests]
            dataplane.flush_mirrors()
            summary["phases"].append({
                "phase": tag, "version": store.current_version(),
                "decisions": chains,
            })

        replay("trace@v1")

        for patch in patches[:3]:
            result = control.submit_patch(patch)
            summary["phases"].append({"phase": "patch:%s" % patch["patch_id"],
                                      "version": result["version"],
                                      "invalidation": result["invalidation"]})
            replay("trace@v%d" % result["version"])

        for patch in patches[3:]:
            try:
                control.submit_patch(patch)
                outcome = {"error": None}
            except BusinessError as exc:
                outcome = {"error": exc.to_dict()}
            summary["phases"].append({"phase": "reject:%s" % patch["patch_id"],
                                      "expected_error": patch["expect_error"],
                                      "outcome": outcome,
                                      "version_after": store.current_version()})

        replay_result = control.submit_patch(patches[1])
        summary["phases"].append({"phase": "idempotent-replay",
                                  "applied": replay_result["applied"],
                                  "version": replay_result["version"]})

        try:
            control.submit_patch({"patch_id": "patch-006-wrong-version",
                                  "expected_version": 999,
                                  "ops": [{"op": "remove", "kind": "gray_route",
                                           "id": "r-canary"}]})
            mismatch = {"error": None}
        except BusinessError as exc:
            mismatch = {"error": exc.to_dict()}
        summary["phases"].append({"phase": "version-mismatch", "outcome": mismatch,
                                  "version_after": store.current_version()})

        version_before_crash = store.current_version()
        rc_torn = run_crash_child("torn-commit", db_path)
        rc_mirror = run_crash_child("mid-mirror", db_path)
        recovered = RuleStore(db_path)  # recover() runs in the constructor
        report = recovered.recover()  # second call must be a no-op
        dataplane.flush_mirrors()
        summary["phases"].append({
            "phase": "crash-recovery",
            "child_exit_codes": [rc_torn, rc_mirror],
            "version_before_crash": version_before_crash,
            "version_after_recovery": recovered.current_version(),
            "recovery_report_idempotent": report,
        })
        store = recovered

        summary["final"] = {
            "version": store.current_version(),
            "snapshot_digest": store.load_snapshot().digest,
            "decisions": store.list_decisions(),
            "mirrors": store.list_mirrors(),
            "invalidations": store.latest_invalidation(),
            "upstream_request_counts": {u.name: u.request_count for u in upstreams},
        }
        dataplane.close()
    finally:
        for upstream in upstreams:
            upstream.stop()

    summary_path = os.path.join(workdir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return summary_path


def main():
    if os.path.isdir(ARTIFACTS):
        shutil.rmtree(ARTIFACTS)
    os.makedirs(ARTIFACTS)

    first = run_scenario(os.path.join(ARTIFACTS, "run1"))
    second = run_scenario(os.path.join(ARTIFACTS, "run2"))

    with open(first, "rb") as fh:
        blob1 = fh.read()
    with open(second, "rb") as fh:
        blob2 = fh.read()

    print("run1 summary: %s (%d bytes)" % (first, len(blob1)))
    print("run2 summary: %s (%d bytes)" % (second, len(blob2)))
    if blob1 != blob2:
        print("FAIL: two runs differ")
        sys.exit(1)

    summary = json.loads(blob1.decode("utf-8"))
    final = summary["final"]
    recovery = [p for p in summary["phases"] if p["phase"] == "crash-recovery"][0]
    rejects = [p for p in summary["phases"] if p["phase"].startswith("reject:")]
    checks = [
        ("two runs byte-identical", True),
        ("final version == 4 (bootstrap + 3 patches)", final["version"] == 4),
        ("invalid patches rejected with expected codes",
         all(p["outcome"]["error"]
             and p["outcome"]["error"]["code"] == p["expected_error"] for p in rejects)),
        ("no version rollback after crash",
         recovery["version_after_recovery"] == recovery["version_before_crash"]),
        ("torn version removed on recovery",
         recovery["recovery_report_idempotent"]["torn_versions_removed"] == []),
        ("mirrors recorded", len(final["mirrors"]) > 0),
        ("decisions recorded", len(final["decisions"]) > 0),
    ]
    ok = True
    for name, passed in checks:
        print("%s %s" % ("PASS" if passed else "FAIL", name))
        ok = ok and passed
    print("DEMO %s" % ("OK" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
