"""Incremental feature drift monitoring engine."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable

from .alerts import AlertStore
from .cache import CacheResult, CacheStore
from .canonical import canonical_bytes, sha256_obj
from .data import generate_batch
from .detectors import (
    SEVERITY_ORDER,
    evaluate_feature_stats,
    rank_test_results,
    test_key_components,
)
from .errors import E_CACHE_CORRUPT, AppError, config_invalid, resource_limit
from .models import Batch, FeatureSpec, MonitorConfig
from .stats import build_feature_snapshot, stats_key_components


ACTIVE_START_BATCH = 1


@dataclass(frozen=True)
class Product:
    feature: str
    target_batch: int
    stats_key: str
    stats_content: dict[str, Any]
    stats_content_sha256: str
    stats_status: str
    test_key: str
    test_content: dict[str, Any]
    test_content_sha256: str
    test_status: str

    @property
    def status(self) -> str:
        if self.stats_status in {"miss", "forced", "bypass"}:
            return "recomputed"
        if self.test_status in {"miss", "forced", "bypass"}:
            return "recomputed"
        return "reused"

    def identity(self) -> list[Any]:
        return [self.feature, self.target_batch]

    def snapshot_payload(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "target_batch": self.target_batch,
            "stats_key": self.stats_key,
            "stats_content_sha256": self.stats_content_sha256,
            "stats": self.stats_content,
            "test_key": self.test_key,
            "test_content_sha256": self.test_content_sha256,
            "test": self.test_content,
        }


def _feature_profile(config: MonitorConfig) -> dict[str, tuple[Any, ...]]:
    return {
        feature.name: (
            feature.kind,
            feature.stat_version,
            feature.enabled,
            feature.definition_sha256(),
        )
        for feature in config.features
    }


def _window_range(batch: int, window_size: int, reference: bool) -> tuple[int, int]:
    if reference:
        return max(0, batch - window_size), batch - 1
    return max(0, batch - window_size + 1), batch


def _ranges(values: Iterable[int]) -> list[dict[str, Any]]:
    ordered = sorted(set(values))
    if not ordered:
        return []
    result = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        result.append({"start_batch": start, "end_batch": previous})
        start = previous = value
    result.append({"start_batch": start, "end_batch": previous})
    return result


def affected_scope(
    old_config: MonitorConfig | None,
    old_cursor: int | None,
    new_config: MonitorConfig,
    new_cursor: int,
) -> set[tuple[str, int]]:
    features = [feature.name for feature in new_config.features]
    active = {
        (feature, batch)
        for batch in range(ACTIVE_START_BATCH, new_cursor + 1)
        for feature in features
    }
    if old_config is None or old_cursor is None:
        return active

    affected: set[tuple[str, int]] = set()
    old_profiles = _feature_profile(old_config)
    new_profiles = _feature_profile(new_config)
    changed_features = {
        name
        for name in features
        if old_profiles.get(name) != new_profiles.get(name)
    }
    global_data_change = any(
        (
            old_config.seed != new_config.seed,
            old_config.batch_size != new_config.batch_size,
            old_config.n_batches != new_config.n_batches,
        )
    )
    old_events = {
        (event.batch, event.feature): event for event in old_config.drift_events
    }
    new_events = {
        (event.batch, event.feature): event for event in new_config.drift_events
    }
    changed_event_features: dict[str, int] = {}
    for event_key in set(old_events) | set(new_events):
        if old_events.get(event_key) == new_events.get(event_key):
            continue
        batch, feature = event_key
        changed_event_features[feature] = min(
            changed_event_features.get(feature, batch), batch
        )
    test_config_change = any(
        (
            old_config.numeric_test != new_config.numeric_test,
            old_config.numeric_threshold != new_config.numeric_threshold,
            old_config.category_threshold != new_config.category_threshold,
            old_config.quantiles != new_config.quantiles,
        )
    )
    window_change = old_config.window_size != new_config.window_size

    for batch in range(ACTIVE_START_BATCH, new_cursor + 1):
        batch_affected_all = (
            global_data_change
            or test_config_change
            or batch > old_cursor
        )
        if window_change and batch <= old_cursor:
            old_reference = _window_range(batch, old_config.window_size, True)
            new_reference = _window_range(batch, new_config.window_size, True)
            old_monitor = _window_range(batch, old_config.window_size, False)
            new_monitor = _window_range(batch, new_config.window_size, False)
            if old_reference != new_reference or old_monitor != new_monitor:
                batch_affected_all = True
        for feature in features:
            event_invalidates = (
                feature in changed_event_features
                and batch >= changed_event_features[feature]
            )
            if batch_affected_all or feature in changed_features or event_invalidates:
                affected.add((feature, batch))
    return affected


@dataclass
class RunResult:
    config: MonitorConfig
    cursor: int
    products: list[Product]
    affected_products: list[list[Any]]
    recomputed_products: list[list[Any]]
    reused_products: list[list[Any]]
    removed_products: list[list[Any]]
    affected_features: list[str]
    affected_batches: list[int]
    affected_suffixes: list[dict[str, Any]]
    latest_results: list[dict[str, Any]]
    window_report: dict[str, Any]
    alert: dict[str, Any] | None
    degradations: list[str] = field(default_factory=list)
    memory_estimate_bytes: int = 0
    parallelism: dict[str, Any] = field(default_factory=dict)

    def _product_by_identity(self) -> dict[tuple[str, int], Product]:
        return {(product.feature, product.target_batch): product for product in self.products}

    def snapshot_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.config.schema_version,
            "config": self.config.to_state(),
            "cursor": self.cursor,
            "products": [
                product.snapshot_payload()
                for product in sorted(
                    self.products, key=lambda item: (item.target_batch, item.feature)
                )
            ],
        }

    def snapshot_bytes(self) -> bytes:
        return canonical_bytes(self.snapshot_payload())

    def incremental_report(self) -> dict[str, Any]:
        affected = {tuple(item) for item in self.affected_products}
        recomputed = {tuple(item) for item in self.recomputed_products}
        reused = {tuple(item) for item in self.reused_products}
        return {
            "affected_products": [list(item) for item in sorted(affected)],
            "recomputed_products": [list(item) for item in sorted(recomputed)],
            "reused_products": [list(item) for item in sorted(reused)],
            "removed_products": self.removed_products,
            "affected_features": self.affected_features,
            "affected_batches": self.affected_batches,
            "affected_suffixes": self.affected_suffixes,
            "coverage": {
                "intersection_size": len(recomputed & reused),
                "affected_covered": affected.issubset(recomputed | reused),
                "all_active_accounted_for": len(recomputed | reused) == len(self.products),
            },
            "degradations": self.degradations,
        }

    def public(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "config_version": self.config.config_version,
            "window_size": self.config.window_size,
            "window_report": self.window_report,
            "results": self.latest_results,
            "incremental": self.incremental_report(),
            "alert": {
                "alert_id": self.alert["alert_id"],
                "version": self.alert["version"],
                "checksum": self.alert["checksum"],
                "created": self.alert.get("created", False),
            }
            if self.alert
            else None,
            "memory_estimate_bytes": self.memory_estimate_bytes,
            "parallelism": self.parallelism,
            "snapshot_sha256": sha256_obj(self.snapshot_payload()),
        }


class MonitorEngine:
    def __init__(
        self,
        config: MonitorConfig,
        cache: CacheStore,
        alert_store: AlertStore | None = None,
    ) -> None:
        self.config = config
        self.cache = cache
        self.alert_store = alert_store
        self._batches: dict[int, Batch] = {}

    def _batch(self, index: int) -> Batch:
        batch = self._batches.get(index)
        if batch is None:
            batch = generate_batch(self.config, index)
            self._batches[index] = batch
        return batch

    def _batch_map(self, cursor: int) -> dict[int, Batch]:
        return {index: self._batch(index) for index in range(0, cursor + 1)}

    def _check_memory(self, cursor: int, workers: int) -> int:
        features = len(self.config.features)
        peak_samples = (cursor + 2) * self.config.batch_size
        estimate = features * peak_samples * 8 * 8 * max(1, workers)
        if estimate > self.config.limits.max_stats_memory_bytes:
            raise resource_limit(
                "max_stats_memory_bytes",
                "驻留统计量内存估算超过上限",
                estimate_bytes=estimate,
                max_stats_memory_bytes=self.config.limits.max_stats_memory_bytes,
            )
        return estimate

    def _resolve_parallelism(
        self, requested_parallel: int | None
    ) -> tuple[int, list[str], dict[str, Any]]:
        maximum = self.config.limits.max_parallel_features
        feature_count = len(self.config.features)
        requested = maximum if requested_parallel is None else int(requested_parallel)
        requested = max(1, min(requested, feature_count))
        degradations: list[str] = []
        if requested > maximum:
            if self.config.limits.resource_policy == "strict":
                raise resource_limit(
                    "max_parallel_features",
                    "并行重算特征数超过资源上限",
                    requested=requested,
                    max_parallel_features=maximum,
                )
            requested = maximum
            degradations.append("PARALLELISM_CAPPED")
        workers = max(1, min(requested, feature_count))
        return workers, degradations, {
            "requested": requested_parallel if requested_parallel is not None else maximum,
            "workers": workers,
            "max_parallel_features": maximum,
        }

    def _make_product(
        self,
        feature: FeatureSpec,
        target_batch: int,
        batches: dict[int, Batch],
        use_cache: bool,
        degradations: set[str],
    ) -> Product:
        snapshot = build_feature_snapshot(
            self.config, batches, feature, target_batch
        )
        stats_components = stats_key_components(self.config, feature, snapshot)
        stats_key = self.cache.key_for(stats_components)
        if use_cache:
            stats_result: CacheResult = self.cache.get_or_compute(
                stats_components, "feature_stats", lambda: snapshot
            )
            if sha256_obj(stats_result.content) != sha256_obj(snapshot):
                raise AppError(
                    E_CACHE_CORRUPT,
                    "缓存键命中了内容不一致的统计量快照",
                    details={"key": stats_result.key},
                )
            if stats_result.degradation:
                degradations.add(stats_result.degradation)
            stats_content = stats_result.content
            stats_status = stats_result.status
            stats_content_sha = stats_result.content_sha256
        else:
            stats_content = snapshot
            stats_status = "forced"
            stats_content_sha = sha256_obj(snapshot)

        test_content = evaluate_feature_stats(self.config, feature, stats_content)
        test_components = test_key_components(
            self.config,
            feature,
            stats_content,
            stats_key,
            stats_content_sha,
        )
        test_key = self.cache.key_for(test_components)
        if use_cache:
            test_result = self.cache.get_or_compute(
                test_components, "feature_test", lambda: test_content
            )
            if sha256_obj(test_result.content) != sha256_obj(test_content):
                raise AppError(
                    E_CACHE_CORRUPT,
                    "缓存键命中了内容不一致的检验快照",
                    details={"key": test_result.key},
                )
            if test_result.degradation:
                degradations.add(test_result.degradation)
            final_test_content = test_result.content
            test_status = test_result.status
            test_content_sha = test_result.content_sha256
        else:
            final_test_content = test_content
            test_status = "forced"
            test_content_sha = sha256_obj(test_content)

        return Product(
            feature=feature.name,
            target_batch=target_batch,
            stats_key=stats_key,
            stats_content=stats_content,
            stats_content_sha256=stats_content_sha,
            stats_status=stats_status,
            test_key=test_key,
            test_content=final_test_content,
            test_content_sha256=test_content_sha,
            test_status=test_status,
        )

    def _latest_results(self, products: list[Product], cursor: int) -> list[dict[str, Any]]:
        current = [product for product in products if product.target_batch == cursor]
        ranked = rank_test_results(product.test_content for product in current)
        product_map = {product.feature: product for product in current}
        results = []
        for item in ranked:
            product = product_map[item["feature"]]
            record = dict(item)
            record.update(
                {
                    "stats_key": product.stats_key,
                    "test_key": product.test_key,
                    "stats_cache_status": product.stats_status,
                    "test_cache_status": product.test_status,
                    "product_cache_status": product.status,
                }
            )
            results.append(record)
        return results

    def _window_report(
        self, products: list[Product], cursor: int
    ) -> dict[str, Any]:
        if not products:
            return {}
        current = sorted(
            (product for product in products if product.target_batch == cursor),
            key=lambda item: item.feature,
        )
        if not current:
            return {}
        windows = current[0].stats_content["windows"]
        reference = windows["reference"]
        monitor = windows["monitor"]
        full = windows["full"]
        return {
            "target_batch": cursor,
            "reference_start_batch": reference["start_batch"],
            "reference_end_batch": reference["end_batch"],
            "window_start_batch": monitor["start_batch"],
            "window_end_batch": monitor["end_batch"],
            "removed_batch_count": monitor["removed_batch_count"],
            "removed_sample_count": monitor["removed_sample_count"],
            "removed_batch_indices": monitor["removed_batch_indices"],
            "reference_window": reference,
            "monitor_window": monitor,
            "full_history": full,
            "scope": {
                "reference": "当前批之前的 window_size 批",
                "monitor_window": "包含当前批的滑动窗口",
                "full": "从第 0 批到当前批的全量累计",
            },
        }

    def _alert_for(
        self, products: list[Product], cursor: int, window_report: dict[str, Any]
    ) -> dict[str, Any] | None:
        if self.alert_store is None:
            return None
        current = sorted(
            (product for product in products if product.target_batch == cursor),
            key=lambda item: item.feature,
        )
        records = []
        for product in current:
            test = product.test_content
            if not test.get("detected"):
                continue
            records.append(
                {
                    "feature": product.feature,
                    "kind": test["kind"],
                    "target_batch": product.target_batch,
                    "drift": test["drift"],
                    "threshold": test["threshold"],
                    "detected": test["detected"],
                    "severity": test["severity"],
                    "test_method": test["test_method"],
                    "stats_key": product.stats_key,
                    "test_key": product.test_key,
                    "stats_content_sha256": product.stats_content_sha256,
                    "test_content_sha256": product.test_content_sha256,
                    "_severity_order": SEVERITY_ORDER.get(test["severity"], 0),
                }
            )
        alert_window = {
            "target_batch": cursor,
            "reference_window": window_report["reference_window"],
            "monitor_window": window_report["monitor_window"],
            "full_history": window_report["full_history"],
        }
        return self.alert_store.emit(
            self.config.config_version, alert_window, records
        )

    def run(
        self,
        cursor: int,
        previous_config: MonitorConfig | None = None,
        previous_cursor: int | None = None,
        use_cache: bool = True,
        emit_alert: bool = True,
        requested_parallel: int | None = None,
    ) -> RunResult:
        if cursor < ACTIVE_START_BATCH or cursor >= self.config.n_batches:
            raise config_invalid(
                "cursor",
                f"cursor 必须在 [{ACTIVE_START_BATCH}, {self.config.n_batches - 1}] 内",
                cursor=cursor,
            )
        workers, degradations, parallelism = self._resolve_parallelism(
            requested_parallel
        )
        memory_estimate = self._check_memory(cursor, workers)
        batches = self._batch_map(cursor)
        active_targets = list(range(ACTIVE_START_BATCH, cursor + 1))
        products: list[Product] = []
        degradation_set = set(degradations)

        for target_batch in active_targets:
            if workers == 1:
                batch_products = [
                    self._make_product(
                        feature,
                        target_batch,
                        batches,
                        use_cache,
                        degradation_set,
                    )
                    for feature in self.config.features
                ]
            else:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = [
                        executor.submit(
                            self._make_product,
                            feature,
                            target_batch,
                            batches,
                            use_cache,
                            degradation_set,
                        )
                        for feature in self.config.features
                    ]
                    batch_products = [future.result() for future in futures]
            products.extend(
                sorted(batch_products, key=lambda item: item.feature)
            )

        products.sort(key=lambda item: (item.target_batch, item.feature))
        affected = affected_scope(
            previous_config, previous_cursor, self.config, cursor
        )
        active_identities = {tuple(product.identity()) for product in products}
        affected &= active_identities
        recomputed = {
            tuple(product.identity())
            for product in products
            if product.status == "recomputed"
        }
        reused = {
            tuple(product.identity())
            for product in products
            if product.status == "reused"
        }
        removed: set[tuple[str, int]] = set()
        if previous_config is not None and previous_cursor is not None:
            old_active = {
                (feature.name, batch)
                for batch in range(ACTIVE_START_BATCH, previous_cursor + 1)
                for feature in previous_config.features
            }
            removed = old_active - active_identities

        affected_features = sorted({feature for feature, _ in affected})
        affected_batches = sorted({batch for _, batch in affected})
        affected_suffixes = [
            {
                "feature": feature,
                "from_batch": min(
                    batch for item_feature, batch in affected if item_feature == feature
                ),
                "to_batch": cursor,
                "batch_ranges": _ranges(
                    batch for item_feature, batch in affected if item_feature == feature
                ),
            }
            for feature in affected_features
        ]

        window_report = self._window_report(products, cursor)
        latest = self._latest_results(products, cursor)
        alert = self._alert_for(products, cursor, window_report) if emit_alert else None
        return RunResult(
            config=self.config,
            cursor=cursor,
            products=products,
            affected_products=[list(item) for item in sorted(affected)],
            recomputed_products=[list(item) for item in sorted(recomputed)],
            reused_products=[list(item) for item in sorted(reused)],
            removed_products=[list(item) for item in sorted(removed)],
            affected_features=affected_features,
            affected_batches=affected_batches,
            affected_suffixes=affected_suffixes,
            latest_results=latest,
            window_report=window_report,
            alert=alert,
            degradations=sorted(degradation_set),
            memory_estimate_bytes=memory_estimate,
            parallelism=parallelism,
        )


def compare_with_full_rebuild(
    result: RunResult, cache: CacheStore, alert_store: AlertStore | None = None
) -> dict[str, Any]:
    full_engine = MonitorEngine(result.config, cache, alert_store=None)
    full = full_engine.run(
        result.cursor,
        previous_config=None,
        previous_cursor=None,
        use_cache=False,
        emit_alert=False,
    )
    incremental_bytes = result.snapshot_bytes()
    full_bytes = full.snapshot_bytes()
    return {
        "match": incremental_bytes == full_bytes,
        "incremental_sha256": sha256_obj(json_loadable(result.snapshot_payload())),
        "full_sha256": sha256_obj(json_loadable(full.snapshot_payload())),
        "bytes": len(incremental_bytes),
    }


def json_loadable(value: Any) -> Any:
    """Return a canonical plain object without mutating engine state."""
    import json

    return json.loads(canonical_bytes(value).decode("utf-8"))

