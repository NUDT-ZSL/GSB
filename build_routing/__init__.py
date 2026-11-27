"""Deterministic multi-region build egress routing workbench.

The package intentionally depends only on the Python standard library.
"""

from .diff import compare_projects
from .engine import ReplayEngine
from .model import (
    ALL_FILTERED,
    LOAD_ERROR,
    READY,
    SINGLE_CANDIDATE,
    BuildNode,
    FilterRule,
    NextHop,
    NormalizedProject,
    Policy,
    Prefix,
    Region,
    RouteCandidate,
    SelectionResult,
)
from .normalize import (
    ReferenceValidationError,
    load_project,
    normalize_project,
    project_from_storage,
    reference_report,
)
from .selector import select_prefix
from .stats import build_availability_stats
from .store import WorkbenchStore

__all__ = [
    "ALL_FILTERED",
    "LOAD_ERROR",
    "READY",
    "SINGLE_CANDIDATE",
    "BuildNode",
    "FilterRule",
    "NextHop",
    "NormalizedProject",
    "Policy",
    "Prefix",
    "Region",
    "ReplayEngine",
    "ReferenceValidationError",
    "RouteCandidate",
    "SelectionResult",
    "WorkbenchStore",
    "build_availability_stats",
    "compare_projects",
    "load_project",
    "normalize_project",
    "project_from_storage",
    "reference_report",
    "select_prefix",
]
