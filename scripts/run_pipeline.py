#!/usr/bin/env python3
"""端到端流水线: 校验 -> 拓扑加载 -> 事件摄取 -> 降噪+关联 -> 增量补丁。

用法: python3 scripts/run_pipeline.py [--db out/pipeline.db] [--json]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alertcorr.engine import Engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="out/pipeline.db")
    parser.add_argument("--samples", default="samples")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(args.db + suffix):
            os.unlink(args.db + suffix)

    with open(os.path.join(args.samples, "topology.json"),
              encoding="utf-8") as f:
        topo = json.load(f)
    with open(os.path.join(args.samples, "events.json"),
              encoding="utf-8") as f:
        events = json.load(f)["events"]
    with open(os.path.join(args.samples, "patches.json"),
              encoding="utf-8") as f:
        patches = json.load(f)["patches"]

    eng = Engine(args.db)
    eng.load_topology(topo)
    eng.ingest(events)
    result = eng.run()
    print("== initial run ==")
    print("clusters=%d suppressed=%d candidate_lists=%d"
          % (len(result["clusters"]),
             sum(c["suppressed_count"]
                 for c in result["clusters"].values()),
             len(result["candidates"])))
    for domain, dec in sorted(result["decisions"].items()):
        print("  domain=%s mode=%s reason=%s"
              % (domain, dec["mode"], dec["rejected_reason"]))
    for cid, cands in sorted(result["candidates"].items()):
        top = cands[0]
        print("  top1 %s <- %s (score=%d dist=%d dt=%dms path=%s)"
              % (cid, top["candidate_cluster_id"], top["score"],
                 top["distance"], top["dt_ms"], ">".join(top["path"])))

    reports = []
    for patch in patches:
        report = eng.apply_patch(patch)
        reports.append(report)
        print("== patch %s ==" % report["patch_id"])
        print("  version %d -> %d, affected=%s"
              % (report["from_version"], report["to_version"],
                 report["affected_services"]))
        print("  recomputed clusters=%d candidates=%d; reused clusters=%d "
              "candidates=%d"
              % (report["recomputed_clusters"],
                 report["recomputed_candidates"],
                 report["reused_clusters"], report["reused_candidates"]))

    final = eng.dump()
    print("final sha256: %s" % final["sha"])
    if args.json:
        out = {"initial": result, "patch_reports": reports, "final": final}
        path = os.path.join("out", "pipeline_result.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True)
        print("written: %s" % path)
    eng.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
