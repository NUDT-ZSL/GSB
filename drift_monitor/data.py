"""Deterministic local feature-batch stream generator.

Every batch is produced by an independent NumPy ``default_rng`` seeded from a
single master seed and the batch id, so batches are reproducible in isolation
and independent of generation order.  No network or external data source is
involved.  Drift injections are explicit, deterministic and limited to the
batch ids supplied by the caller.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .errors import DriftError, ErrorCode
from .hashing import content_hash

# Fixed schema of the synthetic stream.  The ordering here is the canonical
# column ordering used everywhere downstream.
NUMERIC_FEATURES: Tuple[str, ...] = ("income_score", "session_length", "browse_depth", "cart_value")
CATEGORICAL_FEATURES: Tuple[str, ...] = ("device_class", "region", "membership_tier")

BASE_MEANS = (100.0, 20.0, 5.0, 40.0)
BASE_STDS = (10.0, 4.0, 1.5, 8.0)

# Baseline categorical probability vectors (rows sum to 1).
CATEGORY_LEVELS: Dict[str, List[str]] = {
    "device_class": ["desktop", "mobile", "tablet"],
    "region": ["north", "south", "east", "west"],
    "membership_tier": ["free", "silver", "gold"],
}
BASE_CATEGORY_PROBS: Dict[str, List[float]] = {
    "device_class": [0.45, 0.40, 0.15],
    "region": [0.25, 0.25, 0.30, 0.20],
    "membership_tier": [0.60, 0.30, 0.10],
}

# Drifted category probability vectors used when proportion drift is injected.
DRIFT_CATEGORY_PROBS: Dict[str, List[float]] = {
    "device_class": [0.15, 0.65, 0.20],
    "region": [0.45, 0.10, 0.35, 0.10],
    "membership_tier": [0.25, 0.35, 0.40],
}

MISSING_SENTINEL = None  # encoded as JSON null; pandas-free by design.
ILLEGAL_MARKERS = frozenset({"NaN", "nan", "INF", "inf", "-INF", "-inf", "null_string"})


@dataclass
class DriftInjection:
    batch_id: int
    mean_shift: float = 0.0
    variance_multiplier: float = 1.0
    category_shift: bool = False
    missing_rate: float = 0.0


@dataclass
class Batch:
    batch_id: int
    sample_count: int
    columns: Dict[str, List[Any]]
    drift_injection: Optional[Dict[str, Any]] = None

    def fingerprint(self) -> str:
        return content_hash(
            {
                "batch_id": self.batch_id,
                "sample_count": self.sample_count,
                "columns": {name: self.columns[name] for name in sorted(self.columns)},
            }
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sample_count": self.sample_count,
            "drift_injection": self.drift_injection,
            "columns": {name: self.columns[name] for name in sorted(self.columns)},
            "fingerprint": self.fingerprint(),
        }


def _rng(master_seed: int, batch_id: int, stream: str) -> np.random.Generator:
    # Mix a stable stream label into the seed so that numeric draws, categorical
    # draws and missing masks never consume the same random sequence.
    stream_hash = int.from_bytes(
        content_hash({"seed": master_seed, "batch": batch_id, "stream": stream}).encode("utf-8")[:8],
        "big",
    )
    return np.random.default_rng(stream_hash)


def generate_batch(
    batch_id: int,
    sample_count: int = 200,
    master_seed: int = 20260101,
    injection: Optional[DriftInjection] = None,
) -> Batch:
    if sample_count <= 0:
        raise DriftError(
            ErrorCode.EMPTY_BATCH,
            "a batch must contain at least one sample",
            {"batch_id": batch_id, "field": "sample_count"},
        )

    numeric_rng = _rng(master_seed, batch_id, "numeric")
    category_rng = _rng(master_seed, batch_id, "category")
    missing_rng = _rng(master_seed, batch_id, "missing")

    columns: Dict[str, List[Any]] = {}
    for index, name in enumerate(NUMERIC_FEATURES):
        mean = BASE_MEANS[index] + (injection.mean_shift if injection else 0.0)
        std = BASE_STDS[index] * (
            math.sqrt(injection.variance_multiplier) if injection else 1.0
        )
        draws = numeric_rng.normal(loc=mean, scale=std, size=sample_count)
        columns[name] = [float(x) for x in draws]

    for name in CATEGORICAL_FEATURES:
        levels = CATEGORY_LEVELS[name]
        probs = (
            DRIFT_CATEGORY_PROBS[name]
            if injection and injection.category_shift
            else BASE_CATEGORY_PROBS[name]
        )
        indices = category_rng.choice(len(levels), size=sample_count, p=probs)
        columns[name] = [levels[int(i)] for i in indices]

    if injection and injection.missing_rate > 0.0:
        rate = injection.missing_rate
        for name in (*NUMERIC_FEATURES, *CATEGORICAL_FEATURES):
            mask = missing_rng.random(sample_count) < rate
            values = columns[name]
            columns[name] = [MISSING_SENTINEL if flag else value for flag, value in zip(mask, values)]

    payload = None
    if injection is not None:
        payload = {
            "mean_shift": injection.mean_shift,
            "variance_multiplier": injection.variance_multiplier,
            "category_shift": injection.category_shift,
            "missing_rate": injection.missing_rate,
        }
    return Batch(batch_id=batch_id, sample_count=sample_count, columns=columns, drift_injection=payload)


def generate_stream(
    batch_count: int,
    sample_count: int = 200,
    master_seed: int = 20260101,
    injections: Optional[Sequence[DriftInjection]] = None,
) -> List[Batch]:
    by_id = {item.batch_id: item for item in (injections or [])}
    return [
        generate_batch(
            batch_id=batch_id,
            sample_count=sample_count,
            master_seed=master_seed,
            injection=by_id.get(batch_id),
        )
        for batch_id in range(batch_count)
    ]


def write_batches(batches: Sequence[Batch], path: str) -> None:
    document = {
        "schema_version": "batch-stream-v1",
        "numeric_features": list(NUMERIC_FEATURES),
        "categorical_features": list(CATEGORICAL_FEATURES),
        "batches": [batch.to_dict() for batch in batches],
    }
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, sort_keys=True, indent=2, ensure_ascii=False)
        handle.write("\n")


def read_batches(path: str) -> List[Batch]:
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    batches: List[Batch] = []
    for raw in document["batches"]:
        batch = Batch(
            batch_id=int(raw["batch_id"]),
            sample_count=int(raw["sample_count"]),
            columns={name: list(values) for name, values in raw["columns"].items()},
            drift_injection=raw.get("drift_injection"),
        )
        batches.append(batch)
    return batches


def validate_batch(batch: Batch, config_features: Dict[str, str]) -> None:
    """Apply the structured ingestion checks required by the business rules."""

    if batch.sample_count <= 0 or not any(batch.columns.values()):
        raise DriftError(
            ErrorCode.EMPTY_BATCH,
            "batch contains zero samples",
            {"batch_id": batch.batch_id},
        )
    for feature, column_type in sorted(config_features.items()):
        if feature not in batch.columns:
            raise DriftError(
                ErrorCode.ILLEGAL_VALUE,
                "configured feature is absent from the batch",
                {"batch_id": batch.batch_id, "feature": feature},
            )
        values = batch.columns[feature]
        for position, value in enumerate(values):
            if isinstance(value, str) and value in ILLEGAL_MARKERS:
                raise DriftError(
                    ErrorCode.ILLEGAL_VALUE,
                    f"illegal sentinel value {value!r} encountered",
                    {
                        "batch_id": batch.batch_id,
                        "feature": feature,
                        "row": position,
                        "field": "value",
                    },
                )
            if column_type == "numeric" and value is not None and not isinstance(value, (int, float)):
                raise DriftError(
                    ErrorCode.ILLEGAL_VALUE,
                    "numeric column contains a non-numeric, non-null value",
                    {"batch_id": batch.batch_id, "feature": feature, "row": position, "field": "value"},
                )
            if column_type == "categorical" and value is not None and not isinstance(value, str):
                raise DriftError(
                    ErrorCode.ILLEGAL_VALUE,
                    "categorical column contains a non-string, non-null value",
                    {"batch_id": batch.batch_id, "feature": feature, "row": position, "field": "value"},
                )


def ensure_runtime_dirs(base_dir: str) -> Dict[str, str]:
    subdirs = ("cache", "alerts", "snapshots", "reports")
    paths: Dict[str, str] = {}
    for name in subdirs:
        target = os.path.join(base_dir, name)
        os.makedirs(target, exist_ok=True)
        paths[name] = target
    return paths
