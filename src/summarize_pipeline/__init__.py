"""Deterministic extractive summarization with precision error attribution."""

from .config import load_config
from .pipeline import run_all_precisions
from .snapshots import SnapshotStore, build_snapshot
from .validator import validate_report, verify_reproducibility

__all__ = [
    "SnapshotStore",
    "build_snapshot",
    "load_config",
    "run_all_precisions",
    "validate_report",
    "verify_reproducibility",
]

__version__ = "1.0.0"

