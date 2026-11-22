"""Deterministic local video-prediction pipeline.

The public API intentionally uses plain Python dictionaries and JSON-safe
values so checkpoints can be compared byte-for-byte across processes.
"""

from .config import PipelineConfig
from .dataset import generate_dataset, load_dataset
from .errors import BusinessError
from .pipeline import Pipeline, RunOutcome
from .snapshots import SnapshotStore
from .verification import compare_recovery, verify_checkpoint, verify_final_result

__all__ = [
    "BusinessError",
    "Pipeline",
    "PipelineConfig",
    "RunOutcome",
    "SnapshotStore",
    "compare_recovery",
    "generate_dataset",
    "load_dataset",
    "verify_checkpoint",
    "verify_final_result",
]
