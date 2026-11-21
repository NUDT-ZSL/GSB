"""Orchestration engine: ingestion, windowing, tests, caching and invalidation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .cache import CacheKeyComponents, ContentStore, build_cache_key
from .config_model import MonitorConfig
from .data import Batch, validate_batch
from .drift import FeatureResult, evaluate_feature, sort_results
from .errors import DriftError, ErrorCode
from .hashing import content_hash
from .resources import ResourceGuard
from .snapshot import AlertStore, SnapshotStore
from .statistics import (
    STAT_SCHEMA_VERSION,
    BatchStat,
    compute_batch_stat,
    cumulative_stats,
)
from .windowing import ReferenceWindow


@dataclass
class StepReport:
    batch_id: int
    window_start: int
    window_end: int
    window_batch_ids: List[int]
    removed_batches: List[int]
    removal_amount: Dict[str, Any]
    results: List[FeatureResult]
    reused: List[str]
    recomputed: List[str]
    alerts: List[Dict[str, Any]]
    cache_keys: Dict[str, str]
    window_sample_hashes: Dict[str, str]
    degradation: List[Dict[str, Any]]
    cumulative_summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "window_range": [self.window_start, self.window_end],
            "window_batch_ids": self.window_batch_ids,
            "removed_batches": self.removed_batches,
            "removal_amount": self.removal_amount,
            "feature_results": [item.to_dict() for item in sort_results(self.results)],
            "reused_features": self.reused,
            "recomputed_features": self.recomputed,
            "alerts": self.alerts,
            "cache_keys": dict(sorted(self.cache_keys.items())),
            "window_sample_hashes": dict(sorted(self.window_sample_hashes.items())),
            "degradation": self.degradation,
            "cumulative_summary": self.cumulative_summary,
        }


class MonitoringEngine:
    def __init__(
        self,
        config: MonitorConfig,
        cache: ContentStore,
        snapshots: SnapshotStore,
        alerts: AlertStore,
        guard: ResourceGuard,
    ):
        config.validate()
        self.config = config
        self.cache = cache
        self.snapshots = snapshots
        self.alerts = alerts
        self.guard = guard
        self.snapshot = snapshots.replace(config.to_dict())
        self.windows: Dict[str, ReferenceWindow] = {
            name: ReferenceWindow(length=spec.window, step=1)
            for name, spec in config.features.items()
        }
        self.batch_stats: Dict[int, BatchStat] = {}
        self.batches: Dict[int, Batch] = {}
        self.last_report: Optional[StepReport] = None
        self.last_error: Optional[Dict[str, Any]] = None

    def ingest(self, batch: Batch) -> BatchStat:
        self.guard.check_batch_size(batch.sample_count, batch.batch_id)
        validate_batch(
            batch,
            {name: spec.column_type for name, spec in self.config.features.items()},
        )
        stat = compute_batch_stat(batch, self.config.features)
        self.batch_stats[batch.batch_id] = stat
        self.batches[batch.batch_id] = batch
        degraded, estimate = self.guard.check_resident(list(self.batch_stats.values()))
        self.resident_estimate = estimate
        return stat

    def _window_sample_hash(self, feature: str, window_ids: Sequence[int]) -> str:
        payload = {
            "feature": feature,
            "batches": [
                {
                    "batch_id": bid,
                    "fingerprint": self.batch_stats[bid].batch_fingerprint,
                    "column_slice": self.batches[bid].columns[feature],
                }
                for bid in window_ids
            ],
        }
        return content_hash(payload)

    def _upstream_hash(self, feature: str, window_ids: Sequence[int]) -> str:
        records = [self.batch_stats[bid].to_cache_dict() for bid in window_ids]
        return content_hash({"feature": feature, "upstream_records": records})

    def _components(
        self,
        feature: str,
        spec: Any,
        window_ids: Sequence[int],
        upstream_hash: str,
    ) -> Tuple[CacheKeyComponents, str]:
        sample_hash = self._window_sample_hash(feature, window_ids)
        components = CacheKeyComponents(
            window_sample_hash=sample_hash,
            methods=list(spec.methods),
            thresholds={feature: spec.threshold},
            stat_schema_version=STAT_SCHEMA_VERSION,
            feature_definition_digest=self.config.definition_digest(),
            upstream_hash=upstream_hash,
        )
        return components, build_cache_key(components)

    def evaluate_batch(self, batch: Batch) -> StepReport:
        try:
            report = self._evaluate(batch)
            self.last_report = report
            return report
        except DriftError as exc:
            self.last_error = exc.to_dict()
            raise

    def _evaluate(self, batch: Batch) -> StepReport:
        current = self.ingest(batch)
        results: List[FeatureResult] = []
        reused: List[str] = []
        recomputed: List[str] = []
        cache_keys: Dict[str, str] = {}
        sample_hashes: Dict[str, str] = {}
        published_alerts: List[Dict[str, Any]] = []
        removal_amount: Dict[str, Any] = {}
        removed_batches: List[int] = []

        for feature in self.config.feature_names():
            spec = self.config.features[feature]
            if not spec.enabled:
                continue
            window = self.windows[feature]
            state = window.append_and_slide(current, self.config.features)
            window_ids = list(state.batch_ids)
            removed_batches = state.last_removed
            removal_amount = state.last_removal_amount
            aggregate = window.current_aggregate()
            upstream_hash = self._upstream_hash(feature, window_ids)
            components, key = self._components(feature, spec, window_ids, upstream_hash)
            cache_keys[feature] = key
            sample_hashes[feature] = components.window_sample_hash

            cached = self.cache.get(key)
            if cached is not None:
                reused.append(feature)
                result = FeatureResult(**cached["payload"]["feature_result"])
            else:
                recomputed.append(feature)
                if spec.column_type == "numeric":
                    window_numeric = aggregate["numeric"][feature]
                    window_categorical = None
                else:
                    window_numeric = None
                    window_categorical = aggregate["categorical"][feature]
                result = evaluate_feature(
                    spec,
                    window_numeric,
                    window_categorical,
                    current,
                    state.start_batch,
                    state.end_batch,
                )
                payload = {
                    "feature_result": result.to_dict(),
                    "window_ids": window_ids,
                    "upstream_hash": upstream_hash,
                }
                self.cache.put(key, components, payload)
            results.append(result)

            if result.triggered:
                record = self.alerts.publish(
                    feature_result=result.to_dict(),
                    batch_id=batch.batch_id,
                    cache_key=key,
                    snapshot=self.snapshot,
                )
                published_alerts.append(record.to_dict())

        overlap = sorted(set(reused).intersection(recomputed))
        if overlap:
            raise DriftError(
                ErrorCode.INCONSISTENT,
                "a feature cannot be both reused and recomputed",
                {"category": "reuse_recompute_overlap", "features": overlap},
            )

        cumulative = cumulative_stats(list(self.batch_stats.values()), self.config.features)
        summary = self._summarize_cumulative(cumulative)
        primary = self.windows[self.config.feature_names()[0]]

        return StepReport(
            batch_id=batch.batch_id,
            window_start=primary.state.start_batch,
            window_end=primary.state.end_batch,
            window_batch_ids=primary.state.batch_ids,
            removed_batches=removed_batches,
            removal_amount=removal_amount,
            results=sort_results(results),
            reused=sorted(reused),
            recomputed=sorted(recomputed),
            alerts=published_alerts,
            cache_keys=cache_keys,
            window_sample_hashes=sample_hashes,
            degradation=list(self.guard.degradation_marks),
            cumulative_summary=summary,
        )

    def _summarize_cumulative(self, cumulative: Dict[str, Any]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"numeric": {}, "categorical": {}}
        for feature, stat in cumulative["numeric"].items():
            spec = self.config.features[feature]
            summary["numeric"][feature] = {
                "count": stat.count,
                "mean": stat.mean,
                "variance": stat.variance,
                "quantiles": {
                    str(q): value
                    for q, value in zip(spec.quantiles, stat.quantiles(spec.quantiles))
                },
            }
        for feature, stat in cumulative["categorical"].items():
            summary["categorical"][feature] = {
                "total": stat.total,
                "proportions": stat.proportions(),
            }
        return summary

    def change_config(
        self,
        new_config: MonitorConfig,
        affected_features: Optional[Set[str]] = None,
        affected_batches: Optional[Set[int]] = None,
    ) -> Dict[str, Any]:
        new_config.validate()
        old_config = self.config
        computed_features, computed_batches = self._impact_analysis(old_config, new_config)
        if affected_features is not None:
            computed_features = computed_features | set(affected_features)
        if affected_batches is not None:
            computed_batches = computed_batches | set(affected_batches)

        all_features = set(new_config.feature_names())
        reused_features = sorted(all_features - computed_features)
        recompute_features = sorted(all_features & computed_features)
        overlap = sorted(set(reused_features).intersection(recompute_features))
        if overlap:
            raise DriftError(
                ErrorCode.INCONSISTENT,
                "reuse and recompute feature sets intersect",
                {"category": "feature_sets", "features": overlap},
            )

        new_snapshot = self.snapshots.replace(new_config.to_dict())
        self.config = new_config
        self.snapshot = new_snapshot
        for feature in recompute_features:
            spec = new_config.features[feature]
            self.windows[feature] = ReferenceWindow(length=spec.window, step=1)
        for feature in reused_features:
            if feature not in self.windows:
                spec = new_config.features[feature]
                self.windows[feature] = ReferenceWindow(length=spec.window, step=1)

        all_batches = set(self.batch_stats)
        reused_batches = sorted(all_batches - computed_batches)
        recompute_batches = sorted(all_batches & computed_batches)
        return {
            "recompute_features": recompute_features,
            "reused_features": reused_features,
            "recompute_batches": recompute_batches,
            "reused_batches": reused_batches,
            "new_snapshot_version": new_snapshot["snapshot_version"],
        }

    def _impact_analysis(
        self, old: MonitorConfig, new: MonitorConfig
    ) -> Tuple[Set[str], Set[int]]:
        affected_features: Set[str] = set()
        for name in new.feature_names():
            if name not in old.features:
                affected_features.add(name)
                continue
            before = old.features[name]
            after = new.features[name]
            if (
                before.column_type != after.column_type
                or before.methods != after.methods
                or before.quantiles != after.quantiles
                or before.threshold != after.threshold
                or before.window != after.window
                or before.enabled != after.enabled
            ):
                affected_features.add(name)
        for name in old.feature_names():
            if name not in new.features or (
                name in new.features and not new.features[name].enabled
            ):
                affected_features.add(name)
        affected_batches: Set[int] = set(self.batch_stats) if affected_features else set()
        return affected_features, affected_batches

    def rebuild_from_scratch(self, stream: Sequence[Batch]) -> List[StepReport]:
        fresh_cache = ContentStore(self.cache.directory + "_verify")
        fresh_snapshots = SnapshotStore(self.snapshots.directory + "_verify")
        fresh_alerts = AlertStore(self.alerts.directory + "_verify")
        fresh_engine = MonitoringEngine(
            self.config,
            fresh_cache,
            fresh_snapshots,
            fresh_alerts,
            self.guard,
        )
        reports = []
        for batch in sorted(stream, key=lambda item: item.batch_id):
            reports.append(fresh_engine.evaluate_batch(batch))
        return reports
