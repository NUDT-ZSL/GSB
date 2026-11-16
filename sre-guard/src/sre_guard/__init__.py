"""sre_guard：本地 SRE 限流、熔断与降级决策领域组件。

公共 API 覆盖策略/事件加载、定点量化、限流推进、熔断更新、降级决策、
误差界合成、快照保存恢复与重放校验。
"""
from __future__ import annotations

from .breaker import CircuitBreaker, MinuteBucket
from .degrade import choose_degrade, deterministic_selector
from .engine import GovernanceEngine
from .errors import (
    E_DEGRADE,
    E_EVENT,
    E_GRID,
    E_POLICY,
    E_REPLAY,
    GovernanceError,
    Issue,
)
from .events import load_events
from .fixedpoint import (
    Grid,
    compare_percent,
    compose_chain_probability,
    format_fixed,
    mul_fixed,
    quantize,
)
from .policy import bump_version, diff_policies, load_policy
from .ratelimiter import TokenBucket
from .replay import ReplayReport, replay_snapshot
from .snapshot import RestoreResult, restore_snapshot, save_snapshot

__version__ = "1.0.0"

__all__ = [
    "Grid",
    "GovernanceEngine",
    "GovernanceError",
    "Issue",
    "MinuteBucket",
    "CircuitBreaker",
    "TokenBucket",
    "ReplayReport",
    "RestoreResult",
    "E_POLICY",
    "E_EVENT",
    "E_GRID",
    "E_DEGRADE",
    "E_REPLAY",
    "load_policy",
    "load_events",
    "bump_version",
    "diff_policies",
    "quantize",
    "mul_fixed",
    "compare_percent",
    "compose_chain_probability",
    "format_fixed",
    "deterministic_selector",
    "choose_degrade",
    "save_snapshot",
    "restore_snapshot",
    "replay_snapshot",
]
