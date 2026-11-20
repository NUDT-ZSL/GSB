"""Fixed-point table generation, dequantization, and Gather."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import NumericsConfig
from .errors import (
    E_DTYPE_UNKNOWN,
    E_QUANT_INVALID,
    E_SHAPE_MISMATCH,
    business_error,
)
from .numeric import apply_numeric_policy, round_values
from .util import array_digest


@dataclass(frozen=True)
class TierInfo:
    name: str
    kind: str
    numpy_dtype: np.dtype[Any]
    qmin: int | None = None
    qmax: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "numpy_dtype": self.numpy_dtype.name,
            "quantized_range": (
                [self.qmin, self.qmax] if self.qmin is not None else None
            ),
        }


TIERS: dict[str, TierInfo] = {
    "float64": TierInfo("float64", "float", np.dtype(np.float64)),
    "float32": TierInfo("float32", "float", np.dtype(np.float32)),
    "qint8": TierInfo("qint8", "quantized", np.dtype(np.int8), -128, 127),
    "qint16": TierInfo("qint16", "quantized", np.dtype(np.int16), -32768, 32767),
}


@dataclass(frozen=True)
class QuantizedTable:
    table_dtype: str
    tier: TierInfo
    storage: np.ndarray
    dequantized: np.ndarray
    quant_scale: float
    quant_zero_point: int
    quant_applicable: bool
    clipped_values: int
    storage_digest: str
    dequantized_digest: str
    independent_recompute_diff: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_dtype": self.table_dtype,
            "tier": self.tier.to_dict(),
            "quant_applicable": self.quant_applicable,
            "quant_scale": self.quant_scale,
            "quant_zero_point": self.quant_zero_point,
            "clipped_values": self.clipped_values,
            "storage_shape": list(self.storage.shape),
            "storage_digest": self.storage_digest,
            "dequantized_digest": self.dequantized_digest,
            "independent_recompute_diff": self.independent_recompute_diff,
        }


def resolve_tier(table_dtype: str, job_id: str) -> TierInfo:
    tier = TIERS.get(table_dtype)
    if tier is None:
        raise business_error(
            E_DTYPE_UNKNOWN,
            "table_build",
            job_id,
            f"unknown table_dtype tier: {table_dtype!r}",
            table_dtype=table_dtype,
            known_tiers=list(TIERS),
        )
    return tier


def validate_table_shape(
    truth_table: np.ndarray,
    *,
    vocab_size: int,
    embed_dim: int,
    job_id: str,
) -> np.ndarray:
    table = np.asarray(truth_table)
    if table.ndim != 2:
        raise business_error(
            E_SHAPE_MISMATCH,
            "table_build",
            job_id,
            f"embedding table must be 2-D, got shape {table.shape}",
            expected_shape=[vocab_size, embed_dim],
            actual_shape=list(table.shape),
        )
    if table.shape != (vocab_size, embed_dim):
        raise business_error(
            E_SHAPE_MISMATCH,
            "table_build",
            job_id,
            (
                f"vocab_size/embed_dim mismatch: expected "
                f"{(vocab_size, embed_dim)}, table has {table.shape}"
            ),
            expected_vocab_size=vocab_size,
            expected_embed_dim=embed_dim,
            actual_shape=list(table.shape),
        )
    return np.ascontiguousarray(table, dtype=np.float64)


def validate_quant_params(
    tier: TierInfo,
    quant_scale: float,
    quant_zero_point: int,
    *,
    job_id: str,
) -> None:
    if tier.kind != "quantized":
        return
    if (
        isinstance(quant_zero_point, bool)
        or not isinstance(quant_zero_point, (int, np.integer))
    ):
        raise business_error(
            E_QUANT_INVALID,
            "table_build",
            job_id,
            "quant_zero_point must be an integer",
            quant_zero_point=quant_zero_point,
        )
    if not np.isfinite(quant_scale) or quant_scale <= 0.0:
        raise business_error(
            E_QUANT_INVALID,
            "table_build",
            job_id,
            (
                "quant_scale must be a positive finite number, "
                f"got {quant_scale!r}"
            ),
            quant_scale=quant_scale,
        )
    assert tier.qmin is not None and tier.qmax is not None
    if not (tier.qmin <= int(quant_zero_point) <= tier.qmax):
        raise business_error(
            E_QUANT_INVALID,
            "table_build",
            job_id,
            (
                f"quant_zero_point must be in [{tier.qmin}, {tier.qmax}], "
                f"got {quant_zero_point}"
            ),
            quant_zero_point=int(quant_zero_point),
            valid_range=[tier.qmin, tier.qmax],
        )


def _numeric_policy(
    values: np.ndarray,
    config: NumericsConfig,
) -> np.ndarray:
    return apply_numeric_policy(
        values,
        config,
        rounding_step=config.dequant_rounding_step,
    )


def dequantize_table(
    storage: np.ndarray,
    *,
    table_dtype: str,
    quant_scale: float,
    quant_zero_point: int,
    config: NumericsConfig,
    job_id: str,
) -> np.ndarray:
    tier = resolve_tier(table_dtype, job_id)
    if tier.kind == "quantized":
        validate_quant_params(tier, quant_scale, quant_zero_point, job_id=job_id)
        raw = (
            np.asarray(storage, dtype=np.float64)
            - float(quant_zero_point)
        ) * float(quant_scale)
    else:
        raw = np.asarray(storage).astype(np.float64)
    return _numeric_policy(raw, config)


def build_quantized_table(
    truth_table: np.ndarray,
    *,
    spec: Any,
    config: NumericsConfig,
) -> QuantizedTable:
    tier = resolve_tier(spec.table_dtype, spec.job_id)
    table = validate_table_shape(
        truth_table,
        vocab_size=spec.vocab_size,
        embed_dim=spec.embed_dim,
        job_id=spec.job_id,
    )
    clipped_values = 0
    if tier.kind == "quantized":
        validate_quant_params(
            tier,
            spec.quant_scale,
            spec.quant_zero_point,
            job_id=spec.job_id,
        )
        assert tier.qmin is not None and tier.qmax is not None
        scaled = table / float(spec.quant_scale) + int(spec.quant_zero_point)
        rounded = round_values(scaled, config.rounding_mode)
        clipped_values = int(
            np.count_nonzero((rounded < tier.qmin) | (rounded > tier.qmax))
        )
        clipped = np.clip(rounded, tier.qmin, tier.qmax)
        storage = np.ascontiguousarray(clipped.astype(tier.numpy_dtype))
        dequantized = dequantize_table(
            storage,
            table_dtype=tier.name,
            quant_scale=spec.quant_scale,
            quant_zero_point=spec.quant_zero_point,
            config=config,
            job_id=spec.job_id,
        )
    else:
        storage = np.ascontiguousarray(table.astype(tier.numpy_dtype))
        dequantized = dequantize_table(
            storage,
            table_dtype=tier.name,
            quant_scale=spec.quant_scale,
            quant_zero_point=spec.quant_zero_point,
            config=config,
            job_id=spec.job_id,
        )
    recomputed = dequantize_table(
        storage,
        table_dtype=tier.name,
        quant_scale=spec.quant_scale,
        quant_zero_point=spec.quant_zero_point,
        config=config,
        job_id=spec.job_id,
    )
    diff = float(np.max(np.abs(dequantized - recomputed))) if table.size else 0.0
    return QuantizedTable(
        table_dtype=tier.name,
        tier=tier,
        storage=storage,
        dequantized=dequantized,
        quant_scale=float(spec.quant_scale),
        quant_zero_point=int(spec.quant_zero_point),
        quant_applicable=tier.kind == "quantized",
        clipped_values=clipped_values,
        storage_digest=array_digest(storage),
        dequantized_digest=array_digest(dequantized),
        independent_recompute_diff=diff,
    )
