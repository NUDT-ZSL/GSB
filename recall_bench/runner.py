"""Top-level orchestration, concurrent scale runs and report building."""

from __future__ import annotations

import os
from typing import Any

from . import cache as cache_mod
from .concurrency import run_stable
from .config import config_hash
from .engine import ScalePipeline, ScaleRunResult
from .snapshot import ShutdownController, SnapshotMismatch, SnapshotStore, STAGES
from .util import atomic_write_json, stable_hash


class BenchmarkRunner:
    def __init__(
        self,
        sample_dir: str,
        results_dir: str,
        config: dict[str, Any],
        scales: list[dict[str, Any]],
        manifest: dict[str, Any],
    ):
        self.sample_dir = sample_dir
        self.results_dir = results_dir
        self.config = config
        self.config_hash_value = config_hash(config)
        self.scales = scales
        self.manifest = manifest
        self.cache_dir = os.path.join(results_dir, "cache")
        self.snapshot_dir = os.path.join(results_dir, "snapshots")
        self.report_dir = os.path.join(results_dir, "reports")
        for directory in (self.cache_dir, self.snapshot_dir, self.report_dir):
            os.makedirs(directory, exist_ok=True)
        self.cache = cache_mod.ArtifactCache(self.cache_dir)
        self.shutdown = ShutdownController()

    def run(self, scale_filter: list[str] | None = None) -> dict[str, Any]:
        selected = [
            scale for scale in self.scales
            if not scale_filter or scale["scale_id"] in scale_filter
        ]
        max_workers = int(self.config["runtime"]["max_workers"])
        tasks = [
            (f"scale::{scale['scale_id']}", lambda scale=scale: self._run_scale(scale))
            for scale in selected
        ]
        outputs = run_stable(tasks, max_workers)
        results = [payload for _key, payload in outputs]
        results.sort(key=lambda result: result["scale_id"])
        report = self._build_report(results)
        finalize_report_fingerprint(report)
        report_path = os.path.join(self.report_dir, "comparison_report.json")
        atomic_write_json(report_path, report)
        return report

    def _run_scale(self, scale: dict[str, Any]) -> dict[str, Any]:
        store = SnapshotStore(self.snapshot_dir, self.config_hash_value, scale["scale_id"])
        self._validate_snapshots(store)
        pipeline = ScalePipeline(
            scale, self.config, self.cache, store, self.shutdown
        )
        result: ScaleRunResult = pipeline.run()
        return self._serialize_result(result)

    def _validate_snapshots(self, store: SnapshotStore) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        for stage in STAGES:
            try:
                store.load(stage)
            except SnapshotMismatch as exc:
                mismatches.append({
                    "stage": stage,
                    "scale_id": store.scale_id,
                    "code": exc.code,
                    "message": str(exc),
                })
                # Refuse to restore: remove the stale snapshot and rebuild.
                path = store.path(stage)
                if os.path.exists(path):
                    os.unlink(path)
        return mismatches

    def _serialize_result(self, result: ScaleRunResult) -> dict[str, Any]:
        parsed = result.parsed
        failures = [failure.to_dict() for failure in parsed.failures]
        failure_codes: dict[str, int] = {}
        for failure in failures:
            failure_codes[failure["code"]] = failure_codes.get(failure["code"], 0) + 1
        return {
            "scale_id": result.scale_id,
            "cache_keys": result.cache_keys,
            "ledger": result.ledger.to_dict(),
            "decision": result.decision,
            "statistics": {
                "users": parsed.user_count,
                "items": parsed.item_count,
                "interactions": parsed.interaction_count,
                "time_span": parsed.time_span,
                "user_interaction_counts": parsed.user_interaction_counts,
                "item_interaction_counts": parsed.item_interaction_counts,
            },
            "channel_stats": result.channel_stats,
            "recommendations": result.fused,
            "evaluation": result.evaluation,
            "failures": {
                "total": len(failures),
                "code_counts": dict(sorted(failure_codes.items())),
                "entries": failures,
            },
            "user_errors": [error.to_dict() for error in result.user_errors],
            "shutdown_requested": result.shutdown_requested,
        }

    def _build_report(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        merged_reused: list[dict[str, str]] = []
        merged_recomputed: list[dict[str, str]] = []
        metric_rows: list[dict[str, Any]] = []
        for result in results:
            merged_reused.extend(result["ledger"]["reused"])
            merged_recomputed.extend(result["ledger"]["recomputed"])
            metric_rows.append({
                "scale_id": result["scale_id"],
                "metrics": result["evaluation"]["metrics"],
                "selected_strategy": result["decision"]["selected_strategy_id"],
                "failure_codes": result["failures"]["code_counts"],
            })
        return {
            "report_version": 1,
            "config_hash": self.config_hash_value,
            "config": self.config,
            "scale_metrics": metric_rows,
            "scale_results": results,
            "incremental": {
                "reused": merged_reused,
                "recomputed": merged_recomputed,
                "reused_count": len(merged_reused),
                "recomputed_count": len(merged_recomputed),
            },
            "input_manifest": self.manifest,
            "report_fingerprint": "",
        }


def finalize_report_fingerprint(report: dict[str, Any]) -> str:
    report["report_fingerprint"] = stable_hash({k: v for k, v in report.items() if k != "report_fingerprint"})
    return report["report_fingerprint"]
