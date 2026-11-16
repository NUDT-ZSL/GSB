"""Categorical feature cross combination with budget bounded degradation."""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np

from .config import Config, ScaleSpec
from .data import MISSING_TOKEN, SyntheticDataset
from .hashing import digest_iterable, hash_uint64

KEEP_STRATEGY = "kept"
HASH_STRATEGY = "hash_bucket"
SKIP_STRATEGY = "skip"


@dataclass
class CrossColumn:
    name: str
    members: list[str]
    raw_cardinality: int
    final_cardinality: int
    strategy: str
    codes: np.ndarray                 # integer codes in [0, final_cardinality)
    vocabulary: list[str] | None      # present only when kept (collision free)
    budget: int


@dataclass
class CrossResult:
    scale_id: str
    cross_columns: list[CrossColumn]
    audit: list[dict]
    total_raw_cardinality: int
    total_final_cardinality: int
    combiner_fingerprint: str


def _cross_value(row: tuple) -> str:
    return "||".join("" if value is None else str(value) for value in row)


def build_crosses(config: Config, scale: ScaleSpec,
                  dataset: SyntheticDataset) -> CrossResult:
    whitelist = [name for name in config.cross.whitelist
                 if name in dataset.categorical_names]
    available = set(whitelist)
    cross_columns: list[CrossColumn] = []
    audit: list[dict] = []
    total_raw = 0
    total_final = 0

    orders = range(config.cross.min_order, config.cross.max_order + 1)
    for order in orders:
        for members in combinations(sorted(available), order):
            indices = [dataset.categorical_names.index(name) for name in members]
            block = dataset.categorical[:, indices]
            joined = np.array([_cross_value(tuple(row)) for row in block], dtype=object)
            joined[np.any(block == MISSING_TOKEN, axis=1)] = MISSING_TOKEN
            unique_values = sorted({value for value in joined.tolist()
                                    if value != MISSING_TOKEN})
            raw_cardinality = len(unique_values)
            total_raw += raw_cardinality
            budget = config.cross.cardinality_budget
            name = "x__" + "__".join(members)
            if raw_cardinality <= budget:
                vocabulary = unique_values
                lookup = {value: idx for idx, value in enumerate(vocabulary)}
                codes = np.array([lookup.get(value, -1) for value in joined],
                                 dtype=np.int64)
                strategy = KEEP_STRATEGY
                final_cardinality = raw_cardinality
                vocabulary_out = vocabulary
            elif config.cross.degrade_strategy == "skip":
                audit.append({
                    "cross": name,
                    "members": list(members),
                    "order": order,
                    "raw_cardinality": raw_cardinality,
                    "final_cardinality": 0,
                    "budget": budget,
                    "strategy": SKIP_STRATEGY,
                    "reason": "raw_cardinality exceeds budget; combination skipped",
                })
                continue
            else:
                final_cardinality = budget
                hashed = np.array([
                    -1 if value == MISSING_TOKEN
                    else hash_uint64(f"{name}={value}") % final_cardinality
                    for value in joined
                ], dtype=np.int64)
                codes = hashed
                strategy = HASH_STRATEGY
                vocabulary_out = None
            total_final += final_cardinality
            cross_columns.append(CrossColumn(
                name=name, members=list(members), raw_cardinality=raw_cardinality,
                final_cardinality=final_cardinality, strategy=strategy,
                codes=codes, vocabulary=vocabulary_out, budget=budget,
            ))
            audit.append({
                "cross": name,
                "members": list(members),
                "order": order,
                "raw_cardinality": raw_cardinality,
                "final_cardinality": final_cardinality,
                "budget": budget,
                "strategy": strategy,
                "reason": "within budget" if strategy == KEEP_STRATEGY
                else "raw_cardinality exceeds budget; truncated to hash buckets",
            })

    combiner_fingerprint = digest_iterable([
        config.config_fingerprint, scale.id, dataset.generation_fingerprint,
        config.cross.__dict__,
        [(item.name, item.members, item.raw_cardinality,
          item.final_cardinality, item.strategy) for item in cross_columns],
    ])
    return CrossResult(
        scale_id=scale.id, cross_columns=cross_columns, audit=audit,
        total_raw_cardinality=total_raw, total_final_cardinality=total_final,
        combiner_fingerprint=combiner_fingerprint,
    )
