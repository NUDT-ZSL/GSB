"""End-to-end pipeline facade: data -> recall/fusion -> report + snapshots."""

from __future__ import annotations

import json
from pathlib import Path

from .config import FusionConfig
from .models import Document, Query
from .scheduler import PipelineScheduler, SchedulingReport
from .snapshot import Snapshot, SnapshotStore


def build_snapshots(
    results: dict,
    config: FusionConfig,
    store: SnapshotStore,
    versions: dict[str, int] | None = None,
) -> SnapshotStore:
    """Build and atomically submit the three versioned snapshot kinds."""
    versions = versions or {"recall_stats": 1, "normalization_params": 1, "fusion_weights": 1}

    recall_stats: dict[str, dict] = {}
    normalization_params: dict[str, dict] = {}
    fusion_weights: dict[str, float] = {}

    for channel in config.enabled_channels():
        hit_count = 0
        raw_scores: list[float] = []
        for result in results.values():
            for candidate in result.fused:
                for hit in candidate.all_hits:
                    if hit.channel == channel:
                        hit_count += 1
                        raw_scores.append(hit.raw_score)
        recall_stats[channel] = {
            "kept_hit_count": hit_count,
            "raw_score_min": min(raw_scores) if raw_scores else 0.0,
            "raw_score_max": max(raw_scores) if raw_scores else 0.0,
        }
        channel_cfg = config.channels[channel]
        normalization_params[channel] = {
            "normalization": channel_cfg.normalization
            or config.scheduler.default_normalization,
            "rank_constant": config.scheduler.rank_constant,
        }
        fusion_weights[channel] = channel_cfg.weight

    for kind, payload in (
        ("recall_stats", recall_stats),
        ("normalization_params", normalization_params),
        ("fusion_weights", fusion_weights),
    ):
        store.submit(Snapshot.create(kind, versions[kind], payload))
    return store


class FusionPipeline:
    def __init__(
        self,
        documents: list[Document],
        config: FusionConfig,
        in_process: bool = False,
    ):
        self.documents = documents
        self.config = config
        self.scheduler = PipelineScheduler(documents, config, in_process=in_process)
        self.snapshot_store = SnapshotStore()

    def run(self, queries: list[Query]) -> SchedulingReport:
        report = self.scheduler.run(queries)
        build_snapshots(self.scheduler.results, self.config, self.snapshot_store)
        return report

    def save_artifacts(self, out_dir: str | Path, report: SchedulingReport) -> dict[str, Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / "scheduling_report.json"
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        results_path = out_dir / "query_results.jsonl"
        with results_path.open("w", encoding="utf-8", newline="\n") as handle:
            for query_id in sorted(self.scheduler.results):
                result = self.scheduler.results[query_id]
                handle.write(
                    json.dumps(
                        {
                            "query_id": result.query_id,
                            "batch_id": result.batch_id,
                            "top_doc_ids": [c.doc_id for c in result.fused],
                            "final_scores": [c.final_score for c in result.fused],
                            "details": [
                                {
                                    "doc_id": c.doc_id,
                                    "kept_channel": c.kept_channel,
                                    "final_score": c.final_score,
                                    "per_channel": [d.__dict__ for d in c.details],
                                    "all_hits": [h.__dict__ for h in c.all_hits],
                                }
                                for c in result.fused
                            ],
                            "per_channel_caps": result.per_channel_caps,
                            "cost_recall": result.cost_recall,
                            "cost_fusion": result.cost_fusion,
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                )
                handle.write("\n")
        snapshot_dir = out_dir / "snapshots"
        snapshot_paths = {}
        for kind, snapshot in self.snapshot_store._current.items():
            snapshot_paths[kind] = snapshot.save(snapshot_dir)
        return {"report": report_path, "results": results_path, **snapshot_paths}
