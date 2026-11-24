#!/usr/bin/env python3
"""Check every batch permutation and concurrency level produces one signature."""

from __future__ import annotations

import argparse
import sys
from itertools import permutations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sensitivity_engine.atomicio import atomic_write_json  # noqa: E402
from sensitivity_engine.hashing import canonical_json  # noqa: E402
from sensitivity_engine.loader import load_dataset  # noqa: E402
from sensitivity_engine.service import import_dataset  # noqa: E402
from sensitivity_engine.store import Store  # noqa: E402


def signature(data_dir: Path, batch_order, concurrency: int) -> dict:
    dataset = load_dataset(data_dir)
    by_id = {batch.batch_id: batch for batch in dataset.batches}
    dataset.batches = [by_id[batch_id] for batch_id in batch_order]
    store = Store(":memory:")
    try:
        import_dataset(store, dataset, concurrency=concurrency, queue_limit=8)
        reentry = store.export_reentry_report()
        query = store.query_ledger()
        return {
            "ledger_digest": store.ledger_digest(),
            "reentry_digest": canonical_json(reentry),
            "query_digest": canonical_json(query),
            "rejection_digest": canonical_json(store.list_rejections()),
            "reentry_set": reentry["reentry_set"],
            "changed_object_set": reentry["changed_object_set"],
            "alternative_evidence": reentry["alternative_evidence"],
            "rejections": store.list_rejections(),
        }
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(ROOT / "data"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "order-independence.json"))
    args = parser.parse_args()

    dataset = load_dataset(args.data)
    ids = [batch.batch_id for batch in dataset.batches]
    results = []
    signatures = set()
    for order in permutations(ids):
        for concurrency in (1, 4):
            result = signature(Path(args.data), order, concurrency)
            signatures.add(
                (
                    result["ledger_digest"],
                    result["reentry_digest"],
                    result["query_digest"],
                    result["rejection_digest"],
                )
            )
            results.append(
                {
                    "batch_order": list(order),
                    "concurrency": concurrency,
                    "ledger_digest": result["ledger_digest"],
                    "reentry_set": result["reentry_set"],
                    "changed_object_set": result["changed_object_set"],
                }
            )

    payload = {
        "report_type": "sensitivity-order-independence",
        "ok": len(signatures) == 1,
        "permutation_runs": len(results),
        "unique_signatures": len(signatures),
        "canonical_batch_order": sorted(ids),
        "runs": results,
    }
    atomic_write_json(args.out, payload)
    print(canonical_json(payload)[: 1200] + "\n...")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
