"""Deterministic offline MoE capacity-planning toolkit.

The package intentionally uses only the Python standard library.  The public
entry points are:

* :func:`generate_project_set` -- deterministic three-schema sample generator.
* :func:`normalize_file` / :func:`normalize_document` -- schema normalization.
* :func:`run_allocation` -- request-by-request routing, event replay, ledgers,
  imbalance metrics, and final min-max rebalancing.
* :func:`run_reference` -- independently implemented reference allocator.
* :func:`verify_project_set` -- end-to-end acceptance checks.
"""

from .generator import generate_project_set, write_project_set
from .model import (
    ChangeEvent,
    Expert,
    InferenceRequest,
    NormalizedProject,
    Replica,
    Topology,
)
from .normalize import (
    NormalizationError,
    canonical_fingerprint,
    normalize_document,
    normalize_file,
)
from .reference import compare_with_reference, run_reference
from .router import run_allocation
from .verify import verify_project_set

__all__ = [
    "ChangeEvent",
    "Expert",
    "InferenceRequest",
    "NormalizedProject",
    "NormalizationError",
    "Replica",
    "Topology",
    "canonical_fingerprint",
    "compare_with_reference",
    "generate_project_set",
    "normalize_document",
    "normalize_file",
    "run_allocation",
    "run_reference",
    "verify_project_set",
    "write_project_set",
]

