"""Integratable chunked incremental FlashAttention-style operator."""

from .config import EngineConfig
from .dependency import DependencyGraph
from .engine import IncrementalAttentionEngine
from .errors import (
    E_INVALID_CONFIG,
    E_KERNEL_NORMALIZE,
    E_KERNEL_SCORE,
    E_KERNEL_WEIGHTED,
    E_NON_FINITE_INPUT,
    E_OK,
    E_RECOMPUTE_PENDING,
    E_SNAPSHOT_STALE,
)
from .injection import InjectionConfig
from .kernel import attention_block
from .reference import reference_full

__all__ = [
    "EngineConfig",
    "DependencyGraph",
    "IncrementalAttentionEngine",
    "InjectionConfig",
    "attention_block",
    "reference_full",
]
