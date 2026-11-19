"""Deterministic, cache-aware FlashAttention-style block attention."""

from .cache import CacheDiagnostics, CacheStore
from .engine import (
    AttentionResult,
    BatchResult,
    FlashAttentionEngine,
    IncrementalResult,
    SelfCheckReport,
)
from .errors import (
    BusinessError,
    CacheCorruptError,
    ErrorCode,
    Issue,
    ResourceLimitError,
)
from .models import AttentionCase, QKVArrays, RuntimeConfig, SoftmaxConfig
from .reference import dense_attention_reference

__all__ = [
    "AttentionCase",
    "AttentionResult",
    "BatchResult",
    "BusinessError",
    "CacheDiagnostics",
    "CacheCorruptError",
    "CacheStore",
    "ErrorCode",
    "FlashAttentionEngine",
    "IncrementalResult",
    "Issue",
    "QKVArrays",
    "ResourceLimitError",
    "RuntimeConfig",
    "SelfCheckReport",
    "SoftmaxConfig",
    "dense_attention_reference",
]
