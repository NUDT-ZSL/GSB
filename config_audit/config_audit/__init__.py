"""config_audit: deterministic fixed-point configuration consistency audit.

Public API
----------
load_archives, Grid, quantize, run_audit, compare_pair, Tolerance,
build_quantized, save_snapshot, load_snapshot, restore_and_replay,
canonical_hash, and the structured business error classes.
"""

from __future__ import annotations

from .audit import canonical_hash, run_audit
from .comparison import PairVerdict, Tolerance, compare_pair
from .errors import (
    AuditError,
    ConfigError,
    CycleError,
    GridError,
    PropagateError,
    RangeError,
    ReplayError,
)
from .fixedpoint import Grid, quantize, quantize_coefficient
from .loader import load_archives
from .logging_utils import StructuredLogger
from .propagation import build_quantized
from .rangecheck import enforce_ranges
from .snapshot import (
    load_snapshot,
    restore_and_replay,
    save_snapshot,
    versions_match,
)

__version__ = "1.0.0"

__all__ = [
    "AuditError",
    "ConfigError",
    "CycleError",
    "Grid",
    "GridError",
    "PairVerdict",
    "PropagateError",
    "RangeError",
    "ReplayError",
    "StructuredLogger",
    "Tolerance",
    "build_quantized",
    "canonical_hash",
    "compare_pair",
    "enforce_ranges",
    "load_archives",
    "load_snapshot",
    "quantize",
    "quantize_coefficient",
    "restore_and_replay",
    "run_audit",
    "save_snapshot",
    "versions_match",
]
