"""Demonstrate recovery from a fully replaced snapshot after a hard stop.

The engine is killed mid-convergence (simulated by simply dropping the
object). A brand-new process-level engine then restores only from the
atomic snapshot, drains the recompute queue and reaches full equivalence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from incrattn.audit import verify_equivalence  # noqa: E402
from incrattn.config import EngineConfig  # noqa: E402
from incrattn.engine import IncrementalAttentionEngine  # noqa: E402
from incrattn.report import write_report_atomic  # noqa: E402


def resolve(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="medium")
    parser.add_argument("--report", default="reports/recovery_report.json")
    args = parser.parse_args()

    base = os.path.join(ROOT, "samples", args.dataset)
    manifest = json.load(open(os.path.join(base, "manifest.json"), encoding="utf-8"))
    blocks_dir = os.path.join(base, "blocks")
    queries = np.load(os.path.join(blocks_dir, "q.npz"))["queries"]
    snapshot_path = resolve("snapshots/recovery.npz")
    if os.path.exists(snapshot_path):
        os.unlink(snapshot_path)
    config = EngineConfig(
        block_size=manifest["block_size"],
        recompute_budget=1,
        max_workers=2,
        snapshot_path=snapshot_path,
        scale=manifest["scale"],
    )

    engine = IncrementalAttentionEngine(config, queries)
    for edit in manifest["edits"]:
        payload = np.load(os.path.join(blocks_dir, edit["file"]))
        if edit["op"] == "append":
            engine.append_kv(payload["key_block"], payload["value_block"])
        else:
            engine.replace_kv_block(
                edit["kv_block_index"], payload["key_block"], payload["value_block"]
            )

    pre_stop = []
    # Simulate a forced termination while blocks are still pending.
    while len(engine.state.results) < 2:
        pre_stop.append(engine.run_batch())
    stopped_at = {
        "global_version": engine.state.global_version,
        "completed": sorted(engine.state.results),
        "pending": list(engine.state.pending),
        "recovery_point": engine.state.last_recovery_point,
    }
    del engine

    restored = IncrementalAttentionEngine.restore(config)
    post_restore = []
    while restored.state.pending or restored.state.failures:
        post_restore.append(restored.run_batch())
    equivalence = verify_equivalence(restored)
    payload = {
        "dataset": args.dataset,
        "stopped_at": stopped_at,
        "restored_at": {
            "global_version": restored.state.global_version,
            "completed": sorted(restored.state.results),
            "recovery_point": restored.state.last_recovery_point,
        },
        "pre_stop_batches": pre_stop,
        "post_restore_batches": post_restore,
        "equivalence": equivalence,
    }
    path = write_report_atomic(resolve(args.report), payload)
    print(json.dumps({
        "report": path,
        "stopped_version": stopped_at["global_version"],
        "restored_version": restored.state.global_version,
        "equivalent": equivalence["equivalent"],
    }, indent=2))


if __name__ == "__main__":
    main()
