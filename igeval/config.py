"""Validated evaluator configuration."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from .errors import (
    DomainError,
    E_INVALID_CONFIG,
    E_INVALID_LATENCY_BUDGET,
    E_INVALID_MEMORY_LIMIT,
    E_UNKNOWN_PRECISION,
)
from .util import sha256_json


PRECISION_EXACT = "exact"
SUPPORTED_PRECISIONS = (PRECISION_EXACT,)
PIPELINE_VERSION = "igeval-pipeline/1"


@dataclass(frozen=True)
class EvaluatorConfig:
    """Fixed headings for the deterministic pipeline.

    ``exact`` is the only supported precision in this component. The field is
    explicit so callers cannot silently opt into an undeclared approximation.
    """

    down_width: int = 16
    down_height: int = 16
    block_rows: int = 4
    block_cols: int = 4
    embedding_dim: int = 12
    precision: str = PRECISION_EXACT
    workers: int = 2
    latency_budget_ms: float = 100.0
    max_cache_entries: int = 128
    max_feature_memory_bytes: int = 8 * 1024 * 1024
    score_decimals: int = 10
    consistency_tolerance: float = 1e-9

    def __post_init__(self) -> None:
        validate_config(self)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvaluatorConfig":
        return cls(**value)

    @property
    def fingerprint(self) -> str:
        return sha256_json(
            {
                "schema": "igeval.config/1",
                "pipeline_version": PIPELINE_VERSION,
                "scoring": {
                    "down_width": self.down_width,
                    "down_height": self.down_height,
                    "block_rows": self.block_rows,
                    "block_cols": self.block_cols,
                    "embedding_dim": self.embedding_dim,
                    "precision": self.precision,
                    "score_decimals": self.score_decimals,
                },
            }
        )


def _positive_int(error: DomainError, value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DomainError(
            error.code,
            f"{field} must be a positive integer",
            field=field,
            category="configuration",
        )


def validate_config(config: EvaluatorConfig) -> None:
    if config.precision not in SUPPORTED_PRECISIONS:
        raise DomainError(
            E_UNKNOWN_PRECISION,
            f"unknown precision: {config.precision!r}",
            field="precision",
            category="configuration",
            details={"supported": list(SUPPORTED_PRECISIONS)},
        )
    for field in ("down_width", "down_height", "block_rows", "block_cols"):
        _positive_int(DomainError(E_INVALID_CONFIG, field), getattr(config, field), field)
    if config.block_rows > config.down_height or config.block_cols > config.down_width:
        raise DomainError(
            E_INVALID_CONFIG,
            "block grid cannot exceed the downsampled image",
            field="block_rows/block_cols",
            category="configuration",
        )
    if config.embedding_dim != 12:
        raise DomainError(
            E_INVALID_CONFIG,
            "embedding_dim is fixed at 12 for pipeline version igeval-pipeline/1",
            field="embedding_dim",
            category="configuration",
        )
    _positive_int(DomainError(E_INVALID_CONFIG, "workers"), config.workers, "workers")
    _positive_int(
        DomainError(E_INVALID_MEMORY_LIMIT, "max_cache_entries"),
        config.max_cache_entries,
        "max_cache_entries",
    )
    _positive_int(
        DomainError(E_INVALID_MEMORY_LIMIT, "max_feature_memory_bytes"),
        config.max_feature_memory_bytes,
        "max_feature_memory_bytes",
    )
    if (
        isinstance(config.latency_budget_ms, bool)
        or not isinstance(config.latency_budget_ms, (int, float))
        or not math.isfinite(float(config.latency_budget_ms))
        or float(config.latency_budget_ms) <= 0
    ):
        raise DomainError(
            E_INVALID_LATENCY_BUDGET,
            "latency_budget_ms must be a finite positive number",
            field="latency_budget_ms",
            category="configuration",
        )
    if (
        isinstance(config.score_decimals, bool)
        or not isinstance(config.score_decimals, int)
        or not 0 <= config.score_decimals <= 15
    ):
        raise DomainError(
            E_INVALID_CONFIG,
            "score_decimals must be an integer in [0, 15]",
            field="score_decimals",
            category="configuration",
        )
    if (
        isinstance(config.consistency_tolerance, bool)
        or not isinstance(config.consistency_tolerance, (int, float))
        or not math.isfinite(float(config.consistency_tolerance))
        or float(config.consistency_tolerance) <= 0
    ):
        raise DomainError(
            E_INVALID_CONFIG,
            "consistency_tolerance must be a finite positive number",
            field="consistency_tolerance",
            category="configuration",
        )


def validate_precision(precision: str) -> None:
    if precision not in SUPPORTED_PRECISIONS:
        raise DomainError(
            E_UNKNOWN_PRECISION,
            f"unknown precision: {precision!r}",
            field="precision",
            category="precision",
            details={"supported": list(SUPPORTED_PRECISIONS)},
        )

