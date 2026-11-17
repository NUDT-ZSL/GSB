"""Configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import E_CONFIG_INVALID, E_WIDTH_INVALID, PipelineError
from .io_utils import content_digest


DEFAULT_STAGE_TOLERANCES = {
    "normalization": 1.0e-12,
    "quantization": 1.0e-6,
    "fusion": 1.0e-5,
    "truncation": 1.0e-5,
}


@dataclass(frozen=True)
class PipelineConfig:
    path: Path
    raw: dict[str, Any]
    config_hash: str
    normalization: str
    zero_denominator_policy: str
    width: int
    max_width: int
    quantization_base: int
    quantization_levels: int
    temperature: float
    clip_min: float
    clip_max: float
    score_decimals: int
    concurrency: int
    global_budget: int
    per_request_quota: int
    snapshot_commit_interval: int
    injection_path: Path | None
    known_feature_groups: tuple[str, ...]
    high_width: int
    trace_tolerances: dict[str, float]

    @property
    def scale(self) -> int:
        return self.quantization_base ** self.width

    @property
    def high_scale(self) -> int:
        return self.quantization_base ** self.high_width


def _require_int(value: Any, name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PipelineError(E_CONFIG_INVALID, "config", f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise PipelineError(E_CONFIG_INVALID, "config", f"{name} must be >= {minimum}")
    return value


def _require_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PipelineError(E_CONFIG_INVALID, "config", f"{name} must be numeric")
    return float(value)


def load_config(path: str | Path) -> PipelineConfig:
    config_path = Path(path).resolve()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw_loaded = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise PipelineError(E_CONFIG_INVALID, "config", f"cannot read config: {exc}") from exc
    if not isinstance(raw_loaded, dict):
        raise PipelineError(E_CONFIG_INVALID, "config", "config root must be a mapping")
    raw = raw_loaded

    normalization_cfg = raw.get("normalization", {})
    if not isinstance(normalization_cfg, dict):
        raise PipelineError(E_CONFIG_INVALID, "config", "normalization must be a mapping")
    normalization = str(normalization_cfg.get("method", "minmax")).lower()
    if normalization not in {"minmax", "zscore"}:
        raise PipelineError(E_CONFIG_INVALID, "config", "normalization.method must be minmax or zscore")
    zero_policy = str(normalization_cfg.get("zero_denominator_policy", "zero"))
    if zero_policy != "zero":
        raise PipelineError(E_CONFIG_INVALID, "config", "only zero_denominator_policy=zero is supported")

    fixed = raw.get("fixed_point", {})
    if not isinstance(fixed, dict):
        raise PipelineError(E_CONFIG_INVALID, "config", "fixed_point must be a mapping")
    width = _require_int(fixed.get("width"), "fixed_point.width")
    max_width = _require_int(fixed.get("max_width", 12), "fixed_point.max_width", 0)
    base = _require_int(fixed.get("base", 10), "fixed_point.base", 2)
    if width < 0 or width > max_width:
        raise PipelineError(
            E_WIDTH_INVALID,
            "config",
            f"fixed_point.width={width} is outside [0, max_width={max_width}]",
        )
    declared_levels = fixed.get("levels")
    levels = base ** width
    if declared_levels is not None:
        declared_levels = _require_int(declared_levels, "fixed_point.levels", 1)
        if declared_levels != levels:
            raise PipelineError(
                E_CONFIG_INVALID,
                "config",
                "fixed_point.levels must equal fixed_point.base ** fixed_point.width",
            )

    fusion = raw.get("fusion", {})
    if not isinstance(fusion, dict):
        raise PipelineError(E_CONFIG_INVALID, "config", "fusion must be a mapping")
    temperature = _require_number(fusion.get("temperature"), "fusion.temperature")
    if temperature <= 0.0:
        raise PipelineError(E_CONFIG_INVALID, "config", "fusion.temperature must be > 0")

    score = raw.get("score", {})
    if not isinstance(score, dict):
        raise PipelineError(E_CONFIG_INVALID, "config", "score must be a mapping")
    clip_min = _require_number(score.get("clip_min"), "score.clip_min")
    clip_max = _require_number(score.get("clip_max"), "score.clip_max")
    if clip_min > clip_max:
        raise PipelineError(E_CONFIG_INVALID, "config", "score.clip_min must be <= score.clip_max")
    score_decimals = _require_int(score.get("decimals", 8), "score.decimals", 0)

    scheduler = raw.get("scheduler", {})
    if not isinstance(scheduler, dict):
        raise PipelineError(E_CONFIG_INVALID, "config", "scheduler must be a mapping")
    concurrency = _require_int(scheduler.get("concurrency"), "scheduler.concurrency", 1)
    global_budget = _require_int(scheduler.get("global_budget"), "scheduler.global_budget", 0)
    per_request_quota = _require_int(
        scheduler.get("per_request_quota"), "scheduler.per_request_quota", 0
    )
    snapshot_interval = _require_int(
        scheduler.get("snapshot_commit_interval", 1),
        "scheduler.snapshot_commit_interval",
        1,
    )

    known_groups_value = raw.get("known_feature_groups", [])
    if not isinstance(known_groups_value, list) or not all(
        isinstance(item, str) and item for item in known_groups_value
    ):
        raise PipelineError(E_CONFIG_INVALID, "config", "known_feature_groups must be string list")
    known_groups = tuple(str(item) for item in known_groups_value)
    if len(set(known_groups)) != len(known_groups):
        raise PipelineError(E_CONFIG_INVALID, "config", "known_feature_groups has duplicates")

    trace = raw.get("trace", {})
    if not isinstance(trace, dict):
        raise PipelineError(E_CONFIG_INVALID, "config", "trace must be a mapping")
    high_width = _require_int(trace.get("high_width", width + 1), "trace.high_width")
    if high_width < 0 or high_width > max_width:
        raise PipelineError(
            E_WIDTH_INVALID,
            "config",
            f"trace.high_width={high_width} is outside [0, max_width={max_width}]",
        )
    if high_width != width + 1:
        raise PipelineError(
            E_CONFIG_INVALID,
            "config",
            "trace.high_width must be exactly fixed_point.width + 1",
        )
    tolerances_raw = trace.get("tolerances", {})
    if not isinstance(tolerances_raw, dict):
        raise PipelineError(E_CONFIG_INVALID, "config", "trace.tolerances must be a mapping")
    tolerances = dict(DEFAULT_STAGE_TOLERANCES)
    for stage in DEFAULT_STAGE_TOLERANCES:
        if stage in tolerances_raw:
            tolerances[stage] = _require_number(
                tolerances_raw[stage], f"trace.tolerances.{stage}"
            )
            if tolerances[stage] < 0:
                raise PipelineError(E_CONFIG_INVALID, "config", "trace tolerances must be >= 0")

    injection_value = raw.get("injection_list_path")
    injection_path = None
    if injection_value:
        injection_candidate = Path(str(injection_value))
        if not injection_candidate.is_absolute():
            injection_candidate = config_path.parent / injection_candidate
        injection_path = injection_candidate.resolve()

    config_hash = content_digest(raw)
    return PipelineConfig(
        path=config_path,
        raw=raw,
        config_hash=config_hash,
        normalization=normalization,
        zero_denominator_policy=zero_policy,
        width=width,
        max_width=max_width,
        quantization_base=base,
        quantization_levels=levels,
        temperature=temperature,
        clip_min=clip_min,
        clip_max=clip_max,
        score_decimals=score_decimals,
        concurrency=concurrency,
        global_budget=global_budget,
        per_request_quota=per_request_quota,
        snapshot_commit_interval=snapshot_interval,
        injection_path=injection_path,
        known_feature_groups=known_groups,
        high_width=high_width,
        trace_tolerances=tolerances,
    )
