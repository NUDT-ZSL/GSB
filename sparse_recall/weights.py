"""Fixed-seed NumPy term weight table generation and validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import (
    DEFAULT_DECIMALS,
    DEFAULT_SUBNORMAL_FLOOR,
    read_json,
    sha256_bytes,
    stable_float,
    write_json_atomic,
)
from .errors import E_INPUT, RecallError


WEIGHT_SCHEMA = "sparse-recall.weights.v1"


@dataclass(frozen=True)
class WeightTable:
    seed: int
    dimension: int
    nnz_per_term: int
    terms: dict[str, tuple[tuple[int, float], ...]]

    @property
    def term_names(self) -> set[str]:
        return set(self.terms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WEIGHT_SCHEMA,
            "seed": self.seed,
            "dimension": self.dimension,
            "nnz_per_term": self.nnz_per_term,
            "terms": {
                term: [[dim, value] for dim, value in self.terms[term]]
                for term in sorted(self.terms)
            },
        }

    @property
    def content_hash(self) -> str:
        from .canonical import canonical_json_bytes

        return sha256_bytes(canonical_json_bytes(self.to_dict()))

    def weight_bytes(self) -> int:
        return self.dimension * 8


def generate_term_weight_table(
    terms: list[str] | tuple[str, ...],
    *,
    dimension: int = 64,
    nnz_per_term: int = 3,
    seed: int = 20261124,
) -> WeightTable:
    if dimension < 1:
        raise RecallError(E_INPUT, "weights", "dimension must be >= 1")
    if not 1 <= nnz_per_term <= dimension:
        raise RecallError(E_INPUT, "weights", "nnz_per_term must be in [1, dimension]")
    if not terms:
        raise RecallError(E_INPUT, "weights", "at least one term is required")
    if len(set(terms)) != len(terms):
        raise RecallError(E_INPUT, "weights", "term list must not contain duplicates")

    rng = np.random.default_rng(seed)
    generated: dict[str, tuple[tuple[int, float], ...]] = {}
    for term in sorted(terms):
        dims = np.sort(rng.choice(dimension, size=nnz_per_term, replace=False))
        values = rng.uniform(-1.0, 1.0, size=nnz_per_term)
        pairs: list[tuple[int, float]] = []
        for dim, value in zip(dims.tolist(), values.tolist()):
            normalized = stable_float(
                value,
                decimals=DEFAULT_DECIMALS,
                subnormal_floor=DEFAULT_SUBNORMAL_FLOOR,
            )
            if normalized == 0.0 or not math.isfinite(normalized):
                normalized = 1.0
            pairs.append((int(dim), normalized))
        generated[term] = tuple(pairs)
    return WeightTable(
        seed=seed,
        dimension=dimension,
        nnz_per_term=nnz_per_term,
        terms=generated,
    )


def save_weight_table(path: str, table: WeightTable) -> str:
    return write_json_atomic(path, table.to_dict())


def load_weight_table(path: str) -> WeightTable:
    try:
        raw = read_json(path)
    except FileNotFoundError as exc:
        raise RecallError(E_INPUT, "weights", f"weight table not found: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != WEIGHT_SCHEMA:
        raise RecallError(E_INPUT, "weights", "unsupported weight-table schema")
    dimension = raw.get("dimension")
    nnz_per_term = raw.get("nnz_per_term")
    seed = raw.get("seed")
    terms_raw = raw.get("terms")
    if not isinstance(dimension, int) or dimension < 1:
        raise RecallError(E_INPUT, "weights", "invalid dimension")
    if not isinstance(nnz_per_term, int) or not 1 <= nnz_per_term <= dimension:
        raise RecallError(E_INPUT, "weights", "invalid nnz_per_term")
    if not isinstance(seed, int):
        raise RecallError(E_INPUT, "weights", "invalid seed")
    if not isinstance(terms_raw, dict) or not terms_raw:
        raise RecallError(E_INPUT, "weights", "terms must be a non-empty object")

    terms: dict[str, tuple[tuple[int, float], ...]] = {}
    for term in sorted(terms_raw):
        entries = terms_raw[term]
        if not isinstance(term, str) or not term or not isinstance(entries, list):
            raise RecallError(E_INPUT, "weights", "invalid term weight entry")
        if len(entries) != nnz_per_term:
            raise RecallError(E_INPUT, "weights", f"term {term!r} has invalid nnz")
        pairs: list[tuple[int, float]] = []
        seen_dims: set[int] = set()
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                raise RecallError(E_INPUT, "weights", f"invalid weight pair for {term!r}")
            dim, value = entry
            if (
                isinstance(dim, bool)
                or not isinstance(dim, int)
                or not 0 <= dim < dimension
                or dim in seen_dims
            ):
                raise RecallError(E_INPUT, "weights", f"invalid dimension for {term!r}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RecallError(E_INPUT, "weights", f"invalid value for {term!r}")
            numeric = float(value)
            if not math.isfinite(numeric):
                raise RecallError(E_INPUT, "weights", f"non-finite value for {term!r}")
            seen_dims.add(dim)
            pairs.append((dim, stable_float(numeric)))
        pairs.sort()
        terms[term] = tuple(pairs)
    return WeightTable(
        seed=seed,
        dimension=dimension,
        nnz_per_term=nnz_per_term,
        terms=terms,
    )
