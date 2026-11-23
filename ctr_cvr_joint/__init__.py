"""Deterministic CTR/CVR joint estimation toolkit.

The package intentionally has no network-facing code.  Inputs are local JSONL /
Parquet samples, local feature specifications and local statistics snapshots.
"""

from .errors import BusinessError, E_CODES
from .models import (
    CategoricalFeature,
    CrossFeature,
    FeatureSpec,
    NumericFeature,
    Sample,
    StatSpec,
    ThresholdConfig,
)
from .specs import LoadedSpec, load_spec, load_spec_bytes, normalize_spec, serialize_spec
from .migration import MigrationRecord, MigrationResult, migrate_spec, replay_migration
from .engine import JointCTREngine, EngineConfig, RunReport
from .assembly import AssemblyResult, AssembledRow, assemble_features
from .scoring import ScoreResult, score_samples
from .statistics import LoadedStatistics, load_statistics
from .samples import load_samples_jsonl, load_samples_parquet, write_samples_jsonl
from .cache import ContentAddressedCache, StageLock
from .snapshots import SnapshotRegistry

__all__ = [
    "BusinessError",
    "E_CODES",
    "CategoricalFeature",
    "CrossFeature",
    "FeatureSpec",
    "NumericFeature",
    "Sample",
    "StatSpec",
    "ThresholdConfig",
    "LoadedSpec",
    "load_spec",
    "load_spec_bytes",
    "normalize_spec",
    "serialize_spec",
    "migrate_spec",
    "replay_migration",
    "MigrationRecord",
    "MigrationResult",
    "JointCTREngine",
    "EngineConfig",
    "RunReport",
    "AssemblyResult",
    "AssembledRow",
    "assemble_features",
    "ScoreResult",
    "score_samples",
    "LoadedStatistics",
    "load_statistics",
    "load_samples_jsonl",
    "load_samples_parquet",
    "write_samples_jsonl",
    "ContentAddressedCache",
    "StageLock",
    "SnapshotRegistry",
]

__version__ = "0.1.0"

