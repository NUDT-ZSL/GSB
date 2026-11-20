"""Typed configuration objects and strict field-level validation.

The configuration is intentionally pure data (JSON-serialisable) so that a
configuration file can be checksummed and version-managed together with the
snapshots it produced.  Every invalid field raises ``E_CONFIG_INVALID`` with the
offending field name attached.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields

from ..errors import E_CONFIG_INVALID, EngineError

PRECISIONS = ("float64", "float32", "fixed")
ROUNDINGS = ("half_even", "half_up", "truncate")
REDUCTION_ORDERS = ("sequential",)
CONVERGENCE_STRATEGIES = ("strict", "tolerance_band")

MAX_FIXED_SCALE = 2 ** 31
MAX_REGULARIZATION = 1.0


@dataclass
class ResourceLimits:
    max_dialogs: int = 64
    max_resident_segments: int = 4096
    max_gradient_bytes: int = 64 * 1024 * 1024
    min_chunk_size: int = 4


@dataclass
class EngineConfig:
    # --- audio / framing -------------------------------------------------
    sample_rate: int = 16000
    frame_ms: float = 20.0
    hop_ms: float = 10.0
    silence_z_threshold: float = 3.0
    min_silence_frames: int = 2
    # --- precision / arithmetic -----------------------------------------
    precision: str = "float64"
    fixed_scale: int = 1 << 16
    rounding: str = "half_even"
    chunk_size: int = 16
    reduction_order: str = "sequential"
    subnormal_protection: bool = True
    # --- training --------------------------------------------------------
    epochs: int = 40
    learning_rate: float = 0.02
    lr_scale: float = 1.0
    regularization: float = 1e-4
    convergence_tolerance: float = 1e-7
    min_improvement: float = 0.0
    convergence_strategy: str = "tolerance_band"
    tolerance_band: float = 1e-12
    # --- feature scaling ablation knob ----------------------------------
    feature_scale: float = 1.0
    # --- reproducibility / resources ------------------------------------
    random_seed: int = 20261120
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    snapshot_schema_version: int = 1

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, raw: dict) -> "EngineConfig":
        if not isinstance(raw, dict):
            raise EngineError(E_CONFIG_INVALID, "configuration must be an object",
                              field="root")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise EngineError(E_CONFIG_INVALID,
                              f"unknown configuration field(s): {unknown}",
                              field=unknown[0])
        values = dict(raw)
        if "resource_limits" in values and values["resource_limits"] is not None:
            rl_raw = values.pop("resource_limits")
            rl_known = {f.name for f in fields(ResourceLimits)}
            rl_unknown = sorted(set(rl_raw) - rl_known)
            if rl_unknown:
                raise EngineError(E_CONFIG_INVALID,
                                  f"unknown resource limit field(s): {rl_unknown}",
                                  field=f"resource_limits.{rl_unknown[0]}")
            values["resource_limits"] = ResourceLimits(**rl_raw)
        cfg = cls(**values)
        cfg.validate()
        return cfg

    @classmethod
    def from_file(cls, path: str) -> "EngineConfig":
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def to_dict(self) -> dict:
        data = asdict(self)
        return json.loads(json.dumps(data, sort_keys=True))

    def canonical_json(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    # ------------------------------------------------------------------ #
    # Validation (requirement 7)
    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        if self.sample_rate != 16000:
            raise EngineError(E_CONFIG_INVALID,
                              f"sample_rate must be 16000, got {self.sample_rate}",
                              field="sample_rate")
        if not isinstance(self.frame_ms, (int, float)) or not (0 < self.frame_ms <= 1000):
            raise EngineError(E_CONFIG_INVALID,
                              f"frame_ms must be in (0, 1000], got {self.frame_ms}",
                              field="frame_ms")
        frame_len = self.sample_rate * self.frame_ms / 1000.0
        if abs(frame_len - round(frame_len)) > 1e-9:
            raise EngineError(E_CONFIG_INVALID,
                              f"frame_ms {self.frame_ms} does not align to whole samples",
                              field="frame_ms")
        if not isinstance(self.hop_ms, (int, float)) or self.hop_ms <= 0:
            raise EngineError(E_CONFIG_INVALID,
                              f"hop_ms must be positive, got {self.hop_ms}",
                              field="hop_ms")
        hop_len = self.sample_rate * self.hop_ms / 1000.0
        if abs(hop_len - round(hop_len)) > 1e-9:
            raise EngineError(E_CONFIG_INVALID,
                              f"hop_ms {self.hop_ms} does not align to whole samples",
                              field="hop_ms")
        if self.hop_ms > self.frame_ms:
            raise EngineError(E_CONFIG_INVALID,
                              f"hop_ms ({self.hop_ms}) must not exceed frame_ms "
                              f"({self.frame_ms})", field="hop_ms")
        if self.precision not in PRECISIONS:
            raise EngineError(E_CONFIG_INVALID,
                              f"precision must be one of {PRECISIONS}, got {self.precision}",
                              field="precision")
        if self.rounding not in ROUNDINGS:
            raise EngineError(E_CONFIG_INVALID,
                              f"rounding must be one of {ROUNDINGS}, got {self.rounding}",
                              field="rounding")
        if not isinstance(self.fixed_scale, int) or isinstance(self.fixed_scale, bool):
            raise EngineError(E_CONFIG_INVALID,
                              "fixed_scale must be an integer", field="fixed_scale")
        if self.fixed_scale <= 0:
            raise EngineError(E_CONFIG_INVALID,
                              f"fixed_scale must be positive, got {self.fixed_scale}",
                              field="fixed_scale")
        if self.fixed_scale > MAX_FIXED_SCALE:
            raise EngineError(E_CONFIG_INVALID,
                              f"fixed_scale {self.fixed_scale} exceeds max "
                              f"{MAX_FIXED_SCALE}", field="fixed_scale")
        if not (0.0 <= self.regularization <= MAX_REGULARIZATION):
            raise EngineError(E_CONFIG_INVALID,
                              f"regularization must be in [0, {MAX_REGULARIZATION}], "
                              f"got {self.regularization}", field="regularization")
        if self.convergence_tolerance <= 0:
            raise EngineError(E_CONFIG_INVALID,
                              f"convergence_tolerance must be positive, got "
                              f"{self.convergence_tolerance}",
                              field="convergence_tolerance")
        if self.min_improvement < 0:
            raise EngineError(E_CONFIG_INVALID,
                              "min_improvement must be non-negative",
                              field="min_improvement")
        if self.convergence_strategy not in CONVERGENCE_STRATEGIES:
            raise EngineError(E_CONFIG_INVALID,
                              f"convergence_strategy must be one of "
                              f"{CONVERGENCE_STRATEGIES}",
                              field="convergence_strategy")
        if self.tolerance_band < 0:
            raise EngineError(E_CONFIG_INVALID,
                              "tolerance_band must be non-negative",
                              field="tolerance_band")
        if self.epochs <= 0:
            raise EngineError(E_CONFIG_INVALID,
                              f"epochs must be positive, got {self.epochs}",
                              field="epochs")
        if self.learning_rate <= 0:
            raise EngineError(E_CONFIG_INVALID,
                              "learning_rate must be positive", field="learning_rate")
        if self.lr_scale <= 0:
            raise EngineError(E_CONFIG_INVALID,
                              "lr_scale must be positive", field="lr_scale")
        if self.feature_scale <= 0:
            raise EngineError(E_CONFIG_INVALID,
                              "feature_scale must be positive", field="feature_scale")
        if self.chunk_size < 0:
            raise EngineError(E_CONFIG_INVALID,
                              "chunk_size must be >= 0 (0 means whole batch)",
                              field="chunk_size")
        if self.reduction_order not in REDUCTION_ORDERS:
            raise EngineError(E_CONFIG_INVALID,
                              f"reduction_order must be one of {REDUCTION_ORDERS}",
                              field="reduction_order")
        if self.min_silence_frames < 1:
            raise EngineError(E_CONFIG_INVALID,
                              "min_silence_frames must be >= 1",
                              field="min_silence_frames")
        rl = self.resource_limits
        for name, value in asdict(rl).items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise EngineError(E_CONFIG_INVALID,
                                  f"resource_limits.{name} must be a positive integer",
                                  field=f"resource_limits.{name}")

    @property
    def frame_length(self) -> int:
        return int(round(self.sample_rate * self.frame_ms / 1000.0))

    @property
    def hop_length(self) -> int:
        return int(round(self.sample_rate * self.hop_ms / 1000.0))
