"""blocksnap: local deterministic block-volume snapshot & rollback component.

Public package API for integration. All domain failures raise a
:class:`~blocksnap.errors.BlockSnapError` subclass carrying one of the
business codes E_VOLUME / E_SNAPSHOT / E_INTEGRITY / E_CHAIN.
"""
from .errors import (
    BlockSnapError,
    ChainError,
    IntegrityError,
    SnapshotError,
    VolumeError,
)
from .volume import Volume
from .manager import SnapshotManager, open_store
from .rollback import rollback, RollbackEngine
from .manifest import Manifest, SnapshotEntry, BlockRef, load_manifest, save_manifest
from .audit import run_audit
from .journal import Journal, JournalState
from .logging_utils import query_logs

__all__ = [
    "Volume",
    "SnapshotManager",
    "open_store",
    "rollback",
    "RollbackEngine",
    "Manifest",
    "SnapshotEntry",
    "BlockRef",
    "load_manifest",
    "save_manifest",
    "run_audit",
    "Journal",
    "JournalState",
    "BlockSnapError",
    "VolumeError",
    "SnapshotError",
    "IntegrityError",
    "ChainError",
    "query_logs",
]

__version__ = "1.0.0"
