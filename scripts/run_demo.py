"""End-to-end acceptance demo for the xrel application.

Runs deterministic samples through every required capability and writes a
verification report under ``snapshots/demo_report.json``::

    python scripts/run_demo.py
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xrel.config import default_config  # noqa: E402
from xrel.concurrency import ChangeSet  # noqa: E402
from xrel.data_gen import (  # noqa: E402
    DEFAULT_SEED,
    generate_bridge,
    generate_documents,
    generate_queries,
)
from xrel.model import Document, DocTerm  # noqa: E402
from xrel.pipeline import XrelPipeline  # noqa: E402
from xrel.snapshot import attach_checksum  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default=str(ROOT / "snapshots"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    bridge = attach_checksum(generate_bridge(args.seed, version=1))
    documents = generate_documents(args.seed)
    queries = generate_queries(args.seed)

    pipeline = XrelPipeline(
        workdir / "snapshots", workdir / "results", default_config("float64")
    )
    pipeline.load_inputs(documents, queries, bridge)

    report: dict = {"seed": args.seed, "precision_tiers": {}, "increments": {}}

    # 1. Three precision tiers + error spectrum for one query.
    query_id = queries[0].query_id
    for precision in ("float64", "float32", "fixed"):
        record = pipeline.query_by_id(query_id, precision)
        report["precision_tiers"][precision] = {
            "hit_count": len(record.result["scores"]),
            "topk": [
                {"doc_id": item["doc_id"], "score": item["score"], "grade": item["grade"]}
                for item in record.result["topk"]["selected"]
            ],
            "error_spectrum": None
            if record.result["error_spectrum"] is None
            else {
                "dominant_stage": record.result["error_spectrum"]["dominant_stage"],
                "fixed_recompute_ok": record.result["error_spectrum"]["fixed_recompute_ok"],
            },
            "cache_key": record.cache_key,
            "bridge_version": record.bridge_version,
            "node_path": record.node_path,
        }

    # 2. Query-side vs batch-side consistency.
    report["query_batch_self_check"] = {
        precision: pipeline.self_check_query_vs_batch(precision)
        for precision in ("float64", "float32", "fixed")
    }

    # 3a. Bridge entry correction.
    bridge_v2 = copy.deepcopy(bridge)
    bridge_v2.version = 2
    source_lang = queries[0].query_lang
    source_term = queries[0].terms[0]
    bridge_v2.entries[(source_lang, source_term)] = [
        (target, term, 0.99)
        for target, term, _ in bridge.entries[(source_lang, source_term)]
    ]
    attach_checksum(bridge_v2)
    report["increments"]["bridge_entry"] = pipeline.commit_increment(
        ChangeSet(
            kind="bridge_entry",
            source_lang=source_lang,
            source_terms=[source_term],
            target_langs=sorted({t for _, t in bridge.directions if _ == source_lang}),
        ),
        new_bridge=bridge_v2,
    )

    # 3b. Direction table addition.
    bridge_v3 = copy.deepcopy(bridge_v2)
    bridge_v3.version = 3
    bridge_v3.directions = sorted(set(bridge_v3.directions) | {("zh", "en")})
    attach_checksum(bridge_v3)
    report["increments"]["direction"] = pipeline.commit_increment(
        ChangeSet(kind="direction", source_lang="zh", target_langs=["en"]),
        new_bridge=bridge_v3,
    )

    # 3c. Document batch append.
    batch = [
        Document(
            doc_id="doc-en-900",
            doc_lang="en",
            terms=[DocTerm("en:t0001", 2), DocTerm("en:t0002", 1)],
            doc_len=3,
        )
    ]
    report["increments"]["document_batch"] = pipeline.commit_increment(
        ChangeSet(kind="document_batch", doc_langs=["en"]),
        new_documents=batch,
    )

    report_path = workdir / "demo_report.json"
    with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, sort_keys=True, indent=2, ensure_ascii=False)

    all_consistent = all(
        item["consistent"] for item in report["query_batch_self_check"].values()
    )
    all_bytes_equal = all(
        item["verification"]["byte_for_byte_equal_to_full_recompute"]
        for item in report["increments"].values()
    )
    print(f"report written to {report_path}")
    print(f"query/batch consistent across tiers: {all_consistent}")
    print(f"incremental byte-for-byte equal to full recompute: {all_bytes_equal}")
    return 0 if all_consistent and all_bytes_equal else 1


if __name__ == "__main__":
    raise SystemExit(main())
