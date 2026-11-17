"""SLPGuard: deterministic SLP state-machine vulnerability verification."""

from .analyzer import (
    analyze_bytes,
    analyze_path,
    judge,
    transition_table,
    verify_truth,
)
from .batch import BatchConfig, discover_files, normalized_results, run_batch
from .checkpoint import CheckpointState, CheckpointStore, FileLock
from .errors import ErrorCode, ResourceStop, SLPError, mask_for
from .injector import InjectionResult, InjectionSpec, inject
from .models import (
    FileResult,
    JudgeConfig,
    ResourceLimits,
    SessionVerdict,
    TruthSummary,
    Violation,
)
from .protocol import (
    CHECKPOINT_SCHEMA_VERSION,
    FILE_MAGIC,
    PHASE_TABLE_VERSION,
    RECORD_MAGIC,
    Direction,
    Extension,
    ParsedSLP,
    Phase,
    SLPRecord,
    encode_file,
    parse_bytes,
    parse_file,
)

__all__ = [
    "BatchConfig",
    "CHECKPOINT_SCHEMA_VERSION",
    "CheckpointState",
    "CheckpointStore",
    "Direction",
    "ErrorCode",
    "Extension",
    "FILE_MAGIC",
    "FileLock",
    "FileResult",
    "InjectionResult",
    "InjectionSpec",
    "JudgeConfig",
    "PHASE_TABLE_VERSION",
    "ParsedSLP",
    "Phase",
    "RECORD_MAGIC",
    "ResourceLimits",
    "ResourceStop",
    "SLPError",
    "SLPRecord",
    "SessionVerdict",
    "TruthSummary",
    "Violation",
    "analyze_bytes",
    "analyze_path",
    "discover_files",
    "encode_file",
    "inject",
    "judge",
    "mask_for",
    "normalized_results",
    "parse_bytes",
    "parse_file",
    "run_batch",
    "transition_table",
    "verify_truth",
]

__version__ = "0.1.0"
