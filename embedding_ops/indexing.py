"""Deterministic index-set generation and validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import (
    E_INDEX_EMPTY,
    E_INDEX_INVALID,
    E_INDEX_OUT_OF_RANGE,
    E_MANIFEST_INVALID,
    E_PAD_INDEX_INVALID,
    business_error,
)
from .util import array_digest, sha256_json


@dataclass(frozen=True)
class IndexValidation:
    indices: np.ndarray
    pad_mask: np.ndarray
    pad_positions: list[list[int]]
    token_positions: list[int]
    mask_digest: str
    index_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": list(self.indices.shape),
            "token_positions": self.token_positions,
            "pad_positions": self.pad_positions,
            "mask_digest": self.mask_digest,
            "index_digest": self.index_digest,
        }


def _explicit_values(description: Any, job_id: str) -> np.ndarray:
    if isinstance(description, dict):
        value = description.get(
            "values",
            description.get("indices", description.get("data")),
        )
        if value is None:
            raise business_error(
                E_MANIFEST_INVALID,
                "index_validation",
                job_id,
                "explicit index_set requires values/indices/data",
            )
    else:
        value = description
    return np.asarray(value)


def _parse_position(
    value: Any,
    shape: tuple[int, int],
    field: str,
    job_id: str,
) -> tuple[int, int]:
    try:
        position = [int(value[0]), int(value[1])]
    except (TypeError, ValueError, IndexError) as exc:
        raise business_error(
            E_MANIFEST_INVALID,
            "index_validation",
            job_id,
            f"{field} entries must be [token, hit] pairs",
            value=value,
        ) from exc
    if not (0 <= position[0] < shape[0] and 0 <= position[1] < shape[1]):
        raise business_error(
            E_MANIFEST_INVALID,
            "index_validation",
            job_id,
            f"{field} position is outside generated index shape",
            position=position,
            shape=list(shape),
        )
    return position[0], position[1]


def generate_index_array(
    description: Any,
    *,
    vocab_size: int,
    pad_index: int | None,
    seed: int,
    job_id: str,
) -> np.ndarray:
    """Generate a ``(tokens, hits)`` index array from a description."""

    if not isinstance(description, dict):
        return _explicit_values(description, job_id)
    kind = description.get("kind", "explicit")
    if kind == "explicit":
        return _explicit_values(description, job_id)
    if kind not in ("generated", "random"):
        raise business_error(
            E_MANIFEST_INVALID,
            "index_validation",
            job_id,
            f"unknown index_set kind: {kind!r}",
            known_kinds=["explicit", "generated", "random"],
        )
    num_tokens = description.get("num_tokens")
    hits_per_token = description.get("hits_per_token", 1)
    if isinstance(num_tokens, bool) or not isinstance(num_tokens, int):
        raise business_error(
            E_MANIFEST_INVALID,
            "index_validation",
            job_id,
            "generated index_set requires integer num_tokens",
        )
    if isinstance(hits_per_token, bool) or not isinstance(hits_per_token, int):
        raise business_error(
            E_MANIFEST_INVALID,
            "index_validation",
            job_id,
            "hits_per_token must be an integer",
        )
    if num_tokens <= 0:
        raise business_error(
            E_INDEX_EMPTY,
            "index_validation",
            job_id,
            "generated index_set has no token positions",
        )
    if hits_per_token <= 0:
        raise business_error(
            E_INDEX_EMPTY,
            "index_validation",
            job_id,
            "hits_per_token must be positive",
        )
    low = description.get("low", 0)
    high = description.get("high", vocab_size)
    if (
        isinstance(low, bool)
        or isinstance(high, bool)
        or not isinstance(low, int)
        or not isinstance(high, int)
        or low < 0
        or high > vocab_size
        or low >= high
    ):
        raise business_error(
            E_MANIFEST_INVALID,
            "index_validation",
            job_id,
            "generated index bounds must satisfy 0 <= low < high <= vocab_size",
            low=low,
            high=high,
            vocab_size=vocab_size,
        )
    rng = np.random.default_rng(np.random.SeedSequence([seed, 1]))
    indices = rng.integers(low, high, size=(num_tokens, hits_per_token))
    shape = (num_tokens, hits_per_token)

    for entry in description.get("duplicate_of", []):
        if not isinstance(entry, dict) or "at" not in entry or "source" not in entry:
            raise business_error(
                E_MANIFEST_INVALID,
                "index_validation",
                job_id,
                "duplicate_of entries require at and source pairs",
                entry=entry,
            )
        at = _parse_position(entry["at"], shape, "duplicate_of.at", job_id)
        source = _parse_position(
            entry["source"], shape, "duplicate_of.source", job_id
        )
        indices[at] = indices[source]

    for entry in description.get("duplicate_positions", []):
        at = _parse_position(entry, shape, "duplicate_positions", job_id)
        if at[1] > 0:
            indices[at] = indices[at[0], at[1] - 1]
        elif at[0] > 0:
            indices[at] = indices[at[0] - 1, hits_per_token - 1]
        else:
            raise business_error(
                E_MANIFEST_INVALID,
                "index_validation",
                job_id,
                "first generated position cannot duplicate a previous position",
                position=list(at),
            )

    for entry in description.get("pad_positions", []):
        at = _parse_position(entry, shape, "pad_positions", job_id)
        if pad_index is None:
            raise business_error(
                E_PAD_INDEX_INVALID,
                "index_validation",
                job_id,
                "pad_positions require a non-negative pad_index",
                position=list(at),
            )
        indices[at] = pad_index
    return indices


def validate_indices(
    indices: np.ndarray,
    *,
    vocab_size: int,
    pad_index: int | None,
    job_id: str,
) -> IndexValidation:
    array = np.asarray(indices)
    if array.size == 0:
        raise business_error(
            E_INDEX_EMPTY,
            "index_validation",
            job_id,
            "index array is empty",
        )
    if array.dtype.kind not in ("i", "u"):
        raise business_error(
            E_INDEX_INVALID,
            "index_validation",
            job_id,
            "index array must contain integers",
            dtype=str(array.dtype),
        )
    if array.ndim == 1:
        array = array.reshape(array.shape[0], 1)
    if array.ndim != 2 or array.shape[0] == 0 or array.shape[1] == 0:
        raise business_error(
            E_INDEX_INVALID,
            "index_validation",
            job_id,
            "index array must be non-empty and two-dimensional after parsing",
            shape=list(array.shape),
        )
    normalized = np.ascontiguousarray(array, dtype=np.int64)
    if pad_index is not None and (pad_index < 0 or pad_index >= vocab_size):
        raise business_error(
            E_PAD_INDEX_INVALID,
            "index_validation",
            job_id,
            f"pad_index {pad_index} out of range [0, {vocab_size})",
            pad_index=pad_index,
            lower=0,
            upper=vocab_size,
        )
    bad_mask = (normalized < 0) | (normalized >= vocab_size)
    bad_flat = np.flatnonzero(bad_mask.reshape(-1))
    if bad_flat.size:
        first_flat = int(bad_flat[0])
        multi = np.unravel_index(first_flat, normalized.shape)
        first_position = [int(multi[0]), int(multi[1])]
        raise business_error(
            E_INDEX_OUT_OF_RANGE,
            "index_validation",
            job_id,
            (
                f"index {int(normalized[multi])} at position {first_position} "
                f"out of range [0, {vocab_size})"
            ),
            first_position=first_flat,
            position=first_position,
            value=int(normalized[multi]),
            lower=0,
            upper=vocab_size,
        )
    if pad_index is not None:
        pad_mask = normalized == pad_index
    else:
        pad_mask = np.zeros(normalized.shape, dtype=bool)
    pad_positions = [
        [int(token), int(hit)]
        for token, hit in np.argwhere(pad_mask).tolist()
    ]
    mask_digest = sha256_json(
        {
            "job_id": job_id,
            "shape": list(normalized.shape),
            "indices": normalized.tolist(),
            "pad_mask": pad_mask.tolist(),
        }
    )
    return IndexValidation(
        indices=normalized,
        pad_mask=np.ascontiguousarray(pad_mask),
        pad_positions=pad_positions,
        token_positions=list(range(normalized.shape[0])),
        mask_digest=mask_digest,
        index_digest=array_digest(normalized),
    )
