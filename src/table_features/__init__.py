"""Deterministic tabular feature encoding and selection."""

from .config import FeatureConfig, load_config, parse_config
from .cache import ContentAddressedCache
from .data import generate_rows, load_jsonl, write_jsonl
from .errors import BusinessError, ErrorRecord
from .injection import InjectionConfig, load_injection_config, parse_injection_config
from .pipeline import (
    IncrementalRun,
    InvalidationPlan,
    Pipeline,
    PipelineResult,
    compute_invalidation,
    run_pipeline,
    write_result_artifacts,
)
from .snapshots import SnapshotStore

__all__ = [
    "BusinessError",
    "ContentAddressedCache",
    "ErrorRecord",
    "FeatureConfig",
    "IncrementalRun",
    "InjectionConfig",
    "InvalidationPlan",
    "Pipeline",
    "PipelineResult",
    "SnapshotStore",
    "compute_invalidation",
    "generate_rows",
    "load_config",
    "load_injection_config",
    "load_jsonl",
    "parse_injection_config",
    "parse_config",
    "run_pipeline",
    "write_jsonl",
    "write_result_artifacts",
]

