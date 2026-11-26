"""Deterministic, blockwise FlashAttention diagnostics."""

from .engine import FlashAttentionDiagnostics
from .errors import DiagnosticError
from .ledger import SnapshotLedger
from .models import ResourcePolicy, TaskSpec
from .samples import generate_samples

__all__ = [
    "DiagnosticError",
    "FlashAttentionDiagnostics",
    "ResourcePolicy",
    "SnapshotLedger",
    "TaskSpec",
    "generate_samples",
]

__version__ = "0.1.0"

