"""Deterministic dialog speech emotion pipeline with incremental provenance."""

from .cache import ConfigSnapshotStore, ContentAddressedCache
from .config import PipelineConfig
from .errors import BusinessError
from .lineage import LineageStore
from .pipeline import (
    AppendRegistration,
    CorrectTruth,
    IncrementalPipeline,
    RunResult,
    SetFrameShift,
    SetSegmentThreshold,
    compare_runs,
)
from .samples import (
    DialogInput,
    RegistrationSample,
    SampleBundle,
    generate_bundle,
    load_bundle,
    write_bundle,
)
from .truth import TruthStore

__all__ = [
    "BusinessError",
    "AppendRegistration",
    "ConfigSnapshotStore",
    "ContentAddressedCache",
    "CorrectTruth",
    "DialogInput",
    "IncrementalPipeline",
    "LineageStore",
    "PipelineConfig",
    "RegistrationSample",
    "RunResult",
    "SampleBundle",
    "TruthStore",
    "SetFrameShift",
    "SetSegmentThreshold",
    "compare_runs",
    "generate_bundle",
    "load_bundle",
    "write_bundle",
]
