"""Deterministic timbre-cloning and pronunciation-alignment error laboratory."""

from . import api
from .api import *
from .component import TimbreCloneComponent
from .config import FrameConfig, PipelineConfig, PrecisionConfig, SimilarityConfig
from .errors import DomainError

__all__ = sorted(
    set(api.__all__)
    | {
    "DomainError",
    "FrameConfig",
    "PipelineConfig",
    "PrecisionConfig",
    "SimilarityConfig",
    "TimbreCloneComponent",
    }
)

__version__ = "0.1.0"

