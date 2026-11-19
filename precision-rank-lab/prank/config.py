"""Configuration loading, override merging and validation."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BusinessError

KNOWN_PRECISIONS = ("float64", "float32", "fixed")
KNOWN_ROUNDING = ("trunc", "floor", "half_up", "half_even")
KNOWN_GATE_STRATEGIES = ("strict", "tolerance_band")


@dataclass
class FixedConfig:
    fraction_bits: int
    scale: int
    rounding: str
    int_bits: int
    exp_terms: int


@dataclass
class Config:
    raw: dict[str, Any]
    config_version: int
    weights: dict[str, float]
    bias: float
    threshold: float
    tolerance: float
    gate_strategy: str
    top_k: int
    rerank_n: int
    category_penalty: float
    position_decay: float
    fixed: FixedConfig
    subnormal_protection: bool
    bounds: dict[str, float]
    degradation_policy: str
    interval_quantiles: list[float]
    near_threshold_delta: float
    report_hash_alg: str
    run_precision_overrides: dict[str, Any] = field(default_factory=dict)


def _invalid(field_name: str, message: str) -> BusinessError:
    return BusinessError("E_CONFIG_INVALID", "config", message, field=field_name)


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> Config:
    """Load a JSON config file, apply dotted-path overrides and validate."""
    p = Path(path)
    if not p.is_file():
        raise _invalid("config_path", f"config file not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _invalid("config_path", f"invalid JSON: {exc}") from exc
    if overrides:
        raw = _apply_overrides(raw, overrides)
    return build_config(raw)


def _apply_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(raw))
    for dotted, value in overrides.items():
        cursor = merged
        parts = dotted.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return merged


def build_config(raw: dict[str, Any]) -> Config:
    weights = raw.get("feature_weights")
    if not isinstance(weights, dict) or not weights:
        raise _invalid("feature_weights", "feature_weights must be a non-empty object")
    for name, value in weights.items():
        if not isinstance(value, (int, float)):
            raise _invalid(f"feature_weights.{name}", "weight must be numeric")

    threshold = raw.get("threshold")
    if not isinstance(threshold, (int, float)) or not (0.0 <= float(threshold) <= 1.0):
        raise _invalid("threshold", "threshold must be a number in [0, 1]")
    tolerance = raw.get("tolerance")
    if not isinstance(tolerance, (int, float)) or float(tolerance) < 0.0:
        raise _invalid("tolerance", "tolerance must be a non-negative number")
    if float(tolerance) > min(float(threshold), 1.0 - float(threshold)) + 1e-12:
        raise _invalid("tolerance", "tolerance band must stay inside [0, 1]")
    gate_strategy = raw.get("gate_strategy", "strict")
    if gate_strategy not in KNOWN_GATE_STRATEGIES:
        raise _invalid("gate_strategy", f"unknown gate strategy {gate_strategy!r}")

    top_k = raw.get("top_k")
    if not isinstance(top_k, int) or top_k <= 0:
        raise _invalid("top_k", "top_k must be a positive integer")
    rerank_n = raw.get("rerank_n", top_k)
    if not isinstance(rerank_n, int) or rerank_n <= 0:
        raise _invalid("rerank_n", "rerank_n must be a positive integer")

    rerank = raw.get("rerank", {})
    category_penalty = rerank.get("category_penalty", 0.0)
    position_decay = rerank.get("position_decay", 1.0)
    if not isinstance(category_penalty, (int, float)) or not (0.0 <= float(category_penalty) <= 1.0):
        raise _invalid("rerank.category_penalty", "must be in [0, 1]")
    if not isinstance(position_decay, (int, float)) or not (0.0 < float(position_decay) <= 1.0):
        raise _invalid("rerank.position_decay", "must be in (0, 1]")

    precision_raw = raw.get("precision", {})
    tiers = precision_raw.get("tiers", list(KNOWN_PRECISIONS))
    for tier in tiers:
        if tier not in KNOWN_PRECISIONS:
            raise BusinessError(
                "E_PRECISION_UNKNOWN", "config",
                f"unknown precision tier {tier!r}", field="precision.tiers",
                context={"bad_value": tier, "known": list(KNOWN_PRECISIONS)},
            )
    fixed_raw = precision_raw.get("fixed", {})
    fraction_bits = fixed_raw.get("fraction_bits", 16)
    if not isinstance(fraction_bits, int) or not (1 <= fraction_bits <= 60):
        raise _invalid("precision.fixed.fraction_bits", "must be an integer in [1, 60]")
    scale = fixed_raw.get("scale", 2 ** fraction_bits)
    if not isinstance(scale, int) or isinstance(scale, bool) or scale <= 0:
        raise BusinessError(
            "E_SCALE_INVALID", "config",
            f"fixed-point scale must be a positive integer, got {scale!r}",
            field="precision.fixed.scale", context={"bad_value": scale},
        )
    rounding = fixed_raw.get("rounding", "half_even")
    if rounding not in KNOWN_ROUNDING:
        raise _invalid("precision.fixed.rounding", f"unknown rounding mode {rounding!r}")
    int_bits = fixed_raw.get("int_bits", 62)
    if not isinstance(int_bits, int) or int_bits <= fraction_bits:
        raise _invalid("precision.fixed.int_bits", "must exceed fraction_bits")
    exp_terms = fixed_raw.get("exp_terms", 24)
    if not isinstance(exp_terms, int) or exp_terms <= 0:
        raise _invalid("precision.fixed.exp_terms", "must be a positive integer")

    bounds = raw.get("bounds", {})
    for name in ("price_min", "price_max", "quality_min", "quality_max"):
        if name not in bounds or not isinstance(bounds[name], (int, float)):
            raise _invalid(f"bounds.{name}", "bound must be numeric")
    if bounds["price_min"] <= 0 or bounds["price_max"] <= bounds["price_min"]:
        raise _invalid("bounds", "price bounds must satisfy 0 < min < max")
    if not (bounds["quality_min"] <= bounds["quality_max"]):
        raise _invalid("bounds", "quality bounds must satisfy min <= max")

    degradation = raw.get("degradation", {})
    policy = degradation.get("policy", "abort")
    allowed = degradation.get("allowed_policies", ["abort", "skip_query", "fallback_float64"])
    if policy not in allowed:
        raise _invalid("degradation.policy", f"policy {policy!r} not in allowed list {allowed}")

    report = raw.get("report", {})
    quantiles = report.get("interval_quantiles", [0.0, 0.5, 0.95, 1.0])
    if not all(isinstance(q, (int, float)) and 0.0 <= float(q) <= 1.0 for q in quantiles):
        raise _invalid("report.interval_quantiles", "quantiles must be in [0, 1]")
    if quantiles != sorted(quantiles):
        raise _invalid("report.interval_quantiles", "quantiles must be ascending")

    return Config(
        raw=raw,
        config_version=raw.get("config_version", 1),
        weights=dict(weights),
        bias=float(raw.get("score_bias", 0.0)),
        threshold=float(threshold),
        tolerance=float(tolerance),
        gate_strategy=gate_strategy,
        top_k=int(top_k),
        rerank_n=int(rerank_n),
        category_penalty=float(category_penalty),
        position_decay=float(position_decay),
        fixed=FixedConfig(
            fraction_bits=int(fraction_bits),
            scale=int(scale),
            rounding=rounding,
            int_bits=int(int_bits),
            exp_terms=int(exp_terms),
        ),
        subnormal_protection=bool(raw.get("subnormal_protection", True)),
        bounds=dict(bounds),
        degradation_policy=policy,
        interval_quantiles=[float(q) for q in quantiles],
        near_threshold_delta=float(report.get("near_threshold_delta", 0.02)),
        report_hash_alg=report.get("report_hash_alg", "sha256"),
        run_precision_overrides=raw.get("_runtime_overrides", {}),
    )
