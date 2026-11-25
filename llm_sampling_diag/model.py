from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_ERROR_BUDGET = 1e-6
DEFAULT_CHUNK_SIZE = 4
DEFAULT_REVISION = 1
DEFAULT_REVISION_ID = "r-1"


@dataclass(frozen=True)
class ChunkPlan:
    chunk_size: int
    chunk_count: int

    def to_dict(self) -> dict[str, int]:
        return {"chunk_size": self.chunk_size, "chunk_count": self.chunk_count}

    @classmethod
    def for_logits(cls, logits_length: int, chunk_size: int = DEFAULT_CHUNK_SIZE) -> "ChunkPlan":
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        return cls(chunk_size=chunk_size, chunk_count=(logits_length + chunk_size - 1) // chunk_size)


@dataclass(frozen=True)
class CandidateMask:
    excluded_indices: tuple[int, ...] = ()
    selectors: tuple[dict[str, Any], ...] = ()
    mask_version: int = 1
    source: str = "flat"

    @classmethod
    def empty(cls, source: str = "default_empty") -> "CandidateMask":
        return cls(excluded_indices=(), selectors=(), mask_version=1, source=source)

    @classmethod
    def flat(cls, excluded_indices: list[int] | tuple[int, ...], source: str = "flat") -> "CandidateMask":
        return cls(
            excluded_indices=tuple(sorted(set(int(index) for index in excluded_indices))),
            selectors=(),
            mask_version=1,
            source=source,
        )

    def has_exclusions(self) -> bool:
        return bool(self.excluded_indices)

    def to_nested_dict(self) -> dict[str, Any]:
        if self.selectors:
            return {
                "mask_version": self.mask_version,
                "selectors": [dict(selector) for selector in self.selectors],
            }
        return {
            "mask_version": self.mask_version,
            "selectors": [
                {"selector_id": "flat", "op": "exclude", "indices": list(self.excluded_indices)}
            ]
            if self.excluded_indices
            else [],
        }


@dataclass(frozen=True)
class UnifiedTask:
    case_id: str
    effective_at_ms: int
    logits: tuple[float, ...]
    logits_shape: tuple[int, ...]
    k_mode: str
    k_value: float
    temperature: float
    tie_policy: str | None
    seed: int | None
    candidate_mask: CandidateMask
    error_budget: float
    revision: int
    revision_id: str
    chunk_plan: ChunkPlan
    num_samples: int
    top_p: float | None
    source_format: str
    source_ref: str
    migration_actions: tuple[dict[str, Any], ...] = ()

    def semantic_projection(self, include_revision: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "case_id": self.case_id,
            "effective_at_ms": self.effective_at_ms,
            "logits": list(self.logits),
            "logits_shape": list(self.logits_shape),
            "k_mode": self.k_mode,
            "k_value": self.k_value,
            "temperature": self.temperature,
            "tie_policy": self.tie_policy,
            "seed": self.seed,
            "excluded_indices": list(self.candidate_mask.excluded_indices),
            "error_budget": self.error_budget,
            "chunk_plan": self.chunk_plan.to_dict(),
            "num_samples": self.num_samples,
            "top_p": self.top_p,
        }
        if include_revision:
            result["revision"] = self.revision
        return result

    def to_snapshot_dict(self) -> dict[str, Any]:
        return {
            "schema": "unified-task-v1",
            "case_id": self.case_id,
            "effective_at_ms": self.effective_at_ms,
            "logits": list(self.logits),
            "logits_shape": list(self.logits_shape),
            "k_mode": self.k_mode,
            "k_value": self.k_value,
            "temperature": self.temperature,
            "tie_policy": self.tie_policy,
            "seed": self.seed,
            "candidate_mask": {
                "mask_version": self.candidate_mask.mask_version,
                "excluded_indices": list(self.candidate_mask.excluded_indices),
                "selectors": [dict(item) for item in self.candidate_mask.selectors],
                "source": self.candidate_mask.source,
            },
            "error_budget": self.error_budget,
            "revision": self.revision,
            "revision_id": self.revision_id,
            "chunk_plan": self.chunk_plan.to_dict(),
            "num_samples": self.num_samples,
            "top_p": self.top_p,
            "source_format": self.source_format,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class OperatorConfig:
    mask_priority: str | None
    dtype: str = "float64"
    rounding_mode: str = "none"
    subnormal_guard: str = "clamp_tiny"
    tolerance: float = 1e-5
    rng_algorithm: str | None = "pcg64"

    def validate(self) -> None:
        from .errors import DiagnosticError, ErrorCode

        if self.dtype not in {"float32", "float64"}:
            raise DiagnosticError(ErrorCode.E_CONFIG_INVALID, "dtype must be float32 or float64", field="dtype")
        if self.rounding_mode not in {"none", "nearest_even", "toward_zero", "floor", "ceil"}:
            raise DiagnosticError(
                ErrorCode.E_CONFIG_INVALID,
                "unknown rounding mode",
                field="rounding_mode",
            )
        if self.subnormal_guard not in {"none", "clamp_tiny", "flush_zero"}:
            raise DiagnosticError(
                ErrorCode.E_CONFIG_INVALID,
                "unknown subnormal guard",
                field="subnormal_guard",
            )
        if self.tolerance < 0 or not float(self.tolerance) == self.tolerance:
            raise DiagnosticError(
                ErrorCode.E_CONFIG_INVALID,
                "tolerance must be a finite non-negative number",
                field="tolerance",
            )
        if self.rng_algorithm is None:
            raise DiagnosticError(ErrorCode.E_RNG_UNSPECIFIED, "RNG algorithm was not declared", field="rng_algorithm")
        if self.rng_algorithm not in {"pcg64"}:
            raise DiagnosticError(
                ErrorCode.E_CONFIG_INVALID,
                "unsupported RNG algorithm",
                field="rng_algorithm",
            )
        if self.mask_priority not in {None, "mask_first", "k_first"}:
            raise DiagnosticError(
                ErrorCode.E_MASK_PRIORITY_INVALID,
                "mask_priority must be mask_first or k_first",
                field="mask_priority",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mask_priority": self.mask_priority,
            "dtype": self.dtype,
            "rounding_mode": self.rounding_mode,
            "subnormal_guard": self.subnormal_guard,
            "tolerance": self.tolerance,
            "rng_algorithm": self.rng_algorithm,
        }


@dataclass
class ReadResult:
    tasks: list[UnifiedTask]
    records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MigrationPolicy:
    mask_policy: str = "flatten"
    revision_policy: str = "drop"
    strict: bool = False

    def validate(self) -> None:
        from .errors import DiagnosticError, ErrorCode

        if self.mask_policy not in {"flatten", "drop"}:
            raise DiagnosticError(
                ErrorCode.E_CONFIG_INVALID,
                "mask_policy must be flatten or drop",
                field="mask_policy",
            )
        if self.revision_policy not in {"drop", "strict"}:
            raise DiagnosticError(
                ErrorCode.E_CONFIG_INVALID,
                "revision_policy must be drop or strict",
                field="revision_policy",
            )


@dataclass
class MigrationOutcome:
    target_format: str
    payload: Any
    records: list[dict[str, Any]]
    semantic_complete: bool

