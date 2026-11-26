from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .errors import E_INVALID_FRAME_CONFIG, E_PRECISION_PROFILE_UNKNOWN, DiagnosticError


PRECISION_PROFILES: dict[str, dict[str, Any]] = {
    "fp64": {
        "name": "fp64",
        "mode": "float64",
        "scale": None,
        "rounding": None,
        "description": "IEEE-754 binary64 reference profile",
    },
    "fp32": {
        "name": "fp32",
        "mode": "float32",
        "scale": None,
        "rounding": None,
        "description": "IEEE-754 binary32 declared intermediates",
    },
    "q15": {
        "name": "q15",
        "mode": "fixed",
        "scale": 32768,
        "rounding": "nearest_even",
        "description": "signed Q0.15-style scaled integers, ties to even",
    },
    "q10_half_up": {
        "name": "q10_half_up",
        "mode": "fixed",
        "scale": 1024,
        "rounding": "nearest_away",
        "description": "scale=1024 fixed point, ties away from zero",
    },
}


@dataclass(frozen=True)
class Config:
    seed: int = 20261127
    sample_rate: int = 16000
    frame_length_ms: int = 20
    frame_shift_ms: int = 10
    f0_min_hz: float = 72.0
    f0_max_hz: float = 420.0
    precision_profile: str = "fp64"
    tree_version: str = "both"
    conflict_threshold: int = 12
    near_threshold_band: float = 0.08
    stability_repeats: int = 3
    candidate_min_score: float = 0.05
    prune_boost: float = 0.20
    max_utterances_per_run: int = 12
    max_resident_frames: int = 4096
    max_cache_entries: int = 512
    energy_scale: float = 0.22
    f0_scale_hz: float = 240.0
    zcr_scale: float = 0.45
    pause_scale_ms: float = 500.0
    reset_scale: float = 0.45
    error_tolerances: dict[str, float] = field(
        default_factory=lambda: {
            "feature": 0.02,
            "node": 0.02,
            "confidence": 0.02,
            "classification": 0.03,
        }
    )

    @property
    def frame_length_samples(self) -> int:
        return round(self.sample_rate * self.frame_length_ms / 1000)

    @property
    def frame_shift_samples(self) -> int:
        return round(self.sample_rate * self.frame_shift_ms / 1000)

    def validate(self) -> None:
        field_name = "frame_length_ms"
        if not isinstance(self.frame_length_ms, int) or self.frame_length_ms <= 0:
            raise DiagnosticError(
                E_INVALID_FRAME_CONFIG,
                "frame_length_ms must be a positive integer",
                field=field_name,
                details={"value": self.frame_length_ms},
            )
        field_name = "frame_shift_ms"
        if not isinstance(self.frame_shift_ms, int) or self.frame_shift_ms <= 0:
            raise DiagnosticError(
                E_INVALID_FRAME_CONFIG,
                "frame_shift_ms must be a positive integer",
                field=field_name,
                details={"value": self.frame_shift_ms},
            )
        if self.frame_shift_ms > self.frame_length_ms:
            raise DiagnosticError(
                E_INVALID_FRAME_CONFIG,
                "frame_shift_ms must not exceed frame_length_ms",
                field="frame_shift_ms",
                details={
                    "frame_shift_ms": self.frame_shift_ms,
                    "frame_length_ms": self.frame_length_ms,
                },
            )
        if self.sample_rate <= 0 or self.sample_rate % 100:
            raise DiagnosticError(
                E_INVALID_FRAME_CONFIG,
                "sample_rate must be a positive integer divisible by 100",
                field="sample_rate",
                details={"value": self.sample_rate},
            )
        if self.precision_profile not in PRECISION_PROFILES:
            raise DiagnosticError(
                E_PRECISION_PROFILE_UNKNOWN,
                "unknown precision_profile",
                field="precision_profile",
                details={
                    "value": self.precision_profile,
                    "known": sorted(PRECISION_PROFILES),
                },
            )
        if self.tree_version not in {"v1", "v2", "both"}:
            raise DiagnosticError(
                E_INVALID_FRAME_CONFIG,
                "tree_version must be v1, v2, or both",
                field="tree_version",
                details={"value": self.tree_version},
            )

    def with_overrides(self, **overrides: Any) -> "Config":
        valid = {k: v for k, v in overrides.items() if v is not None}
        cfg = replace(self, **valid)
        cfg.validate()
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_precision_profile(name_or_spec: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(name_or_spec, dict):
        spec = dict(name_or_spec)
        if "name" not in spec:
            spec["name"] = "custom"
        if spec.get("mode") == "fixed":
            if not isinstance(spec.get("scale"), int) or spec["scale"] <= 0:
                raise DiagnosticError(
                    E_PRECISION_PROFILE_UNKNOWN,
                    "fixed precision profile requires a positive integer scale",
                    field="precision_profile.scale",
                    details={"profile": spec},
                )
            if spec.get("rounding") not in {"nearest_even", "nearest_away", "truncate"}:
                raise DiagnosticError(
                    E_PRECISION_PROFILE_UNKNOWN,
                    "fixed precision rounding must be nearest_even, nearest_away, or truncate",
                    field="precision_profile.rounding",
                    details={"profile": spec},
                )
        return spec
    aliases = {
        "float64": "fp64",
        "double": "fp64",
        "float32": "fp32",
        "single": "fp32",
        "fixed": "q15",
        "fixed_point": "q15",
        "int": "q15",
    }
    name_or_spec = aliases.get(name_or_spec, name_or_spec)
    if name_or_spec not in PRECISION_PROFILES:
        if name_or_spec.startswith("fixed:"):
            spec: dict[str, Any] = {"name": "cli_fixed", "mode": "fixed"}
            for part in name_or_spec[6:].split(","):
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                if key == "scale":
                    spec["scale"] = int(value)
                elif key == "rounding":
                    spec["rounding"] = value
            return resolve_precision_profile(spec)
        raise DiagnosticError(
            E_PRECISION_PROFILE_UNKNOWN,
            "unknown precision_profile",
            field="precision_profile",
            details={"value": name_or_spec, "known": sorted(PRECISION_PROFILES)},
        )
    return dict(PRECISION_PROFILES[name_or_spec])
