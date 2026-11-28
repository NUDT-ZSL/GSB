"""Tunable configuration for the focus fusion kernel.

Every default is fixed so that two runs with the same inputs produce identical
outputs. Parameters control adaptive window sizing, confidence thresholds,
neighbourhood smoothing and numerical tolerances used by incremental updates.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict


@dataclass(frozen=True)
class CoreConfig:
    """Immutable pipeline configuration.

    Attributes
    ----------
    base_window_fraction:
        Small sharpness window as a fraction of the minimum image dimension.
    large_window_fraction:
        Large sharpness window, capped by ``max_window_radius``.
    min_window_radius / max_window_radius:
        Inclusive integer bounds for adaptive window radii.
    laplacian_weight / gradient_weight:
        Combination weights for the two sharpness families.
    peak_margin_eps:
        Absolute floor added when normalising the peak-second peak margin.
    critical_peak_ratio:
        Confidence at or below this value marks a critically close peak.
    vote_radius:
        Radius of the confidence weighted majority-vote neighbourhood.
    isolated_confidence:
        Neighbours at least this confident may override an isolated pixel.
    isolated_min_agreement:
        Fraction of confident neighbours that must agree on one alternative.
    boundary_vote_entropy:
        Normalised vote entropy above which a pixel enters the boundary band.
    fusion_neighbours:
        Number of adjacent focal planes (each side) allowed into the ledger.
    fusion_temperature:
        Exponent applied to normalised sharpness when allocating weights.
    contrast_radius:
        Box radius for local variance used by the quality report.
    recompute_pad:
        Extra halo added around a sharpness change region for incremental work.
    compare_tol:
        Tolerance used when comparing incremental and full recompute results.
    """

    base_window_fraction: float = 0.02
    large_window_fraction: float = 0.05
    min_window_radius: int = 1
    max_window_radius: int = 12
    laplacian_weight: float = 0.6
    gradient_weight: float = 0.4
    peak_margin_eps: float = 1e-12
    critical_peak_ratio: float = 0.15
    vote_radius: int = 2
    isolated_confidence: float = 0.55
    isolated_min_agreement: float = 0.7
    boundary_vote_entropy: float = 0.45
    fusion_neighbours: int = 1
    fusion_temperature: float = 3.0
    contrast_radius: int = 3
    recompute_pad: int = 1
    compare_tol: float = 1e-9

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CoreConfig":
        fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - fields
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        return cls(**{k: v for k, v in data.items() if k in fields})
