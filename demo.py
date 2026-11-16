#!/usr/bin/env python3
"""Deterministic local demo of the scheduling simulation workbench.

Scenarios (in order):
  1. initial scheduling over the sample manifests, snapshot saved
  2. weights modified -> full re-scheduling
  3. snapshot restored into a fresh session -> replay verification

Fault injection (only alters inputs/parameters):
  --inject manifest   load a corrupted node manifest   -> E_MANIFEST
  --inject affinity   load pods with a keyRef cycle    -> E_AFFINITY
  --inject stats      strip image statistics from nodes -> E_STATS

No network, cluster, or external account is used.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

from scheduler_sim.errors import SimError
from scheduler_sim.report import DiagLog, render_run_summary
from scheduler_sim.session import Session, load_weights

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "samples"


def print_block(title, lines):
    print(f"===== {title} =====")
    for line in lines:
        print(line)
    print()


def run_demo(out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    log = DiagLog(out_dir / "diagnostics.jsonl")
    weights = load_weights(SAMPLES / "weights.yaml")
    snapshot_path = out_dir / "snapshot.json"

    # Scenario 1: initial scheduling + snapshot.
    s1 = Session(weights)
    s1.load_manifest_files(SAMPLES / "nodes.json", SAMPLES / "pods.json")
    result1 = s1.run()
    for e in s1.events:
        log.emit(e["code"], "plugin statistics unavailable",
                 pod=e["pod"], node=e["node"], plugin=e["plugin"])
    s1.save_snapshot(snapshot_path)
    print_block("SCENARIO 1: initial scheduling "
                f"(nodes={s1.nodes_version} pods={s1.pod_queue_version})",
                render_run_summary(result1))

    # Scenario 2: modified weights -> full re-scheduling.
    s2 = Session(load_weights(SAMPLES / "weights_alt.yaml"))
    s2.load_manifest_files(SAMPLES / "nodes.json", SAMPLES / "pods.json")
    result2 = s2.run()
    changed = {p: (result1["bindings"].get(p), result2["bindings"].get(p))
               for p in result2["bindings"]
               if result1["bindings"].get(p) != result2["bindings"].get(p)}
    lines = render_run_summary(result2)
    lines.append("binding changes vs scenario 1: "
                 + (json.dumps(changed, sort_keys=True) if changed
                    else "none"))
    print_block("SCENARIO 2: re-scheduling with modified weights", lines)

    # Scenario 3: restore snapshot into a fresh session -> replay.
    s3 = Session(load_weights(SAMPLES / "weights_alt.yaml"))  # differs on purpose
    s3.load_manifest_files(SAMPLES / "nodes.json", SAMPLES / "pods.json")
    s3.run()
    actions = s3.restore(snapshot_path)
    print_block("SCENARIO 3: snapshot restore + replay verification",
                [f"  - {a}" for a in actions]
                + ["  restored weights: "
                   + json.dumps(s3.weights, sort_keys=True),
                   "  restored bindings: "
                   + json.dumps(s3.result["bindings"], sort_keys=True)])

    log.flush()
    print(f"diagnostics log: {out_dir / 'diagnostics.jsonl'}")
    print(f"snapshot file:   {snapshot_path}")
    return 0


def run_injection(kind, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    log = DiagLog(out_dir / "diagnostics.jsonl")
    try:
        if kind == "manifest":
            s = Session()
            s.load_manifest_files(SAMPLES / "broken" / "nodes_broken.json",
                                  SAMPLES / "pods.json")
        elif kind == "affinity":
            s = Session()
            s.load_manifest_files(SAMPLES / "nodes.json",
                                  SAMPLES / "broken"
                                  / "pods_affinity_cycle.json")
        elif kind == "stats":
            s = Session()
            s.load_manifest_files(SAMPLES / "nodes.json",
                                  SAMPLES / "pods.json")
            for node in s._initial_nodes:  # input-only mutation
                node.images = None
            result = s.run()
            for e in s.events:
                log.emit(e["code"], "plugin statistics unavailable",
                         pod=e["pod"], node=e["node"], plugin=e["plugin"])
            print_block("INJECTION stats: image statistics stripped",
                        render_run_summary(result))
        else:  # pragma: no cover
            raise ValueError(kind)
    except SimError as exc:
        log.emit(exc.code, str(exc), entry=exc.entry, field=exc.field)
        log.flush()
        print(f"fault injected: {kind}")
        print(f"business code:  {exc.code}")
        print(f"detail:         {exc}")
        print(f"diagnostics log: {out_dir / 'diagnostics.jsonl'}")
        return 1
    log.flush()
    print(f"fault injected: {kind} (see diagnostics log)")
    print(f"diagnostics log: {out_dir / 'diagnostics.jsonl'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inject", choices=["manifest", "affinity", "stats"],
                        help="inject one fault class and report its code")
    parser.add_argument("--out", default=str(ROOT / "out"),
                        help="output directory for snapshot and logs")
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    if args.inject:
        return run_injection(args.inject, out_dir)
    return run_demo(out_dir)


if __name__ == "__main__":
    sys.exit(main())
