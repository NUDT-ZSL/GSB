"""Deterministic chunked-attention operator component.

Public surface:
    ChunkedAttentionOperator -- integrable domain component (requirement 8)
    StabilityConfig          -- numerical stability configuration
    BizError                 -- structured business error
"""
from .api import ChunkedAttentionOperator
from .config import StabilityConfig
from .errors import BizError

__all__ = ["ChunkedAttentionOperator", "StabilityConfig", "BizError"]
__version__ = "1.0.0"
