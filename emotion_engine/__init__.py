"""Deterministic local emotion-tier engine for synthetic contact-center dialogs."""

from .config import EngineConfig, load_config
from .errors import EngineError
from .aggregation import assert_stream_batch_match, build_view, first_frame_deviation
from .batch import OfflineEngine
from .cache import ArtifactCache
from .snapshot import Snapshot, SnapshotStore
from .streaming import StreamingEngine
from .synth import generate_samples

__all__ = [
    "ArtifactCache",
    "EngineConfig",
    "EngineError",
    "OfflineEngine",
    "Snapshot",
    "SnapshotStore",
    "StreamingEngine",
    "assert_stream_batch_match",
    "build_view",
    "first_frame_deviation",
    "generate_samples",
    "load_config",
]
__version__ = "1.0.0"
