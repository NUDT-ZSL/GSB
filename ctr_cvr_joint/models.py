"""Domain data structures shared by the readers, engine and CLI."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .errors import (
    E_FORMAT_INVALID,
    E_THRESHOLD_INVALID,
    E_TOLERANCE_INVALID,
    fail,
)
from .hashing import sha256_obj


def _finite_float(value: Any, *, field: str, line: int | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        fail(
            E_FORMAT_INVALID,
            f"field {field!r} is not numeric",
            line=line,
            field=field,
            value=value,
        )
    if not math.isfinite(parsed):
        fail(
            E_FORMAT_INVALID,
            f"field {field!r} must be finite",
            line=line,
            field=field,
            value=value,
        )
    return 0.0 if parsed == 0.0 else parsed


def _non_empty(value: Any, *, field: str, line: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(
            E_FORMAT_INVALID,
            f"required field {field!r} is missing or empty",
            line=line,
            field=field,
        )
    return value.strip()


@dataclass(frozen=True)
class NumericFeature:
    name: str
    mean: float
    std: float
    weight: float
    missing_strategy: str | None
    default: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mean": self.mean,
            "std": self.std,
            "weight": self.weight,
            "missing_strategy": self.missing_strategy,
            "default": self.default,
        }


@dataclass(frozen=True)
class CategoricalFeature:
    name: str
    weight: float
    buckets: int | None
    missing_strategy: str | None
    default: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": self.weight,
            "buckets": self.buckets,
            "missing_strategy": self.missing_strategy,
            "default": self.default,
        }


@dataclass(frozen=True)
class CrossFeature:
    name: str
    left: str
    right: str
    weight: float
    slots: tuple[str, ...]
    missing_strategy: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "left": self.left,
            "right": self.right,
            "weight": self.weight,
            "slots": list(self.slots),
            "missing_strategy": self.missing_strategy,
        }


@dataclass(frozen=True)
class StatSpec:
    metric: str
    prior: float
    smoothing: float
    semantics_name: str | None
    semantics_version: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "prior": self.prior,
            "smoothing": self.smoothing,
            "semantics_name": self.semantics_name,
            "semantics_version": self.semantics_version,
        }


@dataclass(frozen=True)
class FeatureSpec:
    """Version-neutral internal feature specification.

    Fields that a source format cannot express remain ``None`` until migration
    normalization fills them.  This distinction is what makes migration records
    replayable instead of merely descriptive.
    """

    source_version: int
    numeric: tuple[NumericFeature, ...]
    categorical: tuple[CategoricalFeature, ...]
    cross: tuple[CrossFeature, ...]
    statistics: tuple[StatSpec, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "numeric": [item.to_dict() for item in self.numeric],
            "categorical": [item.to_dict() for item in self.categorical],
            "cross": [item.to_dict() for item in self.cross],
            "statistics": [item.to_dict() for item in self.statistics],
            "metadata": self.metadata,
        }

    @property
    def checksum(self) -> str:
        return sha256_obj(self.to_dict())

    def numeric_map(self) -> dict[str, NumericFeature]:
        return {item.name: item for item in self.numeric}

    def categorical_map(self) -> dict[str, CategoricalFeature]:
        return {item.name: item for item in self.categorical}

    def cross_map(self) -> dict[str, CrossFeature]:
        return {item.name: item for item in self.cross}

    def stat_map(self) -> dict[str, StatSpec]:
        return {item.metric: item for item in self.statistics}


@dataclass(frozen=True)
class LineSummary:
    line: int
    checksum: str
    preview: str

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "checksum": self.checksum, "preview": self.preview}


@dataclass(frozen=True)
class Sample:
    request_id: str
    timestamp_ms: int
    user_id: str
    item_id: str
    numeric_feats: dict[str, float]
    categorical_feats: dict[str, str]
    label_click: int
    label_convert: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "timestamp_ms": self.timestamp_ms,
            "user_id": self.user_id,
            "item_id": self.item_id,
            "numeric_feats": self.numeric_feats,
            "categorical_feats": self.categorical_feats,
            "label_click": self.label_click,
            "label_convert": self.label_convert,
        }

    def feature_projection(self) -> dict[str, Any]:
        """The part of a sample used by feature assembly.

        Labels are deliberately excluded so a label correction can reuse both
        assembly and scoring products and invalidate only accumulation.
        """

        return {
            "request_id": self.request_id,
            "timestamp_ms": self.timestamp_ms,
            "user_id": self.user_id,
            "item_id": self.item_id,
            "numeric_feats": self.numeric_feats,
            "categorical_feats": self.categorical_feats,
        }

    @property
    def feature_content_hash(self) -> str:
        return sha256_obj(self.feature_projection())

    @property
    def label_hash(self) -> str:
        return sha256_obj(
            {"label_click": self.label_click, "label_convert": self.label_convert}
        )

    @property
    def content_hash(self) -> str:
        return sha256_obj(self.to_dict())


@dataclass(frozen=True)
class ThresholdConfig:
    policy: str = "strict"
    click_threshold: float = 0.5
    cvr_threshold: float = 0.5
    tolerance: float = 0.0
    stable_digits: int = 12

    def __post_init__(self) -> None:
        if self.policy not in {"strict", "tolerance"}:
            fail(
                E_THRESHOLD_INVALID,
                "threshold policy must be 'strict' or 'tolerance'",
                field="policy",
                value=self.policy,
            )
        for name, value in (
            ("click_threshold", self.click_threshold),
            ("cvr_threshold", self.cvr_threshold),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                fail(E_THRESHOLD_INVALID, "threshold must be finite", field=name, value=value)
            if not 0.0 < float(value) < 1.0:
                fail(
                    E_THRESHOLD_INVALID,
                    "threshold must be strictly between zero and one",
                    field=name,
                    value=value,
                )
        if not isinstance(self.tolerance, (int, float)) or not math.isfinite(
            float(self.tolerance)
        ):
            fail(
                E_TOLERANCE_INVALID,
                "tolerance must be finite",
                field="tolerance",
                value=self.tolerance,
            )
        if float(self.tolerance) < 0.0 or float(self.tolerance) >= 0.5:
            fail(
                E_TOLERANCE_INVALID,
                "tolerance must be in [0, 0.5)",
                field="tolerance",
                value=self.tolerance,
            )
        if not 0 <= int(self.stable_digits) <= 15:
            fail(
                E_THRESHOLD_INVALID,
                "stable_digits must be in [0, 15]",
                field="stable_digits",
                value=self.stable_digits,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "click_threshold": float(self.click_threshold),
            "cvr_threshold": float(self.cvr_threshold),
            "tolerance": float(self.tolerance),
            "stable_digits": int(self.stable_digits),
        }

    @property
    def checksum(self) -> str:
        return sha256_obj(self.to_dict())

