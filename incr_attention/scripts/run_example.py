"""End-to-end incremental-attention example driver.

Applies the generated append/replace edit script under a recompute budget,
then changes block size to prove the accumulation chain is rebuilt, and
finally audits every block against the float64 serial reference.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from incrattn.audit import audit_engine, verify_equivalence  # noqa: E402
from incrattn.config import load_config  # noqa: E402
from incrattn.engine import IncrementalAttentionEngine  # noqa: E402
from incrattn.report import write_report_atomic  # noqa: E402


def resolve(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def load_dataset(name: str):
    base = os.path.join(ROOT, "samples", name)
    with open(os.path.join(base, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    blocks = os.path.join(base, "blocks")
    queries = np.load(os.path.join(blocks, "q.npz"))["queries"]
    final_kv = np.load(os.path.join(blocks, "final_kv.npz"))
    return manifest, queries, blocks, final_kv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="small")
    parser.add_argument("--config", default="configs/engine_serial.json")
    parser.add_argument("--reblock-size", type=int, default=3)
    parser.add_argument("--report", default="reports/example_report.json")
    args = parser.parse_args()

    manifest, queries, blocks_dir, final_kv = load_dataset(args.dataset)
    config = load_config(resolve(args.config))
    config = config.replace(snapshot_path=resolve(config.snapshot_path))
    if os.path.exists(config.snapshot_path):
        os.unlink(config.snapshot_path)

    engine = IncrementalAttentionEngine(config, queries)
    batch_reports = []

    for edit in manifest["edits"]:
        payload = np.load(os.path.join(blocks_dir, edit["file"]))
        if edit["op"] == "append":
            invalidated = engine.append_kv(
                payload["key_block"], payload["value_block"]
            )
        else:
            invalidated = engine.replace_kv_block(
                edit["kv_block_index"],
                payload["key_block"],
                payload["value_block"],
            )
        while True:
            report = engine.run_batch()
            report["trigger"] = edit
            report["invalidated_by_edit"] = invalidated
            batch_reports.append(report)
            if engine.state.pending or engine.state.failures:
                continue
            break

    equivalence_before = verify_equivalence(engine)
    audit_before = audit_engine(engine)

    # Block-size change must rebuild boundaries and invalidate everything.
    reblock_invalidated = engine.change_block_size(args.reblock_size)
    reblock_batches = []
    while True:
        report = engine.run_batch()
        reblock_batches.append(report)
        if not engine.state.pending and not engine.state.failures:
            break
    equivalence_after = verify_equivalence(engine)
    audit_after = audit_engine(engine)

    final_reference = np.load(os.path.join(blocks_dir, "reference.npz"))
    reference_check = {
        "reference_fingerprints_match": all(
            engine.state.results[index].fingerprint
            == str(final_reference["fingerprints"][index])
            for index in sorted(engine.state.results)
        ),
        "note": (
            "reference.npz uses the original block_size; strict fingerprint "
            "match is asserted before the reblock step"
        ),
        "pre_reblock_match": equivalence_before["equivalent"],
        "post_reblock_match": equivalence_after["equivalent"],
    }

    summary = {
        "dataset": args.dataset,
        "manifest": manifest,
        "config": {
            "block_size": config.block_size,
            "recompute_budget": config.recompute_budget,
            "max_workers": config.max_workers,
            "snapshot_path": config.snapshot_path,
        },
        "incremental_batches": batch_reports,
        "reblock": {
            "new_block_size": args.reblock_size,
            "invalidated": reblock_invalidated,
            "batches": reblock_batches,
        },
        "equivalence_before_reblock": equivalence_before,
        "equivalence_after_reblock": equivalence_after,
        "audit_before_reblock": audit_before,
        "audit_after_reblock": audit_after,
        "reference_check": reference_check,
        "final_global_version": engine.state.global_version,
        "final_recovery_point": engine.state.last_recovery_point,
    }
    report_path = resolve(args.report)
    write_report_atomic(report_path, summary)
    print(json.dumps({
        "report": report_path,
        "pre_reblock_equivalent": equivalence_before["equivalent"],
        "post_reblock_equivalent": equivalence_after["equivalent"],
        "audit_ok_before": audit_before["all_within_tolerance"],
        "audit_ok_after": audit_after["all_within_tolerance"],
        "batches": len(batch_reports) + len(reblock_batches),
        "global_version": engine.state.global_version,
    }, indent=2))


if __name__ == "__main__":
    main()
