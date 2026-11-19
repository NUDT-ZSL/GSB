"""Public integration surface for the domain component."""

from .decision import (
    CloneCandidateScore,
    adjudicate_clone_similarity,
    rank_clone_scores,
    sanitize_similarity,
)
from .diagnostics import (
    build_fault_injected_inputs,
    diagnose_dataset,
    diagnose_records,
)
from .embedding import (
    ALIGNMENT_DIMENSION,
    EMBEDDING_DIMENSION,
    STATISTICS_SPEC,
    TimbreEstimate,
    align_units,
    candidate_catalog_and_bases,
    estimate_timbre,
    frame_alignment_vectors,
)
from .error_spectrum import aggregate_error_spectra, compute_error_spectrum
from .features import (
    FeatureTable,
    extract_and_validate,
    extract_features,
    f0_energy_observation,
    validate_frame_alignment,
)
from .pipeline import PipelineResult, run_pipeline
from .precision import (
    PrecisionExecution,
    execute_precision,
    precision_summary,
    quantize_to_grid,
    verify_fixed_recomputation,
)
from .selfcheck import assert_selfcheck, run_selfcheck
from .snapshots import SnapshotManager
from .synthesis import Dataset, generate_dataset, load_dataset

__all__ = [
    "ALIGNMENT_DIMENSION",
    "EMBEDDING_DIMENSION",
    "STATISTICS_SPEC",
    "CloneCandidateScore",
    "Dataset",
    "FeatureTable",
    "PipelineResult",
    "PrecisionExecution",
    "SnapshotManager",
    "TimbreEstimate",
    "adjudicate_clone_similarity",
    "aggregate_error_spectra",
    "align_units",
    "assert_selfcheck",
    "build_fault_injected_inputs",
    "candidate_catalog_and_bases",
    "compute_error_spectrum",
    "diagnose_dataset",
    "diagnose_records",
    "estimate_timbre",
    "execute_precision",
    "extract_and_validate",
    "extract_features",
    "f0_energy_observation",
    "frame_alignment_vectors",
    "generate_dataset",
    "load_dataset",
    "precision_summary",
    "quantize_to_grid",
    "rank_clone_scores",
    "run_pipeline",
    "run_selfcheck",
    "sanitize_similarity",
    "validate_frame_alignment",
    "verify_fixed_recomputation",
]

