"""Deterministic CSR construction, stable identifiers and validation."""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import ConfigError, CSRValidationError
from .utils import derived_seed, sha256_hex


@dataclass
class CSRMatrix:
    shape: tuple[int, int]
    indptr: np.ndarray
    indices: np.ndarray
    data: np.ndarray
    nonzero_ids: list[str]
    generation_ordinals: np.ndarray

    @property
    def rows(self) -> int:
        return int(self.shape[0])

    @property
    def cols(self) -> int:
        return int(self.shape[1])

    @property
    def nnz(self) -> int:
        return int(self.indices.size)

    def row_nnz(self) -> np.ndarray:
        return np.diff(self.indptr).astype(np.int64, copy=False)


def _parse_nonnegative_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ConfigError(
            f"{name} must be an integer",
            stage="csr-generation-config",
            details={"field": name, "value": repr(value)},
        )
    result = int(value)
    if result < 0:
        raise ConfigError(
            f"{name} must be non-negative",
            stage="csr-generation-config",
            details={"field": name, "value": result},
        )
    return result


def _generate_row_counts(
    rng: np.random.Generator,
    *,
    rows: int,
    max_nnz: int,
    structure: dict[str, Any],
) -> tuple[np.ndarray, dict[str, Any]]:
    distribution = structure.get("distribution")
    if distribution not in {"uniform", "powerlaw", "extreme"}:
        raise ConfigError(
            "structure.distribution must be uniform, powerlaw or extreme",
            stage="csr-generation-config",
            details={"value": repr(distribution)},
        )

    nnz_min = _parse_nonnegative_int(structure.get("nnz_min", 0), name="nnz_min")
    nnz_max = _parse_nonnegative_int(structure.get("nnz_max", max_nnz), name="nnz_max")
    if nnz_max < nnz_min or nnz_max > max_nnz:
        raise ConfigError(
            "nnz_max must be between nnz_min and the CSR column count",
            stage="csr-generation-config",
            details={"nnz_min": nnz_min, "nnz_max": nnz_max, "K": max_nnz},
        )

    if distribution == "uniform":
        counts = rng.integers(nnz_min, nnz_max + 1, size=rows, dtype=np.int64)
    elif distribution == "powerlaw":
        alpha = float(structure.get("powerlaw_alpha", 1.35))
        scale = float(structure.get("powerlaw_scale", 12.0))
        if alpha <= 0.0 or scale <= 0.0 or not math.isfinite(alpha * scale):
            raise ConfigError(
                "power-law alpha and scale must be positive finite values",
                stage="csr-generation-config",
                details={"alpha": alpha, "scale": scale},
            )
        raw = nnz_min + np.floor(rng.pareto(alpha, size=rows) * scale)
        counts = np.clip(raw, nnz_min, nnz_max).astype(np.int64, copy=False)
    else:
        zero_rows = min(rows, _parse_nonnegative_int(structure.get("zero_rows", 0), name="zero_rows"))
        hot_rows = min(rows - zero_rows, _parse_nonnegative_int(structure.get("hot_rows", 0), name="hot_rows"))
        medium_fraction = float(structure.get("medium_fraction", 0.0))
        if not 0.0 <= medium_fraction <= 1.0:
            raise ConfigError(
                "medium_fraction must be in [0, 1]",
                stage="csr-generation-config",
                details={"value": medium_fraction},
            )
        medium_rows = min(
            rows - zero_rows - hot_rows,
            int(round((rows - zero_rows - hot_rows) * medium_fraction)),
        )
        hot_min = _parse_nonnegative_int(structure.get("hot_nnz_min", nnz_max), name="hot_nnz_min")
        hot_max = _parse_nonnegative_int(structure.get("hot_nnz_max", nnz_max), name="hot_nnz_max")
        medium_min = _parse_nonnegative_int(structure.get("medium_nnz_min", nnz_min), name="medium_nnz_min")
        medium_max = _parse_nonnegative_int(structure.get("medium_nnz_max", nnz_max), name="medium_nnz_max")
        if not (nnz_min <= hot_min <= hot_max <= nnz_max):
            raise ConfigError(
                "extreme hot nnz bounds must lie inside [nnz_min, nnz_max]",
                stage="csr-generation-config",
                details={"hot_min": hot_min, "hot_max": hot_max},
            )
        if not (nnz_min <= medium_min <= medium_max <= nnz_max):
            raise ConfigError(
                "extreme medium nnz bounds must lie inside [nnz_min, nnz_max]",
                stage="csr-generation-config",
                details={"medium_min": medium_min, "medium_max": medium_max},
            )
        permutation = rng.permutation(rows)
        counts = np.full(rows, nnz_min, dtype=np.int64)
        zero_idx = permutation[:zero_rows]
        hot_idx = permutation[zero_rows : zero_rows + hot_rows]
        medium_idx = permutation[zero_rows + hot_rows : zero_rows + hot_rows + medium_rows]
        counts[zero_idx] = 0
        if hot_rows:
            counts[hot_idx] = rng.integers(hot_min, hot_max + 1, size=hot_rows, dtype=np.int64)
        if medium_rows:
            counts[medium_idx] = rng.integers(
                medium_min, medium_max + 1, size=medium_rows, dtype=np.int64
            )

    metadata = {
        "distribution": distribution,
        "nnz_min": int(counts.min()) if counts.size else 0,
        "nnz_max": int(counts.max()) if counts.size else 0,
        "nnz_sum": int(counts.sum()),
        "nnz_mean": float(counts.mean()) if counts.size else 0.0,
        "zero_row_count": int(np.count_nonzero(counts == 0)),
    }
    return counts, metadata


def _generate_values(
    rng: np.random.Generator,
    size: int,
    values_config: dict[str, Any],
) -> np.ndarray:
    mode = values_config.get("mode", "log_uniform")
    if mode != "log_uniform":
        raise ConfigError(
            "only values.mode=log_uniform is supported for deterministic local samples",
            stage="csr-generation-config",
            details={"mode": repr(mode)},
        )
    exp_min = float(values_config.get("exponent_min", -2.0))
    exp_max = float(values_config.get("exponent_max", 2.0))
    negative_ratio = float(values_config.get("negative_ratio", 0.5))
    scale = float(values_config.get("scale", 1.0))
    if exp_max < exp_min:
        raise ConfigError(
            "values.exponent_max must be >= exponent_min",
            stage="csr-generation-config",
            details={"exponent_min": exp_min, "exponent_max": exp_max},
        )
    if not 0.0 <= negative_ratio <= 1.0:
        raise ConfigError(
            "values.negative_ratio must be in [0, 1]",
            stage="csr-generation-config",
            details={"negative_ratio": negative_ratio},
        )
    exponents = rng.uniform(exp_min, exp_max, size=size)
    signs = np.where(rng.random(size=size) < negative_ratio, -1.0, 1.0)
    values = scale * signs * np.power(10.0, exponents)
    return values.astype(np.float32)


def generate_csr(config: dict[str, Any]) -> tuple[CSRMatrix, dict[str, Any]]:
    """Generate a deterministic CSR matrix from a shape/seed description."""

    shape_config = config.get("shape", config)
    rows = _parse_nonnegative_int(shape_config.get("M", shape_config.get("rows")), name="M")
    cols = _parse_nonnegative_int(shape_config.get("K", shape_config.get("cols")), name="K")
    if rows == 0 or cols == 0:
        raise ConfigError(
            "CSR dimensions M and K must be positive",
            stage="csr-generation-config",
            details={"M": rows, "K": cols},
        )
    seed = _parse_nonnegative_int(config.get("seed"), name="seed")
    structure = config.get("structure", {})
    values_config = config.get("values", {})
    if not isinstance(structure, dict) or not isinstance(values_config, dict):
        raise ConfigError(
            "structure and values must be objects",
            stage="csr-generation-config",
        )

    rng = np.random.default_rng(derived_seed(seed, "csr-structure"))
    counts, count_metadata = _generate_row_counts(
        rng, rows=rows, max_nnz=cols, structure=structure
    )
    nnz = int(counts.sum())
    indptr = np.empty(rows + 1, dtype=np.int64)
    indptr[0] = 0
    np.cumsum(counts, out=indptr[1:])
    indices = np.empty(nnz, dtype=np.int32)
    for row, count in enumerate(counts.tolist()):
        if count == 0:
            continue
        start = int(indptr[row])
        selected = rng.choice(cols, size=int(count), replace=False).astype(np.int32)
        selected.sort()
        indices[start : start + count] = selected

    value_rng = np.random.default_rng(derived_seed(seed, "csr-values"))
    data = _generate_values(value_rng, nnz, values_config)
    ordinals = np.arange(nnz, dtype=np.int64)
    ids = [
        f"nz-r{row:06d}-c{int(col):06d}-g{int(ordinal):09d}"
        for row in range(rows)
        for col, ordinal in zip(
            indices[int(indptr[row]) : int(indptr[row + 1])],
            ordinals[int(indptr[row]) : int(indptr[row + 1])],
        )
    ]
    csr = CSRMatrix(
        shape=(rows, cols),
        indptr=indptr,
        indices=indices,
        data=data,
        nonzero_ids=ids,
        generation_ordinals=ordinals,
    )
    validate_csr(csr, stage="csr-generation-validation")
    metadata = {
        **count_metadata,
        "seed": seed,
        "structure_fingerprint": structure_fingerprint(csr),
        "data_fingerprint": data_fingerprint(csr),
        "row_nnz": counts.tolist(),
        "nonzero_ids": ids,
        "indptr": indptr.astype(int).tolist(),
        "indices": indices.astype(int).tolist(),
    }
    return csr, metadata


def validate_csr(csr: CSRMatrix, *, stage: str = "csr-validation") -> None:
    rows, cols = csr.shape
    if (
        isinstance(rows, bool)
        or isinstance(cols, bool)
        or not isinstance(rows, (int, np.integer))
        or not isinstance(cols, (int, np.integer))
    ):
        raise CSRValidationError(
            "CSR M and K must be integers",
            stage=stage,
            shape={"M": repr(rows), "K": repr(cols), "nnz": int(csr.indices.size)},
        )
    shape = {"M": int(rows), "K": int(cols), "nnz": int(csr.indices.size)}
    if rows <= 0 or cols <= 0:
        raise CSRValidationError(
            "CSR M and K must be positive", stage=stage, shape=shape
        )
    if not np.issubdtype(csr.indptr.dtype, np.integer):
        raise CSRValidationError(
            "indptr must use an integer dtype",
            stage=stage,
            shape=shape,
            details={"dtype": str(csr.indptr.dtype)},
        )
    if not np.issubdtype(csr.indices.dtype, np.integer):
        raise CSRValidationError(
            "indices must use an integer dtype",
            stage=stage,
            shape=shape,
            details={"dtype": str(csr.indices.dtype)},
        )
    if not np.issubdtype(csr.data.dtype, np.floating):
        raise CSRValidationError(
            "data must use a floating dtype",
            stage=stage,
            shape=shape,
            details={"dtype": str(csr.data.dtype)},
        )
    if csr.indptr.ndim != 1 or csr.indptr.size != rows + 1:
        raise CSRValidationError(
            "indptr length must equal M+1",
            stage=stage,
            shape=shape,
            details={"indptr_length": int(csr.indptr.size), "expected": rows + 1},
        )
    if csr.indices.ndim != 1 or csr.data.ndim != 1:
        raise CSRValidationError(
            "indices and data must be one-dimensional", stage=stage, shape=shape
        )
    if csr.indices.size != csr.data.size:
        raise CSRValidationError(
            "indices and data lengths differ",
            stage=stage,
            shape=shape,
            details={"indices": int(csr.indices.size), "data": int(csr.data.size)},
        )
    if int(csr.indptr[0]) != 0:
        raise CSRValidationError(
            "indptr[0] must be zero", stage=stage, shape=shape
        )
    if int(csr.indptr[-1]) != int(csr.indices.size):
        raise CSRValidationError(
            "indptr[-1] must equal the nonzero count",
            stage=stage,
            shape=shape,
            details={"indptr_last": int(csr.indptr[-1]), "nnz": int(csr.indices.size)},
        )
    if np.any(csr.indptr < 0) or np.any(csr.indptr > csr.indices.size):
        raise CSRValidationError(
            "indptr values must lie in [0, nnz]",
            stage=stage,
            shape=shape,
        )
    if np.any(np.diff(csr.indptr) < 0):
        raise CSRValidationError(
            "indptr must be non-decreasing",
            stage=stage,
            shape=shape,
            details={"indptr": csr.indptr.astype(int).tolist()},
        )
    if np.any(csr.indices < 0) or np.any(csr.indices >= cols):
        bad = int(np.flatnonzero((csr.indices < 0) | (csr.indices >= cols))[0])
        raise CSRValidationError(
            "column index outside [0, K)",
            stage=stage,
            shape=shape,
            details={"position": bad, "column": int(csr.indices[bad]), "K": int(cols)},
        )
    for row in range(rows):
        start = int(csr.indptr[row])
        end = int(csr.indptr[row + 1])
        segment = csr.indices[start:end]
        if segment.size > 1 and np.any(np.diff(segment) <= 0):
            raise CSRValidationError(
                "column indices must be strictly increasing in every row",
                stage=stage,
                shape=shape,
                details={"row": row, "start": start, "end": end},
            )
    if len(csr.nonzero_ids) != csr.indices.size:
        raise CSRValidationError(
            "stable nonzero identifier count does not match nnz",
            stage=stage,
            shape=shape,
            details={"ids": len(csr.nonzero_ids), "nnz": int(csr.indices.size)},
        )
    if len(set(csr.nonzero_ids)) != len(csr.nonzero_ids):
        raise CSRValidationError(
            "stable nonzero identifiers must be unique", stage=stage, shape=shape
        )
    if csr.generation_ordinals.size != csr.indices.size:
        raise CSRValidationError(
            "generation ordinal count does not match nnz", stage=stage, shape=shape
        )


def structure_fingerprint(csr: CSRMatrix) -> str:
    digest = hashlib.sha256()
    digest.update(struct.pack("<QQ", int(csr.shape[0]), int(csr.shape[1])))
    digest.update(struct.pack("<Q", int(csr.indices.size)))
    digest.update(np.ascontiguousarray(csr.indptr, dtype="<i8").tobytes())
    digest.update(np.ascontiguousarray(csr.indices, dtype="<i4").tobytes())
    return digest.hexdigest()


def data_fingerprint(csr: CSRMatrix) -> str:
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(csr.data, dtype="<f4").tobytes())
    digest.update("|".join(csr.nonzero_ids).encode("utf-8"))
    return digest.hexdigest()


def nonzero_id(row: int, col: int, ordinal: int) -> str:
    return f"nz-r{int(row):06d}-c{int(col):06d}-g{int(ordinal):09d}"


def reassign_ids(csr: CSRMatrix) -> None:
    """Rebuild identifiers after a controlled structural mutation."""

    ordinals = np.arange(csr.indices.size, dtype=np.int64)
    csr.generation_ordinals = ordinals
    csr.nonzero_ids = [
        nonzero_id(row, int(csr.indices[p]), int(ordinals[p]))
        for row in range(csr.rows)
        for p in range(int(csr.indptr[row]), int(csr.indptr[row + 1]))
    ]
