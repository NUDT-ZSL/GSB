"""Deterministic positional-encoding variants and angle mapping adjudication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import DomainError, E_MAPPING_UNDECLARED, E_INVALID_CASE
from .hashing import hash_arrays
from .models import ResolvedCase, VARIANT_ALIASES, VARIANT_NAMES


PAIRING_CONVENTION = "half_split:dim[i] pairs with dim[i+head_dim/2]; Q and K share the same angle"
ROPE_IMPLEMENTATION = (
    "x'[i] = x[i] * cos(theta_i) - x[i+h] * sin(theta_i); "
    "x'[i+h] = x[i+h] * cos(theta_i) + x[i] * sin(theta_i)"
)


def _base_omega(head_dim: int, base_theta: float) -> np.ndarray:
    half_dim = head_dim // 2
    exponent = np.arange(half_dim, dtype=np.float64) * 2.0 / float(head_dim)
    return np.power(float(base_theta), -exponent)


def canonical_variant_name(variant: str) -> str:
    return VARIANT_ALIASES.get(str(variant).lower(), str(variant).lower())


def _block_factor(freq_block: tuple[float, ...], half_dim: int) -> np.ndarray:
    indices = np.arange(half_dim, dtype=np.int64)
    block_indices = (indices * len(freq_block)) // half_dim
    return np.asarray(freq_block, dtype=np.float64)[block_indices]


def variant_angle_transform(
    variant: str,
    head_dim: int,
    scale_ratio: float,
    freq_block: tuple[float, ...],
) -> tuple[float, np.ndarray]:
    """Return (position factor, per-pair frequency factor) relative to RoPE."""

    half_dim = head_dim // 2
    variant = canonical_variant_name(variant)
    if variant == "rope":
        return 1.0, np.ones(half_dim, dtype=np.float64)
    if variant == "position_interpolation":
        return 1.0 / float(scale_ratio), np.ones(half_dim, dtype=np.float64)
    if variant == "ntk_aware":
        denominator = max(head_dim - 2, 1)
        exponents = -(2.0 * np.arange(half_dim, dtype=np.float64)) / denominator
        return 1.0, np.power(float(scale_ratio), exponents)
    if variant == "block_frequency":
        return 1.0, _block_factor(freq_block, half_dim)
    raise DomainError(
        E_INVALID_CASE,
        f"unknown positional encoding variant: {variant}",
        {"variant": variant},
    )


def compute_angles(case: ResolvedCase, variant: str) -> np.ndarray:
    """Generate angles with shape (seq_len, head_dim / 2) in float64."""

    position_factor, frequency_factor = variant_angle_transform(
        variant,
        case.head_dim,
        case.scale_ratio,
        case.freq_block,
    )
    positions = np.arange(case.seq_len, dtype=np.float64)[:, None]
    omega = _base_omega(case.head_dim, case.base_theta)[None, :]
    return positions * (omega * position_factor * frequency_factor[None, :])


def generate_qkv(case: ResolvedCase) -> dict[str, np.ndarray]:
    """Generate Q/K/V from the case seed using only PCG64.

    The draw order is fixed as Q, then K, then V, each with shape
    (num_heads, seq_len, head_dim).
    """

    generator = np.random.Generator(np.random.PCG64(case.seed))
    shape = (case.num_heads, case.seq_len, case.head_dim)
    return {
        "q": generator.standard_normal(shape, dtype=np.float64),
        "k": generator.standard_normal(shape, dtype=np.float64),
        "v": generator.standard_normal(shape, dtype=np.float64),
    }


def rotate_qk(
    q: np.ndarray, k: np.ndarray, angles: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Apply half-split RoPE to Q and K with a shared angle tensor."""

    half_dim = q.shape[-1] // 2
    cos = np.cos(angles)[None, :, :]
    sin = np.sin(angles)[None, :, :]

    def rotate(value: np.ndarray) -> np.ndarray:
        first = value[..., :half_dim]
        second = value[..., half_dim:]
        return np.concatenate(
            (first * cos - second * sin, second * cos + first * sin),
            axis=-1,
        )

    return rotate(q), rotate(k)


@dataclass(frozen=True)
class MappingDeclaration:
    mapping_id: str
    source_variant: str
    target_variant: str
    formula: str
    description: str

    def prediction_ratio(
        self,
        head_dim: int,
        scale_ratio: float,
        freq_block: tuple[float, ...],
    ) -> np.ndarray:
        source_position, source_frequency = variant_angle_transform(
            self.source_variant, head_dim, scale_ratio, freq_block
        )
        target_position, target_frequency = variant_angle_transform(
            self.target_variant, head_dim, scale_ratio, freq_block
        )
        return (target_position * target_frequency) / (
            source_position * source_frequency
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping_id": self.mapping_id,
            "source_variant": self.source_variant,
            "target_variant": self.target_variant,
            "formula": self.formula,
            "description": self.description,
        }


class MappingRegistry:
    """Explicit registry; absence of a pair is a business error."""

    def __init__(self) -> None:
        self._declarations: dict[tuple[str, str], MappingDeclaration] = {}

    def register(self, declaration: MappingDeclaration) -> None:
        pair = (
            canonical_variant_name(declaration.source_variant),
            canonical_variant_name(declaration.target_variant),
        )
        if pair in self._declarations:
            raise ValueError(f"mapping already registered: {pair}")
        self._declarations[pair] = declaration

    def get(self, source_variant: str, target_variant: str) -> MappingDeclaration | None:
        return self._declarations.get(
            (
                canonical_variant_name(source_variant),
                canonical_variant_name(target_variant),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "declarations": [
                self._declarations[pair].to_dict()
                for pair in sorted(self._declarations)
            ]
        }


def build_builtin_mapping_registry() -> MappingRegistry:
    registry = MappingRegistry()
    for source in VARIANT_NAMES:
        for target in VARIANT_NAMES:
            if source == target:
                continue
            registry.register(
                MappingDeclaration(
                    mapping_id=f"transform-ratio:{source}->{target}",
                    source_variant=source,
                    target_variant=target,
                    formula=(
                        "predicted_target_angle[pos,i] = "
                        "source_angle[pos,i] * target_factor[i] / source_factor[i]"
                    ),
                    description=(
                        "Each variant is anchored to the same base RoPE frequency; "
                        "the mapping composes the target transform with the inverse "
                        "of the source transform. Position interpolation contributes "
                        "1/scale_ratio, NTK contributes scale_ratio^(-2i/(d-2)), "
                        "and block frequency contributes the piecewise block scale."
                    ),
                )
            )
    return registry


def circular_residual(observed: np.ndarray, predicted: np.ndarray) -> np.ndarray:
    difference = observed - predicted
    return (difference + np.pi) % (2.0 * np.pi) - np.pi


@dataclass(frozen=True)
class MappingJudgement:
    case_id: str
    source_variant: str
    target_variant: str
    code: str
    equivalent: bool
    tolerance: float
    declaration: MappingDeclaration | None
    residual_spectrum: np.ndarray | None
    per_dimension_max: np.ndarray | None
    max_residual: float | None
    max_location: dict[str, int] | None
    first_violation: dict[str, Any] | None
    angle_hashes: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "source_variant": self.source_variant,
            "target_variant": self.target_variant,
            "code": self.code,
            "equivalent": self.equivalent,
            "tolerance": self.tolerance,
            "declaration": (
                self.declaration.to_dict() if self.declaration is not None else None
            ),
            "residual_spectrum": (
                self.residual_spectrum.tolist()
                if self.residual_spectrum is not None
                else None
            ),
            "per_dimension_max": (
                self.per_dimension_max.tolist()
                if self.per_dimension_max is not None
                else None
            ),
            "max_residual": self.max_residual,
            "max_location": self.max_location,
            "first_violation": self.first_violation,
            "angle_hashes": dict(self.angle_hashes),
        }


def adjudicate_angles(
    case: ResolvedCase,
    source_variant: str,
    target_variant: str,
    source_angles: np.ndarray,
    target_angles: np.ndarray,
    registry: MappingRegistry,
    tolerance: float = 1e-9,
) -> MappingJudgement:
    source_variant = canonical_variant_name(source_variant)
    target_variant = canonical_variant_name(target_variant)
    angle_hashes = {
        source_variant: hash_arrays({"angles": source_angles}),
        target_variant: hash_arrays({"angles": target_angles}),
    }
    declaration = registry.get(source_variant, target_variant)
    if declaration is None:
        return MappingJudgement(
            case_id=case.case_id,
            source_variant=source_variant,
            target_variant=target_variant,
            code=E_MAPPING_UNDECLARED,
            equivalent=False,
            tolerance=tolerance,
            declaration=None,
            residual_spectrum=None,
            per_dimension_max=None,
            max_residual=None,
            max_location=None,
            first_violation={
                "dimension_index": 0,
                "deviation": 0.0,
                "reason": "mapping pair has no declaration",
            },
            angle_hashes=angle_hashes,
        )

    ratio = declaration.prediction_ratio(
        case.head_dim, case.scale_ratio, case.freq_block
    )
    predicted = source_angles * ratio[None, :]
    residual = circular_residual(target_angles, predicted)
    absolute = np.abs(residual)
    per_dimension = np.max(absolute, axis=0)
    flat_index = int(np.argmax(absolute))
    position, dim_index = np.unravel_index(flat_index, absolute.shape)
    max_residual = float(absolute[position, dim_index])
    max_location = {
        "position": int(position),
        "dimension_index": int(dim_index),
        "paired_dimensions": [int(dim_index), int(dim_index + case.head_dim // 2)],
    }
    violations = np.argwhere(absolute > tolerance)
    code = "OK"
    equivalent = True
    first_violation = None
    if violations.size:
        first_position, first_dim = (int(item) for item in violations[0])
        deviation = float(residual[first_position, first_dim])
        first_violation = {
            "position": first_position,
            "dimension_index": first_dim,
            "paired_dimensions": [first_dim, first_dim + case.head_dim // 2],
            "deviation": deviation,
            "absolute_deviation": abs(deviation),
        }
        code = "E_MAPPING_DRIFT"
        equivalent = False
    return MappingJudgement(
        case_id=case.case_id,
        source_variant=source_variant,
        target_variant=target_variant,
        code=code,
        equivalent=equivalent,
        tolerance=tolerance,
        declaration=declaration,
        residual_spectrum=residual,
        per_dimension_max=per_dimension,
        max_residual=max_residual,
        max_location=max_location,
        first_violation=first_violation,
        angle_hashes=angle_hashes,
    )
