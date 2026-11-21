"""Unified in-memory model for monitoring configurations.

All three on-disk formats (v1 positional pipe lines, v2 key=value lines and
the structured v3 JSON document) parse into the same :class:`MonitorConfig`
representation so that downstream statistics and tests never need to know
which dialect was used.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from .hashing import content_hash

NUMERIC = "numeric"
CATEGORICAL = "categorical"
COLUMN_TYPES = (NUMERIC, CATEGORICAL)

# Supported drift test methods.  ``mean_shift`` and ``quantile_dist`` apply to
# numeric columns; ``proportion_dist`` applies to categorical columns.
MEAN_SHIFT = "mean_shift"
QUANTILE_DIST = "quantile_dist"
PROPORTION_DIST = "proportion_dist"

NUMERIC_METHODS = (MEAN_SHIFT, QUANTILE_DIST)
CATEGORICAL_METHODS = (PROPORTION_DIST,)

# Defaults that v1/v2 are upgraded with.  They are intentionally explicit so
# that the "completion source" audit trail can point at a single constant.
DEFAULT_METHOD = {NUMERIC: MEAN_SHIFT, CATEGORICAL: PROPORTION_DIST}
DEFAULT_QUANTILES: Tuple[float, ...] = (0.25, 0.5, 0.75)
DEFAULT_MIN_SAMPLES = 30
DEFAULT_WINDOW = 4


@dataclass
class FeatureSpec:
    name: str
    column_type: str
    threshold: float
    window: int
    methods: List[str]
    quantiles: List[float] = field(default_factory=list)
    min_samples: int = DEFAULT_MIN_SAMPLES
    enabled: bool = True

    def validate(self) -> None:
        if self.column_type not in COLUMN_TYPES:
            from .errors import DriftError, ErrorCode

            raise DriftError(
                ErrorCode.CONFIG_INVALID,
                f"unsupported column type {self.column_type!r}",
                {"field": "column_type", "feature": self.name},
            )
        if self.column_type == NUMERIC:
            for method in self.methods:
                if method not in NUMERIC_METHODS:
                    from .errors import DriftError, ErrorCode

                    raise DriftError(
                        ErrorCode.CONFIG_INVALID,
                        f"unknown numeric test method {method!r}",
                        {"field": "methods", "feature": self.name},
                    )
        else:
            for method in self.methods:
                if method not in CATEGORICAL_METHODS:
                    from .errors import DriftError, ErrorCode

                    raise DriftError(
                        ErrorCode.CONFIG_INVALID,
                        f"categorical column cannot use method {method!r}",
                        {"field": "methods", "feature": self.name},
                    )
        if not (0.0 < float(self.threshold) < float("inf")):
            from .errors import DriftError, ErrorCode

            raise DriftError(
                ErrorCode.CONFIG_INVALID,
                "threshold must be a finite positive number",
                {"field": "threshold", "feature": self.name, "value": self.threshold},
            )
        if self.window <= 0:
            from .errors import DriftError, ErrorCode

            raise DriftError(
                ErrorCode.CONFIG_INVALID,
                "reference window length must be positive",
                {"field": "window", "feature": self.name, "value": self.window},
            )
        if self.min_samples <= 0:
            from .errors import DriftError, ErrorCode

            raise DriftError(
                ErrorCode.CONFIG_INVALID,
                "min_samples must be positive",
                {"field": "min_samples", "feature": self.name},
            )
        if self.column_type == NUMERIC:
            for q in self.quantiles:
                if not (0.0 < q < 1.0):
                    from .errors import DriftError, ErrorCode

                    raise DriftError(
                        ErrorCode.CONFIG_INVALID,
                        "quantiles must lie strictly inside (0, 1)",
                        {"field": "quantiles", "feature": self.name, "value": q},
                    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MonitorConfig:
    config_id: str
    source_format: Optional[str]  # "v1" | "v2" | "v3" | None for synthesized
    features: Dict[str, FeatureSpec]
    provenance: List[Dict[str, Any]] = field(default_factory=list)

    def feature_names(self) -> List[str]:
        return sorted(self.features)

    def validate(self) -> None:
        if not self.features:
            from .errors import DriftError, ErrorCode

            raise DriftError(
                ErrorCode.CONFIG_INVALID,
                "configuration contains no monitored features",
                {"field": "features"},
            )
        for name in self.feature_names():
            self.features[name].validate()

    def definition_digest(self) -> str:
        """Stable summary of the feature column definitions (cache key input)."""
        payload = [
            {
                "name": spec.name,
                "type": spec.column_type,
                "threshold": spec.threshold,
                "window": spec.window,
                "methods": list(spec.methods),
                "quantiles": list(spec.quantiles),
                "min_samples": spec.min_samples,
                "enabled": spec.enabled,
            }
            for spec in (self.features[n] for n in self.feature_names())
        ]
        return content_hash({"config_id": self.config_id, "features": payload})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config_id": self.config_id,
            "source_format": self.source_format,
            "definition_digest": self.definition_digest(),
            "features": {n: self.features[n].to_dict() for n in self.feature_names()},
            "provenance": self.provenance,
        }


def specs_equal(a: FeatureSpec, b: FeatureSpec) -> bool:
    """Value equality ignoring the (audit only) provenance container."""
    return (
        a.name == b.name
        and a.column_type == b.column_type
        and float(a.threshold) == float(b.threshold)
        and int(a.window) == int(b.window)
        and list(a.methods) == list(b.methods)
        and [float(q) for q in a.quantiles] == [float(q) for q in b.quantiles]
        and int(a.min_samples) == int(b.min_samples)
        and bool(a.enabled) == bool(b.enabled)
    )
