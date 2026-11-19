"""Deterministic Chinese intent configuration interoperability toolkit."""

from .errors import IntentError
from .matching import MatchBatch, ResourceLimits, match_document
from .migration import MigrationResult, migrate_document, prepare_for_matching
from .models import (
    DEFAULT_FALLBACK,
    DEFAULT_THRESHOLD,
    ConfigDocument,
    IntentSpec,
    MatchResult,
    QuerySample,
    SlotSpec,
)
from .snapshots import Snapshot, SnapshotStore

__all__ = [
    "DEFAULT_FALLBACK",
    "DEFAULT_THRESHOLD",
    "ConfigDocument",
    "IntentError",
    "IntentSpec",
    "MatchResult",
    "MatchBatch",
    "MigrationResult",
    "QuerySample",
    "ResourceLimits",
    "Snapshot",
    "SnapshotStore",
    "SlotSpec",
    "match_document",
    "migrate_document",
    "prepare_for_matching",
]

__version__ = "0.1.0"
