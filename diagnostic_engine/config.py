from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import DiagnosticError
from .utils import canonical_json_bytes


PRECISIONS = ("float64", "float32", "fixed")
REGULARIZATIONS = ("l2", "l1", "elastic_net")
ROUNDING_MODES = ("nearest_even", "floor", "ceil", "trunc")
STANDARDIZATIONS = ("zscore", "minmax")


def _as_tuple_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise DiagnosticError(
            "E_CONFIG_INVALID", "config", "column list must be a JSON array"
        )
    return tuple(str(item) for item in value)


def _normalize_regularization(value: Any) -> str:
    aliases = {"ridge": "l2", "lasso": "l1", "elasticnet": "elastic_net"}
    normalized = aliases.get(str(value).lower(), str(value).lower())
    if normalized not in REGULARIZATIONS:
        raise DiagnosticError(
            "E_CONFIG_INVALID",
            "config",
            f"unsupported regularization type {value!r}",
        )
    return normalized


@dataclass(frozen=True)
class EngineConfig:
    snapshot_id: str
    version: int
    generation: int
    created_at_ms: int
    checksum: str
    regularization: str
    strength_path: tuple[float, ...]
    max_iter: int
    tolerance: float
    precision: str
    fixed_scale: int
    rounding_mode: str
    block_size: int | None
    block_budget_bytes: int
    standardization: str
    numeric_columns: tuple[str, ...]
    category_columns: tuple[str, ...]
    category_vocab: dict[str, tuple[str, ...]]
    learning_rate: float
    l1_ratio: float
    coefficient_tolerance: float
    objective_tolerance: float
    metadata_sources: dict[str, str] = field(default_factory=dict)
    injections: tuple[dict[str, Any], ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_parameters(
        cls,
        parameters: dict[str, Any],
        *,
        snapshot_id: str,
        version: int,
        generation: int,
        created_at_ms: int,
        checksum: str,
        metadata_sources: dict[str, str] | None = None,
    ) -> "EngineConfig":
        regularization = _normalize_regularization(
            parameters.get("regularization", parameters.get("regularization_type", "l2"))
        )
        raw_path = parameters.get("strength_path", parameters.get("lambdas"))
        if not isinstance(raw_path, (list, tuple)) or not raw_path:
            raise DiagnosticError(
                "E_CONFIG_INVALID",
                "config",
                "strength_path must be a non-empty JSON array",
            )
        strength_path = tuple(float(item) for item in raw_path)
        precision = str(parameters.get("precision", "float64")).lower()
        if precision not in PRECISIONS:
            raise DiagnosticError(
                "E_CONFIG_INVALID",
                "config",
                f"unsupported precision {precision!r}",
            )
        standardization = str(
            parameters.get("standardization", parameters.get("scaler", "zscore"))
        ).lower()
        if standardization not in STANDARDIZATIONS:
            raise DiagnosticError(
                "E_CONFIG_INVALID",
                "config",
                f"unsupported standardization {standardization!r}",
            )
        rounding_mode = str(parameters.get("rounding_mode", "nearest_even")).lower()
        if rounding_mode not in ROUNDING_MODES:
            raise DiagnosticError(
                "E_CONFIG_INVALID",
                "config",
                f"unsupported rounding_mode {rounding_mode!r}",
            )
        block_size = parameters.get("block_size")
        if block_size is not None:
            block_size = int(block_size)
        vocab_raw = parameters.get("category_vocab", {})
        if not isinstance(vocab_raw, dict):
            raise DiagnosticError(
                "E_CONFIG_INVALID", "config", "category_vocab must be a JSON object"
            )
        category_vocab = {
            str(column): tuple(str(item) for item in values)
            for column, values in vocab_raw.items()
        }
        known = {
            "snapshot_id",
            "version",
            "generation",
            "created_at_ms",
            "checksum",
            "id",
            "label",
            "regularization",
            "regularization_type",
            "strength_path",
            "lambdas",
            "max_iter",
            "tolerance",
            "precision",
            "fixed_scale",
            "rounding_mode",
            "block_size",
            "block_budget_bytes",
            "budget_bytes",
            "standardization",
            "scaler",
            "numeric_columns",
            "category_columns",
            "category_vocab",
            "learning_rate",
            "l1_ratio",
            "coefficient_tolerance",
            "objective_tolerance",
            "metadata_sources",
            "injections",
        }
        extra = {key: value for key, value in parameters.items() if key not in known}
        return cls(
            snapshot_id=snapshot_id,
            version=version,
            generation=generation,
            created_at_ms=created_at_ms,
            checksum=checksum,
            regularization=regularization,
            strength_path=strength_path,
            max_iter=int(parameters.get("max_iter", 300)),
            tolerance=float(parameters.get("tolerance", 1e-6)),
            precision=precision,
            fixed_scale=int(parameters.get("fixed_scale", 4096)),
            rounding_mode=rounding_mode,
            block_size=block_size,
            block_budget_bytes=int(
                parameters.get("block_budget_bytes", parameters.get("budget_bytes", 1_048_576))
            ),
            standardization=standardization,
            numeric_columns=_as_tuple_strings(parameters.get("numeric_columns")),
            category_columns=_as_tuple_strings(parameters.get("category_columns")),
            category_vocab=category_vocab,
            learning_rate=float(parameters.get("learning_rate", 0.5)),
            l1_ratio=float(parameters.get("l1_ratio", 0.5)),
            coefficient_tolerance=float(parameters.get("coefficient_tolerance", 5e-3)),
            objective_tolerance=float(parameters.get("objective_tolerance", 2e-2)),
            metadata_sources=dict(metadata_sources or {}),
            injections=tuple(dict(item) for item in parameters.get("injections", ())),
            extra=extra,
        )

    def parameters(self) -> dict[str, Any]:
        payload = {
            "regularization": self.regularization,
            "strength_path": list(self.strength_path),
            "max_iter": self.max_iter,
            "tolerance": self.tolerance,
            "precision": self.precision,
            "fixed_scale": self.fixed_scale,
            "rounding_mode": self.rounding_mode,
            "block_size": self.block_size,
            "block_budget_bytes": self.block_budget_bytes,
            "standardization": self.standardization,
            "numeric_columns": list(self.numeric_columns),
            "category_columns": list(self.category_columns),
            "category_vocab": {
                key: list(value) for key, value in self.category_vocab.items()
            },
            "learning_rate": self.learning_rate,
            "l1_ratio": self.l1_ratio,
            "coefficient_tolerance": self.coefficient_tolerance,
            "objective_tolerance": self.objective_tolerance,
            "injections": list(self.injections),
        }
        payload.update(self.extra)
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "generation": self.generation,
            "created_at_ms": self.created_at_ms,
            "checksum": self.checksum,
            "metadata_sources": self.metadata_sources,
            "parameters": self.parameters(),
        }

    def fingerprint(self) -> str:
        return canonical_json_bytes(self.parameters()).decode("utf-8")

    def with_updates(self, **updates: Any) -> "EngineConfig":
        from dataclasses import replace

        return replace(self, **updates)

