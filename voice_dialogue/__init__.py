"""Voice emotion & dialogue understanding domain component.

Pure-Python + NumPy, fully deterministic. No external audio files, model
weights or online services are required.
"""

from .errors import BusinessError, ErrorCode
from .config import ComponentConfig, PathProfile, ResourceLimits
from .features import extract_features
from .diarization import separate_speakers
from .emotion import score_segments, aggregate_dialogue
from .selector import observe_scale, decide_path, PathDecision
from .pipeline import run_pipeline

__all__ = [
    "BusinessError",
    "ErrorCode",
    "ComponentConfig",
    "PathProfile",
    "ResourceLimits",
    "extract_features",
    "separate_speakers",
    "score_segments",
    "aggregate_dialogue",
    "observe_scale",
    "decide_path",
    "PathDecision",
    "run_pipeline",
]

__version__ = "1.0.0"
