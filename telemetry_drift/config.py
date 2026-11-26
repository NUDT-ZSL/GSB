"""Typed, validated predictor pipeline configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import read_json, sha256_value
from .errors import DriftError, E_CONFIG_INVALID


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DriftError(E_CONFIG_INVALID, message)


@dataclass(frozen=True)
class VariableSpec:
    id: int
    name: str
    source: str
    role: str
    sample_rate_hz: float

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VariableSpec":
        try:
            item = cls(
                id=int(value["id"]),
                name=str(value["name"]),
                source=str(value["source"]),
                role=str(value["role"]),
                sample_rate_hz=float(value["sample_rate_hz"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DriftError(E_CONFIG_INVALID, "invalid variable declaration") from exc
        _require(item.id >= 0, "variable id must be non-negative")
        _require(item.role in {"input", "output", "aux"}, "invalid variable role")
        _require(item.sample_rate_hz > 0, "sample_rate_hz must be positive")
        return item

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "role": self.role,
            "sample_rate_hz": self.sample_rate_hz,
        }


@dataclass(frozen=True)
class AlignmentConfig:
    period_ms: int = 100
    stale_after_ms: int = 250
    missing_strategy: str = "forward_fill"

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "AlignmentConfig":
        value = value or {}
        item = cls(
            period_ms=int(value.get("period_ms", 100)),
            stale_after_ms=int(value.get("stale_after_ms", 250)),
            missing_strategy=str(value.get("missing_strategy", "forward_fill")),
        )
        _require(
            item.period_ms >= 0,
            "alignment.period_ms must be non-negative (0 means infer)",
        )
        _require(item.stale_after_ms >= 0, "alignment.stale_after_ms must be non-negative")
        _require(
            item.missing_strategy in {"forward_fill", "minimal_change"},
            "unknown missing strategy",
        )
        return item

    def to_dict(self) -> dict[str, Any]:
        return {
            "period_ms": self.period_ms,
            "stale_after_ms": self.stale_after_ms,
            "missing_strategy": self.missing_strategy,
        }


@dataclass(frozen=True)
class ResidualWindowConfig:
    size: int = 8
    step: int = 2
    max_backtrack: int = 16

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ResidualWindowConfig":
        value = value or {}
        item = cls(
            size=int(value.get("size", 8)),
            step=int(value.get("step", 2)),
            max_backtrack=int(value.get("max_backtrack", 16)),
        )
        _require(item.size >= 3, "residual_window.size must be at least 3")
        _require(item.step >= 1, "residual_window.step must be positive")
        _require(item.max_backtrack >= 0, "max_backtrack must be non-negative")
        return item

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "step": self.step,
            "max_backtrack": self.max_backtrack,
        }


@dataclass(frozen=True)
class StatisticsConfig:
    decay: float = 0.82
    skew_weight: float = 0.30
    trend_weight: float = 0.35
    upstream_weight: float = 0.25
    variance_floor: float = 1e-12
    residual_scale: float = 1.0
    input_scale: float = 1.0

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "StatisticsConfig":
        value = value or {}
        item = cls(
            decay=float(value.get("decay", 0.82)),
            skew_weight=float(value.get("skew_weight", 0.30)),
            trend_weight=float(value.get("trend_weight", 0.35)),
            upstream_weight=float(value.get("upstream_weight", 0.25)),
            variance_floor=float(value.get("variance_floor", 1e-12)),
            residual_scale=float(value.get("residual_scale", 1.0)),
            input_scale=float(value.get("input_scale", 1.0)),
        )
        _require(0 < item.decay <= 1, "statistics.decay must be in (0, 1]")
        _require(item.residual_scale > 0, "statistics.residual_scale must be positive")
        _require(item.input_scale > 0, "statistics.input_scale must be positive")
        _require(item.variance_floor >= 0, "variance_floor must be non-negative")
        for name in ("skew_weight", "trend_weight", "upstream_weight"):
            _require(getattr(item, name) >= 0, f"{name} must be non-negative")
        return item

    def to_dict(self) -> dict[str, Any]:
        return {
            "decay": self.decay,
            "skew_weight": self.skew_weight,
            "trend_weight": self.trend_weight,
            "upstream_weight": self.upstream_weight,
            "variance_floor": self.variance_floor,
            "residual_scale": self.residual_scale,
            "input_scale": self.input_scale,
        }


@dataclass(frozen=True)
class ThresholdConfig:
    policy: str = "tolerance"
    tolerance: float = 1e-9
    fixed_scale: int = 1_000_000
    subnormal_floor: float = 1e-12
    joint_rule: str = "both"

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ThresholdConfig":
        value = value or {}
        item = cls(
            policy=str(value.get("policy", "tolerance")),
            tolerance=float(value.get("tolerance", 1e-9)),
            fixed_scale=int(value.get("fixed_scale", 1_000_000)),
            subnormal_floor=float(value.get("subnormal_floor", 1e-12)),
            joint_rule=str(value.get("joint_rule", "both")),
        )
        _require(item.policy in {"strict", "tolerance"}, "unknown comparison policy")
        _require(item.tolerance >= 0, "threshold tolerance must be non-negative")
        _require(item.fixed_scale > 0, "fixed_scale must be positive")
        _require(item.subnormal_floor >= 0, "subnormal_floor must be non-negative")
        _require(item.joint_rule in {"both", "any"}, "joint_rule must be both or any")
        return item

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "tolerance": self.tolerance,
            "fixed_scale": self.fixed_scale,
            "subnormal_floor": self.subnormal_floor,
            "joint_rule": self.joint_rule,
        }


@dataclass(frozen=True)
class RetrainConfig:
    learning_rate: float = 0.08
    epochs: int = 24
    l2: float = 1e-4
    closure_radius: int = 1

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "RetrainConfig":
        value = value or {}
        item = cls(
            learning_rate=float(value.get("learning_rate", 0.08)),
            epochs=int(value.get("epochs", 24)),
            l2=float(value.get("l2", 1e-4)),
            closure_radius=int(value.get("closure_radius", 1)),
        )
        _require(item.learning_rate > 0, "learning_rate must be positive")
        _require(item.epochs > 0, "epochs must be positive")
        _require(item.l2 >= 0, "l2 must be non-negative")
        _require(item.closure_radius >= 0, "closure_radius must be non-negative")
        return item

    def to_dict(self) -> dict[str, Any]:
        return {
            "learning_rate": self.learning_rate,
            "epochs": self.epochs,
            "l2": self.l2,
            "closure_radius": self.closure_radius,
        }


@dataclass(frozen=True)
class PredictorConfig:
    id: str = "predictor-main"
    model_type: str = "linear"
    input_variables: tuple[int, ...] = (0, 1)
    output_variable: int = 2
    drift_threshold: float = 0.35
    input_amplitude_threshold: float = 0.10
    recycle_rate_threshold: float = 0.20

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PredictorConfig":
        try:
            item = cls(
                id=str(value.get("id", "predictor-main")),
                model_type=str(value.get("model_type", "linear")),
                input_variables=tuple(int(v) for v in value["input_variables"]),
                output_variable=int(value["output_variable"]),
                drift_threshold=float(value.get("drift_threshold", 0.35)),
                input_amplitude_threshold=float(
                    value.get("input_amplitude_threshold", 0.10)
                ),
                recycle_rate_threshold=float(
                    value.get("recycle_rate_threshold", 0.20)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DriftError(E_CONFIG_INVALID, "invalid predictor declaration") from exc
        _require(item.model_type in {"linear"}, "only linear predictors are supported")
        _require(bool(item.input_variables), "predictor needs at least one input")
        _require(
            len(set(item.input_variables)) == len(item.input_variables),
            "duplicate predictor input variable",
        )
        _require(
            item.output_variable not in item.input_variables,
            "output variable cannot also be an input",
        )
        _require(item.drift_threshold >= 0, "drift_threshold must be non-negative")
        _require(
            item.input_amplitude_threshold >= 0,
            "input_amplitude_threshold must be non-negative",
        )
        _require(
            0 <= item.recycle_rate_threshold <= 1,
            "recycle_rate_threshold must be in [0, 1]",
        )
        return item

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model_type": self.model_type,
            "input_variables": list(self.input_variables),
            "output_variable": self.output_variable,
            "drift_threshold": self.drift_threshold,
            "input_amplitude_threshold": self.input_amplitude_threshold,
            "recycle_rate_threshold": self.recycle_rate_threshold,
        }


@dataclass(frozen=True)
class PipelineConfig:
    schema_version: int
    seed: int
    alignment: AlignmentConfig
    residual_window: ResidualWindowConfig
    statistics: StatisticsConfig
    thresholds: ThresholdConfig
    retrain: RetrainConfig
    predictor: PredictorConfig
    variables: tuple[VariableSpec, ...]
    source_path: str | None = field(default=None, compare=False)

    @classmethod
    def from_dict(
        cls, value: dict[str, Any], *, source_path: str | None = None
    ) -> "PipelineConfig":
        try:
            variables = tuple(
                VariableSpec.from_dict(item) for item in value["variables"]
            )
            item = cls(
                schema_version=int(value.get("schema_version", 1)),
                seed=int(value.get("seed", 0)),
                alignment=AlignmentConfig.from_dict(value.get("alignment")),
                residual_window=ResidualWindowConfig.from_dict(
                    value.get("residual_window")
                ),
                statistics=StatisticsConfig.from_dict(value.get("statistics")),
                thresholds=ThresholdConfig.from_dict(value.get("thresholds")),
                retrain=RetrainConfig.from_dict(value.get("retrain")),
                predictor=PredictorConfig.from_dict(value["predictor"]),
                variables=variables,
                source_path=source_path,
            )
        except KeyError as exc:
            raise DriftError(E_CONFIG_INVALID, f"missing config field: {exc}") from exc
        ids = {variable.id for variable in variables}
        _require(item.schema_version == 1, "unsupported config schema_version")
        _require(len(ids) == len(variables), "duplicate variable id")
        _require(
            set(item.predictor.input_variables) <= ids,
            "predictor references unknown input variable",
        )
        _require(
            item.predictor.output_variable in ids,
            "predictor references unknown output variable",
        )
        roles = {variable.id: variable.role for variable in variables}
        _require(
            all(roles[var_id] == "input" for var_id in item.predictor.input_variables),
            "predictor input variable role mismatch",
        )
        _require(
            roles[item.predictor.output_variable] == "output",
            "predictor output variable role mismatch",
        )
        return item

    @classmethod
    def load(cls, path: str | Path) -> "PipelineConfig":
        return cls.from_dict(read_json(path), source_path=str(path))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "seed": self.seed,
            "alignment": self.alignment.to_dict(),
            "residual_window": self.residual_window.to_dict(),
            "statistics": self.statistics.to_dict(),
            "thresholds": self.thresholds.to_dict(),
            "retrain": self.retrain.to_dict(),
            "predictor": self.predictor.to_dict(),
            "variables": [variable.to_dict() for variable in self.variables],
        }

    def digest(self) -> str:
        return sha256_value(self.to_dict())

    def retrain_digest(self) -> str:
        return sha256_value(
            {
                "retrain": self.retrain.to_dict(),
                "statistics": self.statistics.to_dict(),
                "thresholds": self.thresholds.to_dict(),
            }
        )

    def variable_map(self) -> dict[int, VariableSpec]:
        return {variable.id: variable for variable in self.variables}

    def expected_period_ms(self) -> float:
        rates = [variable.sample_rate_hz for variable in self.variables]
        gcd_rate = rates[0]
        for rate in rates[1:]:
            gcd_rate = math.gcd(int(round(gcd_rate)), int(round(rate)))
        return max(1, int(round(1000 / gcd_rate)))
