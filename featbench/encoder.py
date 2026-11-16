"""Deterministic encoding of base columns and cross columns into a numeric matrix."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .combiner import CrossResult
from .config import Config
from .data import MISSING_TOKEN, SyntheticDataset
from .errors import BenchError, E_MISSING_UNFILLED
from .hashing import digest_array, digest_iterable


@dataclass
class EncodedMatrix:
    scale_id: str
    matrix: np.ndarray
    feature_names: list[str]
    constant_features: list[str]
    missing_cells: int
    encoder_fingerprint: str
    imputed: bool


def encode_matrix(config: Config, dataset: SyntheticDataset,
                  crosses: CrossResult, impute: bool) -> EncodedMatrix:
    target = config.target_column
    blocks: list[np.ndarray] = []
    feature_names: list[str] = []
    constant_features: list[str] = []
    missing_cells = 0

    numeric_keep = [idx for idx, name in enumerate(dataset.numeric_names)
                    if name != target]
    if numeric_keep:
        numeric_block = dataset.numeric[:, numeric_keep].copy()
        numeric_names = [dataset.numeric_names[idx] for idx in numeric_keep]
        missing_cells += int(np.isnan(numeric_block).sum())
        for idx, name in enumerate(numeric_names):
            if np.nanstd(numeric_block[:, idx]) == 0.0:
                constant_features.append(name)
        blocks.append(numeric_block)
        feature_names.extend(numeric_names)

    cat_keep = [idx for idx, name in enumerate(dataset.categorical_names)
                if name != target]
    for idx in cat_keep:
        name = dataset.categorical_names[idx]
        col = dataset.categorical[:, idx]
        levels = sorted({value for value in col.tolist() if value != MISSING_TOKEN})
        vocab = {level: position for position, level in enumerate(levels)}
        present_missing = bool(np.any(col == MISSING_TOKEN))
        width = len(levels) + (1 if present_missing else 1)
        onehot = np.zeros((dataset.samples, width), dtype=np.float64)
        for row_idx, value in enumerate(col):
            if value == MISSING_TOKEN:
                missing_cells += 1
            else:
                onehot[row_idx, vocab[value]] = 1.0
        onehot[:, -1] = 1.0 if not present_missing else (col == MISSING_TOKEN).astype(float)
        for level_idx, level in enumerate(levels):
            feature_names.append(f"onehot__{name}__{level}")
            if np.std(onehot[:, level_idx]) == 0.0:
                constant_features.append(feature_names[-1])
        feature_names.append(f"missing__{name}")
        if np.std(onehot[:, -1]) == 0.0:
            constant_features.append(feature_names[-1])
        blocks.append(onehot)

    for cross in crosses.cross_columns:
        width = cross.final_cardinality + 1
        onehot = np.zeros((dataset.samples, width), dtype=np.float64)
        observed = cross.codes >= 0
        onehot[np.arange(dataset.samples)[observed], cross.codes[observed]] = 1.0
        onehot[:, -1] = (~observed).astype(float)
        missing_cells += int((~observed).sum())
        for code in range(cross.final_cardinality):
            feature_names.append(f"{cross.name}__b{code}")
            if np.std(onehot[:, code]) == 0.0:
                constant_features.append(feature_names[-1])
        feature_names.append(f"missing__{cross.name}")
        if np.std(onehot[:, -1]) == 0.0:
            constant_features.append(feature_names[-1])
        blocks.append(onehot)

    matrix = np.hstack(blocks) if blocks else \
        np.zeros((dataset.samples, 0), dtype=np.float64)
    assert matrix.shape[1] == len(feature_names)

    imputed = False
    if missing_cells:
        if not impute:
            raise BenchError(
                E_MISSING_UNFILLED,
                f"scale '{dataset.scale_id}': {missing_cells} missing cells remain "
                "and impute_missing is false",
            )
        matrix = _impute(matrix)
        imputed = True

    encoder_fingerprint = digest_iterable([
        config.config_fingerprint, dataset.generation_fingerprint,
        crosses.combiner_fingerprint, {"impute": impute},
        digest_array(matrix), feature_names,
    ])
    return EncodedMatrix(
        scale_id=dataset.scale_id, matrix=matrix, feature_names=feature_names,
        constant_features=constant_features, missing_cells=missing_cells,
        encoder_fingerprint=encoder_fingerprint, imputed=imputed,
    )


def _impute(matrix: np.ndarray) -> np.ndarray:
    if matrix.shape[1] == 0:
        return matrix
    out = matrix.copy()
    nan_mask = np.isnan(out)
    if not nan_mask.any():
        return out
    column_means = np.nanmean(out, axis=0)
    column_means = np.where(np.isnan(column_means), 0.0, column_means)
    index = np.where(nan_mask)
    out[index] = np.take(column_means, index[1])
    return out
