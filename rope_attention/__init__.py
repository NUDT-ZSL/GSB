"""Deterministic long-context RoPE attention component."""

from .attention import (
    BlockedAttentionResult,
    FullAttentionResult,
    NumericTolerance,
    blocked_attention,
    compare_blocked_to_full,
    full_attention,
    online_softmax,
)
from .cache import CacheStore, NodeCacheResult
from .config import parse_config
from .component import RopeAttentionComponent
from .engine import (
    AttentionEngine,
    EngineResult,
    assert_results_byte_identical,
    plan_invalidation,
    retarget_tasks,
)
from .errors import RopeError
from .migration import MigrationResult, migrate
from .models import FrequencyAdjustment, ParsedConfig, ResourceLimits
from .rope import compute_position_angles, rotate_qk
from .snapshot import ConfigSnapshot, SnapshotStore

__all__ = [
    "AttentionEngine",
    "BlockedAttentionResult",
    "CacheStore",
    "ConfigSnapshot",
    "EngineResult",
    "FrequencyAdjustment",
    "FullAttentionResult",
    "MigrationResult",
    "NodeCacheResult",
    "NumericTolerance",
    "ParsedConfig",
    "ResourceLimits",
    "RopeError",
    "RopeAttentionComponent",
    "SnapshotStore",
    "assert_results_byte_identical",
    "blocked_attention",
    "compare_blocked_to_full",
    "compute_position_angles",
    "full_attention",
    "migrate",
    "online_softmax",
    "parse_config",
    "plan_invalidation",
    "retarget_tasks",
    "rotate_qk",
]

