"""Local, deterministic VAD frame-decision pipeline with incremental calibration.

Pipeline nodes: features -> noise -> decision -> segments.
Every node artifact is content-addressed on disk, calibration parameter changes
invalidate only the affected downstream suffix, and every frame decision /
segment conclusion carries a replayable lineage record.
"""

from .config import VadConfig, validate_config
from .errors import VadError
from .cache import ArtifactCache
from .pipeline import Pipeline, affected_nodes, NODE_ORDER

__all__ = [
    "VadConfig",
    "validate_config",
    "VadError",
    "ArtifactCache",
    "Pipeline",
    "affected_nodes",
    "NODE_ORDER",
]
