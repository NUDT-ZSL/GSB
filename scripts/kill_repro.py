#!/usr/bin/env python3
"""强制终止复现脚本: 验证崩溃恢复只重放未完成部分且结果逐位一致。

用法:
  python3 scripts/kill_repro.py all     # clean -> victim(SIGKILL) -> resume -> 比对
  python3 scripts/kill_repro.py clean|victim|resume --db PATH
"""

import argparse
import json
import os
import signal
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alertcorr.engine import Engine

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "samples")


def _load_samples():
    with open(os.path.join(SAMPLES, "topology.json"), encoding="utf-8") as f:
        topo = json.load(f)
    with open(os.path.join(SAMPLES, "events.json"), encoding="utf-8") as f:
        events = json.load(f)["events"]
    return topo, events


def _reset_db(db):
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.unlink(db + suffix)


def run_clean(db):
    _reset_db(db)
    topo, events = _load_samples()
    eng = Engine(db)
    eng.load_topology(topo)
    eng.ingest(events)
    result = eng.run()
    print("clean sha256: %s" % result["sha"])
    eng.close()
    return result["sha"]


def run_victim(db):
    """摄取一半事件并提交, 然后在第二次结果提交的中途被 SIGKILL。"""
    _reset_db(db)
    topo, events = _load_samples()
    eng = Engine(db)
    eng.load_topology(topo)
    half = len(events) // 2
    eng.ingest(events[:half])
    eng.run()
    eng.ingest(events[half:])
    topo_obj = eng._topology()
    stored = eng.store.load_events()
    commit_id = "results-v%d-e%d" % (topo_obj.version, len(stored))
    eng.store.begin_commit(commit_id, "results")
    clusters, candidates, decisions = eng._compute_all(topo_obj, stored)
    eng.store.stage_clusters(clusters)
    eng.store.stage_candidates(candidates)
    eng.store.stage_decisions(decisions)
    eng.store.conn.close()
    os.kill(os.getpid(), signal.SIGKILL)


def run_resume(db):
    eng = Engine(db)
    pending = eng.recover()
    print("recovered pending commits: %s" % [p["commit_id"] for p in pending])
    result = eng.run()
    print("resume sha256: %s" % result["sha"])
    eng.close()
    return result["sha"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["all", "clean", "victim", "resume"])
    parser.add_argument("--db", default="out/kill_repro.db")
    args = parser.parse_args()

    if args.mode == "clean":
        run_clean(args.db)
        return 0
    if args.mode == "victim":
        run_victim(args.db)
        return 0
    if args.mode == "resume":
        run_resume(args.db)
        return 0

    clean_sha = run_clean(args.db)
    here = os.path.abspath(__file__)
    proc = subprocess.run([sys.executable, here, "victim", "--db", args.db])
    print("victim exit code: %d (expected non-zero, killed)" % proc.returncode)
    resume_sha = run_resume(args.db)
    if resume_sha == clean_sha:
        print("PASS: resumed result identical to clean run")
        return 0
    print("FAIL: sha mismatch clean=%s resumed=%s" % (clean_sha, resume_sha))
    return 1


if __name__ == "__main__":
    sys.exit(main())
