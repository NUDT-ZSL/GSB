"""Deterministic detection-candidate match verification component.

Pipeline: ingest/validate -> per-class greedy assignment -> dual-version
ledger (v1 JSON / v2 shards+SQLite) -> snapshots -> replay -> self-check.
"""
from .component import Pipeline, SimulatedCrash
from .config import Config, CONTROLLED_CLASSES
from .errors import (
    BusinessError,
    E_BAD_BOX,
    E_CONFIG_INVALID,
    E_DUP_IMAGE,
    E_FORMAT_CORRUPT,
    E_INCONSISTENT,
    E_MIGRATION_MISMATCH,
    E_REPLAY_MISMATCH,
    E_RESOURCE_LIMIT,
    E_REVISION_REGRESS,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_THRESHOLD_RANGE,
    E_VERSION_UNSUPPORTED,
)

__all__ = [
    "Pipeline",
    "SimulatedCrash",
    "Config",
    "CONTROLLED_CLASSES",
    "BusinessError",
    "E_BAD_BOX",
    "E_CONFIG_INVALID",
    "E_DUP_IMAGE",
    "E_FORMAT_CORRUPT",
    "E_INCONSISTENT",
    "E_MIGRATION_MISMATCH",
    "E_REPLAY_MISMATCH",
    "E_RESOURCE_LIMIT",
    "E_REVISION_REGRESS",
    "E_SNAPSHOT_CORRUPT",
    "E_SNAPSHOT_ROLLBACK",
    "E_STAGE_BUSY",
    "E_THRESHOLD_RANGE",
    "E_VERSION_UNSUPPORTED",
]
