"""Deterministic, standard-library-only columnar batch evaluator."""

from .model import (
    BatchBudget,
    BudgetRejected,
    ColumnVector,
    DType,
    EvalBatch,
    EvaluationError,
)
from .generator import GeneratedSample, generate_sample
from .normalize import (
    NormalizedModel,
    build_version_difference_manifest,
    normalize_all,
    normalize_schema_version,
)
from .evaluator import EvaluationRun, evaluate_batch, evaluate_model
from .reference import ReferenceRun, reference_evaluate_batch, reference_evaluate_model
from .verify import (
    ReferenceMismatch,
    compare_version_consistency,
    verify_against_reference,
)
from .acceptance import run_acceptance

__all__ = [
    "BatchBudget",
    "BudgetRejected",
    "ColumnVector",
    "DType",
    "EvalBatch",
    "EvaluationError",
    "GeneratedSample",
    "generate_sample",
    "NormalizedModel",
    "normalize_all",
    "normalize_schema_version",
    "build_version_difference_manifest",
    "EvaluationRun",
    "evaluate_batch",
    "evaluate_model",
    "ReferenceRun",
    "reference_evaluate_batch",
    "reference_evaluate_model",
    "ReferenceMismatch",
    "verify_against_reference",
    "compare_version_consistency",
    "run_acceptance",
]
