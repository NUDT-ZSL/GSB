"""Deterministic offline dependency supply-chain integrity diagnostics."""

from .checkpoint import CheckpointError, load_checkpoint, save_checkpoint
from .diagnostics import DiagnosticEngine
from .manifest import parse_manifest, parse_manifest_file
from .versioning import (
    Interval,
    Version,
    VersionContext,
    intersect_intervals,
    parse_version_expression,
)

__all__ = [
    "CheckpointError",
    "DiagnosticEngine",
    "Interval",
    "Version",
    "VersionContext",
    "intersect_intervals",
    "load_checkpoint",
    "parse_manifest",
    "parse_manifest_file",
    "parse_version_expression",
    "save_checkpoint",
]

__version__ = "1.0.0"

