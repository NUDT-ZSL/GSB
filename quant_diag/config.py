from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import E_INVALID_CONFIG, BusinessError
from .util import json_digest


@dataclass(frozen=True, order=True)
class Injection:
    kind: str
    layer_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "layer_id": self.layer_id}

    @staticmethod
    def parse(value: str) -> "Injection":
        if "@" in value:
            kind, layer_id = value.split("@", 1)
            return Injection(kind.strip(), layer_id.strip() or None)
        return Injection(value.strip(), None)


@dataclass(frozen=True)
class EngineConfig:
    group_size: int = 16
    degrade_group_size: int = 8
    scale_storage_dtype: str = "float16"
    accumulation_dtype: str = "float32"
    tolerance_abs: float = 0.30
    tolerance_rel: float = 0.50
    min_calibration_samples: int = 2
    max_layers_per_stage: int = 64
    max_resident_weight_mb: float = 8.0
    max_calibration_comparisons: int = 2_000_000
    injections: tuple[Injection, ...] = field(default_factory=tuple)
    input_override: tuple[float, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_size": self.group_size,
            "degrade_group_size": self.degrade_group_size,
            "scale_storage_dtype": self.scale_storage_dtype,
            "accumulation_dtype": self.accumulation_dtype,
            "tolerance_abs": self.tolerance_abs,
            "tolerance_rel": self.tolerance_rel,
            "min_calibration_samples": self.min_calibration_samples,
            "max_layers_per_stage": self.max_layers_per_stage,
            "max_resident_weight_mb": self.max_resident_weight_mb,
            "max_calibration_comparisons": self.max_calibration_comparisons,
            "injections": [item.as_dict() for item in sorted(self.injections)],
            "input_override": (
                list(self.input_override) if self.input_override is not None else None
            ),
        }

    @property
    def digest(self) -> str:
        return json_digest(self.as_dict())

    def injection(self, kind: str, layer_id: str | None = None) -> bool:
        for item in self.injections:
            if item.kind != kind:
                continue
            if item.layer_id is None or item.layer_id == layer_id:
                return True
        return False

    @staticmethod
    def from_mapping(value: dict[str, Any] | None) -> "EngineConfig":
        if value is None:
            return EngineConfig()
        allowed = {
            "group_size",
            "degrade_group_size",
            "scale_storage_dtype",
            "accumulation_dtype",
            "tolerance_abs",
            "tolerance_rel",
            "min_calibration_samples",
            "max_layers_per_stage",
            "max_resident_weight_mb",
            "max_calibration_comparisons",
            "injections",
            "input_override",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise BusinessError(
                E_INVALID_CONFIG,
                f"unknown configuration keys: {', '.join(unknown)}",
                details={"unknown_keys": unknown},
            )
        injections: Iterable[Any] = value.get("injections", ())
        parsed_injections: list[Injection] = []
        for item in injections:
            if isinstance(item, str):
                parsed_injections.append(Injection.parse(item))
            elif isinstance(item, dict):
                parsed_injections.append(
                    Injection(str(item.get("kind", "")), item.get("layer_id"))
                )
            else:
                raise BusinessError(E_INVALID_CONFIG, "invalid injection entry")
        override = value.get("input_override")
        config = EngineConfig(
            group_size=int(value.get("group_size", 16)),
            degrade_group_size=int(value.get("degrade_group_size", 8)),
            scale_storage_dtype=str(value.get("scale_storage_dtype", "float16")),
            accumulation_dtype=str(value.get("accumulation_dtype", "float32")),
            tolerance_abs=float(value.get("tolerance_abs", 0.30)),
            tolerance_rel=float(value.get("tolerance_rel", 0.50)),
            min_calibration_samples=int(value.get("min_calibration_samples", 2)),
            max_layers_per_stage=int(value.get("max_layers_per_stage", 64)),
            max_resident_weight_mb=float(value.get("max_resident_weight_mb", 8.0)),
            max_calibration_comparisons=int(
                value.get("max_calibration_comparisons", 2_000_000)
            ),
            injections=tuple(sorted(parsed_injections)),
            input_override=(
                tuple(float(item) for item in override)
                if override is not None
                else None
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.group_size <= 0 or self.degrade_group_size <= 0:
            raise BusinessError(E_INVALID_CONFIG, "group sizes must be positive")
        if self.scale_storage_dtype not in {"float16", "float64"}:
            raise BusinessError(
                E_INVALID_CONFIG,
                f"unsupported scale_storage_dtype: {self.scale_storage_dtype}",
            )
        if self.accumulation_dtype not in {"float32", "float64"}:
            raise BusinessError(
                E_INVALID_CONFIG,
                f"unsupported accumulation_dtype: {self.accumulation_dtype}",
            )
        if self.tolerance_abs < 0 or self.tolerance_rel < 0:
            raise BusinessError(E_INVALID_CONFIG, "tolerances must be non-negative")
        if self.min_calibration_samples <= 0:
            raise BusinessError(
                E_INVALID_CONFIG, "min_calibration_samples must be positive"
            )
        if self.max_layers_per_stage <= 0:
            raise BusinessError(
                E_INVALID_CONFIG, "max_layers_per_stage must be positive"
            )
        if self.max_resident_weight_mb <= 0:
            raise BusinessError(
                E_INVALID_CONFIG, "max_resident_weight_mb must be positive"
            )
        if self.max_calibration_comparisons <= 0:
            raise BusinessError(
                E_INVALID_CONFIG, "max_calibration_comparisons must be positive"
            )
