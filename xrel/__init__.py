"""xrel: local cross-lingual relevance precision and snapshot diagnostics."""

from .config import AppConfig, Bm25Config, ExpandConfig, ResourceLimits, ThresholdConfig, default_config
from .errors import XrelError
from .model import BridgeResource, Document, Query
from .numerics import FixedConfig, RoundingMode
from .pipeline import XrelPipeline

__all__ = [
    "AppConfig",
    "Bm25Config",
    "ExpandConfig",
    "ResourceLimits",
    "ThresholdConfig",
    "default_config",
    "XrelError",
    "BridgeResource",
    "Document",
    "Query",
    "FixedConfig",
    "RoundingMode",
    "XrelPipeline",
]
