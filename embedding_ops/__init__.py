"""Deterministic embedding gather, reduction, and numerical diagnostics."""

from .config import NumericsConfig
from .diagnostics import (
    build_error_spectrum,
    run_error_injection_suite,
    self_check_job,
)
from .errors import BusinessError
from .models import JobSpec
from .pipeline import gather_embedding, reduce_and_weight, run_job
from .snapshots import Snapshot, SnapshotStore, create_snapshot

__all__ = [
    "BusinessError",
    "JobSpec",
    "NumericsConfig",
    "Snapshot",
    "SnapshotStore",
    "build_error_spectrum",
    "create_snapshot",
    "gather_embedding",
    "reduce_and_weight",
    "run_error_injection_suite",
    "run_job",
    "self_check_job",
]

__version__ = "1.0.0"
