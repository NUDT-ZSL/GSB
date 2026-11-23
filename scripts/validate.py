"""End-to-end acceptance validation with deterministic local samples."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recall_fusion.config import FusionConfig  # noqa: E402
from recall_fusion.errors import (  # noqa: E402
    ErrorCode,
    PipelineError,
)
from recall_fusion.pipeline import FusionPipeline  # noqa: E402
from recall_fusion.sample_data import (  # noqa: E402
    generate_dataset,
    load_dataset,
    write_default_config,
)
from recall_fusion.snapshot import Snapshot, SnapshotStore  # noqa: E402


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_fusion_recomputation(results_path: Path) -> dict:
    rows = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines()]
    checked = 0
    duplicate_doc_violations = 0
    for row in rows:
        doc_ids = [detail["doc_id"] for detail in row["details"]]
        if len(doc_ids) != len(set(doc_ids)):
            duplicate_doc_violations += 1
        for detail in row["details"]:
            recomputed = sum(
                item["weighted_contribution"] for item in detail["per_channel"]
            )
            assert math.isclose(
                detail["final_score"], recomputed, abs_tol=1e-12
            ), f"recompute mismatch for {row['query_id']}/{detail['doc_id']}"
            checked += 1
    return {
        "queries_checked": len(rows),
        "candidates_checked": checked,
        "duplicate_doc_violations": duplicate_doc_violations,
    }


def run_variant(
    docs,
    queries,
    overrides: dict,
    out_dir: Path,
    in_process: bool,
) -> tuple[FusionPipeline, dict]:
    config = FusionConfig.default(overrides)
    pipeline = FusionPipeline(docs, config, in_process=in_process)
    report = pipeline.run(queries)
    pipeline.save_artifacts(out_dir, report)
    return pipeline, report.to_dict()


def main() -> int:
    work = ROOT / "artifacts" / "validation"
    work.mkdir(parents=True, exist_ok=True)
    generate_dataset(ROOT / "data", n_docs=120, n_queries=25, seed=20261124)
    write_default_config(ROOT / "config" / "fusion_config.json")
    docs, queries = load_dataset(ROOT / "data")

    findings: dict = {}

    # 1. Baseline run, in-process vs process pool, byte-level reproducibility.
    _, base_in = run_variant(docs, queries, {}, work / "baseline_inproc", True)
    _, base_pool = run_variant(docs, queries, {}, work / "baseline_pool", False)
    hash_in = file_sha256(work / "baseline_inproc" / "query_results.jsonl")
    hash_pool = file_sha256(work / "baseline_pool" / "query_results.jsonl")
    findings["parallel_reproducibility"] = {
        "in_process_sha256": hash_in,
        "pool_sha256": hash_pool,
        "identical": hash_in == hash_pool,
        "batches_identical": base_in["batches"] == base_pool["batches"],
        "execution_order_identical": (
            base_in["execution_order"] == base_pool["execution_order"]
        ),
    }
    findings["fusion_recomputation"] = check_fusion_recomputation(
        work / "baseline_pool" / "query_results.jsonl"
    )

    # 2. Recall budget pressure.
    _, recall_report = run_variant(
        docs,
        queries,
        {"budgets": {"total_candidates": 120, "fusion_comparisons": 10_000_000}},
        work / "recall_budget",
        True,
    )
    findings["recall_budget"] = {
        "rejected": len(recall_report["rejected_query_ids"]),
        "codes": sorted({r["code"] for r in recall_report["rejections"]}),
        "rejected_excluded_from_completed": set(
            recall_report["rejected_query_ids"]
        ).isdisjoint(recall_report["completed_query_ids"]),
        "arbitrations": len(recall_report["arbitrations"]),
        "remaining": recall_report["budget_remaining"],
    }

    # 3. Fusion budget pressure with two-stage ranking.
    _, fusion_report = run_variant(
        docs,
        queries,
        {
            "scheduler": {
                "two_stage_enabled": True,
                "fine_pool_size": 8,
                "batch_size": 8,
            },
            "budgets": {
                "total_candidates": 10_000_000,
                "fusion_comparisons": 40,
            },
        },
        work / "fusion_budget",
        True,
    )
    findings["fusion_budget"] = {
        "rejected": len(fusion_report["rejected_query_ids"]),
        "codes": sorted({r["code"] for r in fusion_report["rejections"]}),
        "arbitration_triggers": sorted(
            {a["trigger"] for a in fusion_report["arbitrations"]}
        ),
    }

    # 4. Memory eviction: tight cap must evict but preserve fused outputs.
    tight_overrides = {
        "scheduler": {"batch_size": 4},
        "budgets": {
            "total_candidates": 10_000_000,
            "fusion_comparisons": 10_000_000,
            "resident_memory_bytes": 30_000,
        },
    }
    loose_overrides = {
        "scheduler": {"batch_size": 4},
        "budgets": {
            "total_candidates": 10_000_000,
            "fusion_comparisons": 10_000_000,
            "resident_memory_bytes": 50_000_000,
        },
    }
    pipe_tight, tight_report = run_variant(
        docs, queries, tight_overrides, work / "memory_tight", True
    )
    pipe_loose, loose_report = run_variant(
        docs, queries, loose_overrides, work / "memory_loose", True
    )
    same_after_eviction = True
    for query_id in pipe_tight.scheduler.results:
        tight_scores = [
            c.final_score for c in pipe_tight.scheduler.results[query_id].fused
        ]
        loose_scores = [
            c.final_score for c in pipe_loose.scheduler.results[query_id].fused
        ]
        same_after_eviction &= tight_scores == loose_scores
    findings["memory_eviction"] = {
        "eviction_events": len(tight_report["eviction_events"]),
        "sample_event": tight_report["eviction_events"][0]
        if tight_report["eviction_events"]
        else None,
        "results_identical_after_eviction": same_after_eviction,
        "peak_bytes": tight_report["resident_peak_bytes"],
        "limit_bytes": tight_report["memory_limit_bytes"],
    }

    # 5. Snapshot versioning, rollback and corruption.
    store = SnapshotStore()
    store.submit(Snapshot.create("fusion_weights", 2, {"inverted": 1.0}))
    rollback_blocked = False
    try:
        store.submit(Snapshot.create("fusion_weights", 1, {"inverted": 0.1}))
    except PipelineError as exc:
        rollback_blocked = exc.code == ErrorCode.E_SNAPSHOT_ROLLBACK
    corrupt_blocked = False
    snapshot_dir = work / "snapshots"
    Snapshot.create("recall_stats", 1, {"inverted": {"kept_hit_count": 1}}).save(
        snapshot_dir
    )
    target = snapshot_dir / "recall_stats.json"
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw["payload"]["inverted"]["kept_hit_count"] = 999
    target.write_text(json.dumps(raw), encoding="utf-8")
    try:
        Snapshot.load(snapshot_dir, "recall_stats")
    except PipelineError as exc:
        corrupt_blocked = exc.code == ErrorCode.E_SNAPSHOT_CORRUPT
    findings["snapshots"] = {
        "rollback_blocked": rollback_blocked,
        "corruption_detected": corrupt_blocked,
        "baseline_snapshot_kinds": sorted(
            p.name for p in (work / "baseline_pool" / "snapshots").iterdir()
        ),
    }

    out = work / "validation_summary.json"
    out.write_text(json.dumps(findings, indent=2, sort_keys=True, ensure_ascii=False),
                   encoding="utf-8")
    print(json.dumps(findings, indent=2, ensure_ascii=False))

    assertions = [
        findings["parallel_reproducibility"]["identical"],
        findings["fusion_recomputation"]["duplicate_doc_violations"] == 0,
        findings["recall_budget"]["rejected_excluded_from_completed"],
        findings["memory_eviction"]["results_identical_after_eviction"],
        findings["snapshots"]["rollback_blocked"],
        findings["snapshots"]["corruption_detected"],
    ]
    return 0 if all(assertions) else 1


if __name__ == "__main__":
    raise SystemExit(main())
