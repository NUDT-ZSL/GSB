"""Local deterministic RoPE variant arbitration component."""

from .attention import (
    compare_variant_scores,
    compute_block_scores,
    compute_online_normalization,
    estimate_attention_memory,
)
from .cache import ContentAddressedCache
from .diagnostics import run_self_check
from .engine import ArbiterEngine, EngineLimits, NODE_NAMES
from .errors import DomainError
from .models import (
    CaseSpec,
    CommitReceipt,
    SnapshotStage,
    SnapshotStore,
    VariantSnapshot,
)
from .variants import (
    PAIRING_CONVENTION,
    MappingRegistry,
    adjudicate_angles,
    build_builtin_mapping_registry,
    compute_angles,
    generate_qkv,
    rotate_qk,
)

__all__ = [
    "ArbiterEngine",
    "CaseSpec",
    "CommitReceipt",
    "ContentAddressedCache",
    "DomainError",
    "EngineLimits",
    "MappingRegistry",
    "NODE_NAMES",
    "PAIRING_CONVENTION",
    "SnapshotStage",
    "SnapshotStore",
    "VariantSnapshot",
    "adjudicate_angles",
    "build_builtin_mapping_registry",
    "compare_variant_scores",
    "compute_angles",
    "compute_block_scores",
    "compute_online_normalization",
    "estimate_attention_memory",
    "generate_qkv",
    "rotate_qk",
    "run_self_check",
]
