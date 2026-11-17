"""Run every configured fault injection in isolation and record hits."""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from incrattn.config import EngineConfig  # noqa: E402
from incrattn.engine import IncrementalAttentionEngine  # noqa: E402
from incrattn.errors import (  # noqa: E402
    E_NON_FINITE_INPUT,
    E_SNAPSHOT_STALE,
    IncrementalAttentionError,
)
from incrattn.injection import InjectionConfig  # noqa: E402
from incrattn.report import write_report_atomic  # noqa: E402
from incrattn.snapshot import tamper_snapshot_version  # noqa: E402


def resolve(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def load_dataset(name: str):
    base = os.path.join(ROOT, "samples", name)
    blocks = os.path.join(base, "blocks")
    manifest = json.load(open(os.path.join(base, "manifest.json"), encoding="utf-8"))
    queries = np.load(os.path.join(blocks, "q.npz"))["queries"]
    edits = []
    for edit in manifest["edits"]:
        payload = np.load(os.path.join(blocks, edit["file"]))
        edits.append((edit, payload))
    return manifest, queries, edits


def base_config(snapshot_name, budget=2, block_size=4, max_workers=2):
    return EngineConfig(
        block_size=block_size,
        recompute_budget=budget,
        max_workers=max_workers,
        snapshot_path=resolve(f"snapshots/{snapshot_name}"),
    )


def run_item(item, queries, edits):
    kind = item["kind"]
    record = {"id": item["id"], "kind": kind, "hit": False, "details": None}
    try:
        if kind == "nonfinite_input":
            config = base_config(f"inj_{item['id']}.npz")
            engine = IncrementalAttentionEngine(
                config, queries, injection=InjectionConfig([item])
            )
            edit, payload = edits[0]
            key_block = payload["key_block"].copy()
            value = np.nan if item["value"] == "nan" else np.inf
            key_block[item["row"], item["col"]] = value
            engine.append_kv(key_block, payload["value_block"])
            report = engine.run_batch()
            codes = {failure["code"] for failure in report["failures"]}
            record["hit"] = E_NON_FINITE_INPUT in codes
            record["details"] = report["failures"]
        elif kind == "length_mismatch":
            config = base_config(f"inj_{item['id']}.npz")
            engine = IncrementalAttentionEngine(config, queries)
            edit, payload = edits[0]
            bad_key = np.zeros(
                (payload["key_block"].shape[0], queries.shape[1] + 1),
                dtype=np.float32,
            )
            try:
                engine.append_kv(bad_key, payload["value_block"])
            except IncrementalAttentionError as exc:
                record["hit"] = True
                record["details"] = {"code": exc.code, "type": type(exc).__name__}
        elif kind == "zero_block_size":
            config = base_config(f"inj_{item['id']}.npz").replace(
                block_size=item["block_size"]
            )
            try:
                IncrementalAttentionEngine(config, queries)
            except IncrementalAttentionError as exc:
                record["hit"] = True
                record["details"] = {"code": exc.code, "type": type(exc).__name__}
        elif kind == "zero_budget":
            config = base_config(f"inj_{item['id']}.npz", budget=0)
            injection = InjectionConfig([item])
            engine = IncrementalAttentionEngine(config, queries, injection=injection)
            edit, payload = edits[0]
            engine.append_kv(payload["key_block"], payload["value_block"])
            report = engine.run_batch()
            expected_pending = [0, 1, 2]
            record["hit"] = (
                report["budget_consumed"] == 0
                and [item["block_index"] for item in report["pending"]]
                == expected_pending
                and all(
                    item["code"] == "E_RECOMPUTE_PENDING"
                    for item in report["pending"]
                )
            )
            record["details"] = {
                "pending": report["pending"],
                "global_version": report["global_version"],
            }
            # The zero-budget batch only queues work; a later batch with a
            # normal budget drains the same queue and converges.
            engine.state.config = engine.state.config.replace(recompute_budget=4)
            follow_up = engine.run_batch()
            record["details"]["converged_on_next_batch"] = follow_up["converged"]
        elif kind == "snapshot_tamper":
            config = base_config(f"inj_{item['id']}.npz")
            engine = IncrementalAttentionEngine(config, queries)
            edit, payload = edits[0]
            engine.append_kv(payload["key_block"], payload["value_block"])
            engine.run_batch()
            tamper_snapshot_version(
                config.snapshot_path, item["tampered_version"]
            )
            try:
                IncrementalAttentionEngine.restore(config)
            except IncrementalAttentionError as exc:
                record["hit"] = exc.code == E_SNAPSHOT_STALE
                record["details"] = {"code": exc.code, "type": type(exc).__name__}
        elif kind == "stage_raise":
            config = base_config(f"inj_{item['id']}.npz", budget=4)
            injection = InjectionConfig([item])
            engine = IncrementalAttentionEngine(
                config, queries, injection=injection
            )
            for edit, payload in edits[:2]:
                if edit["op"] == "append":
                    engine.append_kv(payload["key_block"], payload["value_block"])
            report = engine.run_batch()
            record["hit"] = item["id"] in report["injections_fired"]
            record["details"] = report["failures"]
        else:
            record["details"] = "unknown injection kind"
    except IncrementalAttentionError as exc:
        record["hit"] = True
        record["details"] = {
            "code": exc.code,
            "type": type(exc).__name__,
            "message": str(exc),
        }
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="small")
    parser.add_argument(
        "--injections", default="configs/injections.json"
    )
    parser.add_argument("--report", default="reports/injection_report.json")
    args = parser.parse_args()

    injection_config = InjectionConfig.load(resolve(args.injections))
    _, queries, edits = load_dataset(args.dataset)
    results = [run_item(item, queries, edits) for item in injection_config.items]
    payload = {
        "dataset": args.dataset,
        "total": len(results),
        "hits": sum(1 for item in results if item["hit"]),
        "results": results,
    }
    path = write_report_atomic(resolve(args.report), payload)
    print(json.dumps({"report": path, **{
        key: payload[key] for key in ("total", "hits")
    }}, indent=2))
    if payload["hits"] != payload["total"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
