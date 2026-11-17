from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import ConfigError
from .util import read_json, sha256_json


ROLES = ("header_footer", "image", "title", "body")
READING_RULES = ("column_first", "region_first")


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be an object", details={"path": path})
    return value


def _number(value: Any, path: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be numeric", details={"path": path, "value": value})
    if integer:
        if int(value) != value:
            raise ConfigError(f"{path} must be an integer", details={"path": path, "value": value})
        return int(value)
    return value


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{path} must be boolean", details={"path": path, "value": value})
    return value


def _pair(value: Any, path: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f"{path} must be a two-element array", details={"path": path})
    low = float(_number(value[0], f"{path}[0]"))
    high = float(_number(value[1], f"{path}[1]"))
    if low <= 0 or high <= 0 or low > high:
        raise ConfigError(
            f"{path} must satisfy 0 < low <= high",
            details={"path": path, "value": [low, high]},
        )
    return low, high


@dataclass(frozen=True)
class AnalysisConfig:
    binary_threshold: int
    min_block_width: int
    min_block_height: int
    projection_analysis_window: int
    title_height_ratio_range: tuple[float, float]
    body_height_ratio_range: tuple[float, float]
    image_ink_density_threshold: float
    text_ink_density_range: tuple[float, float]
    projection_periodicity_threshold: float
    role_priority: tuple[str, ...]
    header_band_ratio: float
    footer_band_ratio: float


@dataclass(frozen=True)
class ColumnConfig:
    enabled: bool
    gap_threshold: int
    overlap_threshold: float
    spanning_width_ratio: float


@dataclass(frozen=True)
class ReadingConfig:
    rule: str
    region_band_tolerance: int


@dataclass(frozen=True)
class RuntimeConfig:
    thread_pool_workers: int
    batch_page_budget: int
    interaction_latency_budget_ms: int
    snapshot_commit_interval: int


@dataclass(frozen=True)
class AdaptiveConfig:
    enabled: bool
    balanced_pressure: float
    fast_pressure: float


@dataclass(frozen=True)
class LayoutConfig:
    schema_version: int
    compatibility_tag: str
    analysis: AnalysisConfig
    columns: ColumnConfig
    reading: ReadingConfig
    runtime: RuntimeConfig
    adaptive: AdaptiveConfig
    raw: dict[str, Any]

    @property
    def full_hash(self) -> str:
        return sha256_json(self.raw)

    @classmethod
    def from_path(cls, path: str | Path) -> "LayoutConfig":
        try:
            payload = read_json(path)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read config: {exc}", details={"path": str(path)}) from exc
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: Any) -> "LayoutConfig":
        root = _mapping(payload, "$")
        schema_version = int(_number(root.get("schema_version"), "$.schema_version", integer=True))
        if schema_version != 1:
            raise ConfigError(
                "unsupported schema_version",
                details={"schema_version": schema_version, "supported": [1]},
            )
        compatibility_tag = root.get("compatibility_tag", "uid103-layout-v1")
        if not isinstance(compatibility_tag, str) or not compatibility_tag:
            raise ConfigError("$.compatibility_tag must be a non-empty string")

        analysis_raw = _mapping(root.get("analysis"), "$.analysis")
        threshold = int(
            _number(analysis_raw.get("binary_threshold"), "$.analysis.binary_threshold", integer=True)
        )
        if not 1 <= threshold <= 254:
            raise ConfigError(
                "binary_threshold must be in [1, 254]",
                details={"path": "$.analysis.binary_threshold", "value": threshold},
            )
        min_width = int(
            _number(analysis_raw.get("min_block_width"), "$.analysis.min_block_width", integer=True)
        )
        min_height = int(
            _number(analysis_raw.get("min_block_height"), "$.analysis.min_block_height", integer=True)
        )
        if min_width < 1 or min_height < 1:
            raise ConfigError("minimum block dimensions must be positive")
        window = int(
            _number(
                analysis_raw.get("projection_analysis_window"),
                "$.analysis.projection_analysis_window",
                integer=True,
            )
        )
        if window < 1:
            raise ConfigError("projection_analysis_window must be >= 1")

        title_range = _pair(
            analysis_raw.get("title_height_ratio_range"),
            "$.analysis.title_height_ratio_range",
        )
        body_range = _pair(
            analysis_raw.get("body_height_ratio_range"),
            "$.analysis.body_height_ratio_range",
        )
        image_threshold = float(
            _number(
                analysis_raw.get("image_ink_density_threshold"),
                "$.analysis.image_ink_density_threshold",
            )
        )
        if not 0 < image_threshold < 1:
            raise ConfigError("image_ink_density_threshold must be in (0, 1)")
        text_density_range = _pair(
            analysis_raw.get("text_ink_density_range"),
            "$.analysis.text_ink_density_range",
        )
        if text_density_range[1] > 1:
            raise ConfigError("text_ink_density_range upper bound must be <= 1")
        periodicity = float(
            _number(
                analysis_raw.get("projection_periodicity_threshold"),
                "$.analysis.projection_periodicity_threshold",
            )
        )
        if not 0 <= periodicity <= 1:
            raise ConfigError("projection_periodicity_threshold must be in [0, 1]")

        priority_raw = analysis_raw.get("role_priority")
        if not isinstance(priority_raw, list) or not all(isinstance(x, str) for x in priority_raw):
            raise ConfigError("$.analysis.role_priority must be an array of role names")
        priority = tuple(priority_raw)
        if len(priority) != len(ROLES) or set(priority) != set(ROLES):
            raise ConfigError(
                "role_priority must contain each role exactly once",
                details={"roles": list(ROLES), "value": priority_raw},
            )
        header_band = float(
            _number(analysis_raw.get("header_band_ratio"), "$.analysis.header_band_ratio")
        )
        footer_band = float(
            _number(analysis_raw.get("footer_band_ratio"), "$.analysis.footer_band_ratio")
        )
        if not 0 <= header_band < 0.5 or not 0 <= footer_band < 0.5:
            raise ConfigError("header/footer band ratios must be in [0, 0.5)")

        columns_raw = _mapping(root.get("columns"), "$.columns")
        columns_enabled = _bool(columns_raw.get("enabled"), "$.columns.enabled")
        gap = int(
            _number(columns_raw.get("gap_threshold"), "$.columns.gap_threshold", integer=True)
        )
        overlap = float(
            _number(columns_raw.get("overlap_threshold"), "$.columns.overlap_threshold")
        )
        spanning = float(
            _number(columns_raw.get("spanning_width_ratio"), "$.columns.spanning_width_ratio")
        )
        if gap < 0:
            raise ConfigError("columns.gap_threshold must be >= 0")
        if not 0 <= overlap <= 1 or not 0 < spanning <= 1:
            raise ConfigError("column overlap/spanning ratios are out of range")

        reading_raw = _mapping(root.get("reading"), "$.reading")
        rule = reading_raw.get("rule")
        if rule not in READING_RULES:
            raise ConfigError(
                "reading.rule must be column_first or region_first",
                details={"value": rule},
            )
        tolerance = int(
            _number(
                reading_raw.get("region_band_tolerance"),
                "$.reading.region_band_tolerance",
                integer=True,
            )
        )
        if tolerance < 0:
            raise ConfigError("reading.region_band_tolerance must be >= 0")

        runtime_raw = _mapping(root.get("runtime"), "$.runtime")
        workers = int(
            _number(
                runtime_raw.get("thread_pool_workers"),
                "$.runtime.thread_pool_workers",
                integer=True,
            )
        )
        page_budget = int(
            _number(runtime_raw.get("batch_page_budget"), "$.runtime.batch_page_budget", integer=True)
        )
        latency_budget = int(
            _number(
                runtime_raw.get("interaction_latency_budget_ms"),
                "$.runtime.interaction_latency_budget_ms",
                integer=True,
            )
        )
        commit_interval = int(
            _number(
                runtime_raw.get("snapshot_commit_interval"),
                "$.runtime.snapshot_commit_interval",
                integer=True,
            )
        )
        if not 1 <= workers <= 256:
            raise ConfigError("runtime.thread_pool_workers must be in [1, 256]")
        if page_budget < 1 or latency_budget < 1 or commit_interval < 1:
            raise ConfigError("runtime budgets and commit interval must be positive")

        adaptive_raw = _mapping(root.get("adaptive"), "$.adaptive")
        adaptive_enabled = _bool(adaptive_raw.get("enabled"), "$.adaptive.enabled")
        balanced_pressure = float(
            _number(adaptive_raw.get("balanced_pressure"), "$.adaptive.balanced_pressure")
        )
        fast_pressure = float(
            _number(adaptive_raw.get("fast_pressure"), "$.adaptive.fast_pressure")
        )
        if not 0 < balanced_pressure < fast_pressure:
            raise ConfigError("adaptive pressure thresholds must satisfy 0 < balanced < fast")

        return cls(
            schema_version=schema_version,
            compatibility_tag=compatibility_tag,
            analysis=AnalysisConfig(
                binary_threshold=threshold,
                min_block_width=min_width,
                min_block_height=min_height,
                projection_analysis_window=window,
                title_height_ratio_range=title_range,
                body_height_ratio_range=body_range,
                image_ink_density_threshold=image_threshold,
                text_ink_density_range=text_density_range,
                projection_periodicity_threshold=periodicity,
                role_priority=priority,
                header_band_ratio=header_band,
                footer_band_ratio=footer_band,
            ),
            columns=ColumnConfig(
                enabled=columns_enabled,
                gap_threshold=gap,
                overlap_threshold=overlap,
                spanning_width_ratio=spanning,
            ),
            reading=ReadingConfig(rule=rule, region_band_tolerance=tolerance),
            runtime=RuntimeConfig(
                thread_pool_workers=workers,
                batch_page_budget=page_budget,
                interaction_latency_budget_ms=latency_budget,
                snapshot_commit_interval=commit_interval,
            ),
            adaptive=AdaptiveConfig(
                enabled=adaptive_enabled,
                balanced_pressure=balanced_pressure,
                fast_pressure=fast_pressure,
            ),
            raw=root,
        )


def validate_role_priority(priority: Iterable[str]) -> bool:
    values = tuple(priority)
    return len(values) == len(ROLES) and set(values) == set(ROLES)
