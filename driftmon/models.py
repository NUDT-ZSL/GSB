"""Core feature, batch, and configuration models."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .canonical import sha256_obj, stable_float
from .errors import config_invalid


NUMERIC = "numeric"
CATEGORY = "category"
FEATURE_KINDS = (NUMERIC, CATEGORY)

MEAN_SHIFT = "mean_shift"
QUANTILE_DISTANCE = "quantile_distance"
TEST_METHODS = (MEAN_SHIFT, QUANTILE_DISTANCE)


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    kind: str
    stat_version: int = 1
    enabled: bool = True
    mean: float = 0.0
    std: float = 1.0
    levels: tuple[str, ...] = ()
    probabilities: tuple[float, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise config_invalid("features.name", "特征名必须非空且不能包含首尾空格")
        if self.kind not in FEATURE_KINDS:
            raise config_invalid("features.kind", f"未知特征类型: {self.kind}")
        if self.stat_version <= 0:
            raise config_invalid("stat_version", "统计口径版本必须为正整数")
        if self.kind == NUMERIC:
            if self.std <= 0:
                raise config_invalid("features.std", "数值列的 std 必须大于 0")
            if self.levels or self.probabilities:
                raise config_invalid("features.levels", "数值列不能定义类别水平")
        else:
            if not self.levels:
                raise config_invalid("features.levels", "类别列必须定义 levels")
            if len(self.levels) != len(self.probabilities):
                raise config_invalid(
                    "features.probabilities", "levels 与 probabilities 长度必须一致"
                )
            if list(self.levels) != sorted(set(self.levels)):
                raise config_invalid("features.levels", "类别水平必须唯一且升序")
            if any(probability <= 0 for probability in self.probabilities):
                raise config_invalid("features.probabilities", "类别概率必须大于 0")
            if abs(sum(self.probabilities) - 1.0) > 1e-9:
                raise config_invalid("features.probabilities", "类别概率之和必须为 1")

    def definition_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "description": self.description,
        }
        if self.kind == NUMERIC:
            payload.update({"mean": stable_float(self.mean), "std": stable_float(self.std)})
        else:
            payload.update(
                {
                    "levels": list(self.levels),
                    "probabilities": [stable_float(value) for value in self.probabilities],
                }
            )
        return payload

    def definition_sha256(self) -> str:
        return sha256_obj(self.definition_payload())

    def to_state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "stat_version": self.stat_version,
            "enabled": self.enabled,
            "mean": self.mean,
            "std": self.std,
            "levels": list(self.levels),
            "probabilities": list(self.probabilities),
            "description": self.description,
        }

    @classmethod
    def from_state(cls, payload: dict[str, Any]) -> "FeatureSpec":
        return cls(
            name=str(payload["name"]),
            kind=str(payload["kind"]),
            stat_version=int(payload.get("stat_version", 1)),
            enabled=bool(payload.get("enabled", True)),
            mean=float(payload.get("mean", 0.0)),
            std=float(payload.get("std", 1.0)),
            levels=tuple(str(item) for item in payload.get("levels", [])),
            probabilities=tuple(float(item) for item in payload.get("probabilities", [])),
            description=str(payload.get("description", "")),
        )

    def with_updates(self, **updates: Any) -> "FeatureSpec":
        return replace(self, **updates)


@dataclass(frozen=True, order=True)
class DriftEvent:
    batch: int
    feature: str
    kind: str
    magnitude: float = 0.0
    probabilities: tuple[float, ...] = ()
    missing_rate: float = 0.0

    def __post_init__(self) -> None:
        if self.batch < 0:
            raise config_invalid("drift_events.batch", "漂移批次不能为负")
        if not self.feature:
            raise config_invalid("drift_events.feature", "漂移特征不能为空")
        if self.kind not in {
            "mean_shift",
            "variance_scale",
            "category_shift",
            "missing_rate",
        }:
            raise config_invalid("drift_events.kind", f"未知漂移类型: {self.kind}")
        if self.kind == "variance_scale" and self.magnitude <= 0:
            raise config_invalid("drift_events.magnitude", "方差放大系数必须大于 0")
        if self.kind == "missing_rate" and not (0 < self.missing_rate < 1):
            raise config_invalid("drift_events.missing_rate", "缺失率必须在 (0, 1) 内")
        if self.kind == "category_shift":
            if not self.probabilities:
                raise config_invalid("drift_events.probabilities", "类别漂移必须提供占比")
            if any(probability < 0 for probability in self.probabilities):
                raise config_invalid("drift_events.probabilities", "类别占比不能为负")
            if abs(sum(self.probabilities) - 1.0) > 1e-9:
                raise config_invalid("drift_events.probabilities", "类别占比之和必须为 1")

    def to_state(self) -> dict[str, Any]:
        return {
            "batch": self.batch,
            "feature": self.feature,
            "kind": self.kind,
            "magnitude": self.magnitude,
            "probabilities": list(self.probabilities),
            "missing_rate": self.missing_rate,
        }

    @classmethod
    def from_state(cls, payload: dict[str, Any]) -> "DriftEvent":
        return cls(
            batch=int(payload["batch"]),
            feature=str(payload["feature"]),
            kind=str(payload["kind"]),
            magnitude=float(payload.get("magnitude", 0.0)),
            probabilities=tuple(float(item) for item in payload.get("probabilities", [])),
            missing_rate=float(payload.get("missing_rate", 0.0)),
        )


@dataclass(frozen=True)
class ResourceLimits:
    max_batch_samples: int = 10_000
    max_cache_entries: int = 10_000
    max_stats_memory_bytes: int = 64 * 1024 * 1024
    max_parallel_features: int = 4
    resource_policy: str = "strict"

    def __post_init__(self) -> None:
        for field_name in (
            "max_batch_samples",
            "max_cache_entries",
            "max_stats_memory_bytes",
            "max_parallel_features",
        ):
            if int(getattr(self, field_name)) <= 0:
                raise config_invalid(field_name, f"{field_name} 必须为正整数")
        if self.resource_policy not in {"strict", "degrade"}:
            raise config_invalid("resource_policy", "resource_policy 只能是 strict 或 degrade")

    def to_state(self) -> dict[str, Any]:
        return {
            "max_batch_samples": self.max_batch_samples,
            "max_cache_entries": self.max_cache_entries,
            "max_stats_memory_bytes": self.max_stats_memory_bytes,
            "max_parallel_features": self.max_parallel_features,
            "resource_policy": self.resource_policy,
        }

    @classmethod
    def from_state(cls, payload: dict[str, Any]) -> "ResourceLimits":
        return cls(
            max_batch_samples=int(payload.get("max_batch_samples", 10_000)),
            max_cache_entries=int(payload.get("max_cache_entries", 10_000)),
            max_stats_memory_bytes=int(
                payload.get("max_stats_memory_bytes", 64 * 1024 * 1024)
            ),
            max_parallel_features=int(payload.get("max_parallel_features", 4)),
            resource_policy=str(payload.get("resource_policy", "strict")),
        )


@dataclass(frozen=True)
class MonitorConfig:
    seed: int = 20261121
    n_batches: int = 8
    batch_size: int = 64
    window_size: int = 3
    slide_step: int = 1
    numeric_test: str = MEAN_SHIFT
    numeric_threshold: float = 0.8
    category_threshold: float = 0.20
    quantiles: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)
    config_version: int = 1
    features: tuple[FeatureSpec, ...] = field(default_factory=tuple)
    drift_events: tuple[DriftEvent, ...] = field(default_factory=tuple)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    schema_version: str = "driftmon.v1"

    def __post_init__(self) -> None:
        if self.n_batches <= 0:
            raise config_invalid("n_batches", "批次总数必须为正整数")
        if self.batch_size <= 0:
            raise config_invalid("batch_size", "单批样本数必须为正整数")
        if self.window_size <= 0:
            raise config_invalid("window_size", "参考窗口长度必须为正整数")
        if self.slide_step <= 0 or self.slide_step > self.window_size:
            raise config_invalid(
                "slide_step",
                "滑动步长必须在 [1, window_size] 内",
                window_size=self.window_size,
            )
        if self.slide_step > self.n_batches:
            raise config_invalid("slide_step", "滑动步长不能超过总批次数")
        if self.numeric_test not in TEST_METHODS:
            raise config_invalid("numeric_test", f"未知数值检验方式: {self.numeric_test}")
        if self.numeric_threshold <= 0:
            raise config_invalid("numeric_threshold", "数值阈值必须大于 0")
        if self.category_threshold <= 0:
            raise config_invalid("category_threshold", "类别阈值必须大于 0")
        if not self.quantiles:
            raise config_invalid("quantiles", "分位数列表不能为空")
        if tuple(sorted(set(self.quantiles))) != self.quantiles:
            raise config_invalid("quantiles", "分位数必须唯一且升序")
        if any(value <= 0 or value >= 1 for value in self.quantiles):
            raise config_invalid("quantiles", "分位数必须在 (0, 1) 内")
        if not self.features:
            raise config_invalid("features", "至少需要一个特征")
        names = [feature.name for feature in self.features]
        if names != sorted(names):
            raise config_invalid("features", "特征定义必须按特征名升序")
        if len(names) != len(set(names)):
            raise config_invalid("features", "特征名不能重复")
        for event in self.drift_events:
            if event.batch >= self.n_batches:
                raise config_invalid("drift_events.batch", "漂移批次超出范围")
            if event.feature not in names:
                raise config_invalid("drift_events.feature", f"未知漂移特征: {event.feature}")
            feature = next(item for item in self.features if item.name == event.feature)
            if event.kind in {"mean_shift", "variance_scale"} and feature.kind != NUMERIC:
                raise config_invalid(
                    "drift_events.kind",
                    "均值/方差漂移只能用于数值列",
                    feature=event.feature,
                )
            if event.kind == "category_shift":
                if feature.kind != CATEGORY:
                    raise config_invalid(
                        "drift_events.kind",
                        "类别占比漂移只能用于类别列",
                        feature=event.feature,
                    )
                if len(event.probabilities) != len(feature.levels):
                    raise config_invalid(
                        "drift_events.probabilities",
                        "类别漂移占比维度必须与特征 levels 一致",
                        feature=event.feature,
                    )
        if self.batch_size > self.limits.max_batch_samples:
            from .errors import resource_limit

            raise resource_limit(
                "max_batch_samples",
                "单批样本数超过资源上限",
                batch_size=self.batch_size,
                max_batch_samples=self.limits.max_batch_samples,
            )

    def feature(self, name: str) -> FeatureSpec:
        for feature in self.features:
            if feature.name == name:
                return feature
        raise config_invalid("features.name", f"未知特征: {name}")

    def feature_index(self) -> dict[str, FeatureSpec]:
        return {feature.name: feature for feature in self.features}

    def to_state(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "n_batches": self.n_batches,
            "batch_size": self.batch_size,
            "window_size": self.window_size,
            "slide_step": self.slide_step,
            "numeric_test": self.numeric_test,
            "numeric_threshold": self.numeric_threshold,
            "category_threshold": self.category_threshold,
            "quantiles": list(self.quantiles),
            "config_version": self.config_version,
            "features": [feature.to_state() for feature in self.features],
            "drift_events": [event.to_state() for event in self.drift_events],
            "limits": self.limits.to_state(),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_state(cls, payload: dict[str, Any]) -> "MonitorConfig":
        return cls(
            seed=int(payload["seed"]),
            n_batches=int(payload["n_batches"]),
            batch_size=int(payload["batch_size"]),
            window_size=int(payload["window_size"]),
            slide_step=int(payload["slide_step"]),
            numeric_test=str(payload["numeric_test"]),
            numeric_threshold=float(payload["numeric_threshold"]),
            category_threshold=float(payload["category_threshold"]),
            quantiles=tuple(float(item) for item in payload["quantiles"]),
            config_version=int(payload["config_version"]),
            features=tuple(
                FeatureSpec.from_state(item) for item in payload.get("features", [])
            ),
            drift_events=tuple(
                DriftEvent.from_state(item) for item in payload.get("drift_events", [])
            ),
            limits=ResourceLimits.from_state(payload.get("limits", {})),
            schema_version=str(payload.get("schema_version", "driftmon.v1")),
        )

    def updated(self, bump_version: bool = True, **updates: Any) -> "MonitorConfig":
        if bump_version:
            updates.setdefault("config_version", self.config_version + 1)
        return replace(self, **updates)


@dataclass(frozen=True)
class FeatureColumn:
    name: str
    kind: str
    definition_sha256: str
    values: tuple[float | str | None, ...]
    missing_count: int
    content_sha256: str


@dataclass(frozen=True)
class Batch:
    index: int
    seed: int
    row_count: int
    columns: tuple[FeatureColumn, ...]
    fingerprint: str
    audit: dict[str, Any]

    def column(self, name: str) -> FeatureColumn:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)

