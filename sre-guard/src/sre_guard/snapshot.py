"""会话快照：整体持久化、整体替换恢复。"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any

from .breaker import CircuitBreaker
from .engine import GovernanceEngine
from .errors import E_GRID, E_POLICY, E_REPLAY, GovernanceError, Issue
from .fixedpoint import Grid
from .models import Policy
from .policy import diff_policies
from .ratelimiter import TokenBucket

SNAPSHOT_SCHEMA = "sre-guard/snapshot/v1"


@dataclass
class RestoreResult:
    engine: GovernanceEngine
    version_replaced: bool
    policy_diffs: list[dict[str, Any]]
    snapshot_version: int
    current_version: int


def _atomic_write(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def save_snapshot(path: str, engine: GovernanceEngine, events: list) -> None:
    """持久化完整会话：策略、网格、限流/熔断状态、逐事件轨迹与误差界。"""
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "policy": engine.policy.to_dict(),
        "grid": engine.grid.to_dict(),
        "events": [event.to_dict() for event in events],
        "state": engine.state_snapshot(),
        "decisions": [record.to_dict() for record in engine.decisions],
        "decision_count": len(engine.decisions),
    }
    _atomic_write(path, payload)


def load_snapshot(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _policy_from_dict(data: dict[str, Any]) -> Policy:
    from .models import (
        BreakerPolicy,
        DegradeTarget,
        RateLimitPolicy,
        ServicePolicy,
    )
    services: dict[str, ServicePolicy] = {}
    for service_id, raw in data["services"].items():
        rl = RateLimitPolicy(**raw["rate_limit"])
        cb = BreakerPolicy(**raw["breaker"])
        chain = [DegradeTarget(**item) for item in raw.get("degrade_chain", [])]
        services[service_id] = ServicePolicy(
            service_id=service_id, rate_limit=rl, breaker=cb, degrade_chain=chain)
    return Policy(version=data["version"], services=services)


def restore_snapshot(path: str, current_policy: Policy) -> RestoreResult:
    """恢复快照；版本不一致时整体替换限流/熔断状态，绝不增量合并。"""
    data = load_snapshot(path)
    grid_data = data.get("grid")
    if not isinstance(grid_data, dict) or "exponent" not in grid_data:
        raise GovernanceError(
            E_GRID,
            "快照缺少定点缩放因子 grid.exponent，无法安全恢复",
            [Issue("grid.exponent", "字段缺失", line=None)],
            {"snapshot_path": path},
        )
    exponent = grid_data["exponent"]
    if not isinstance(exponent, int) or isinstance(exponent, bool) or exponent < 0:
        raise GovernanceError(
            E_GRID, "快照缩放指数非法",
            [Issue("grid.exponent", "指数非法", value=exponent)])
    grid = Grid.from_exponent(exponent)

    policy_data = data.get("policy")
    if not isinstance(policy_data, dict) or "version" not in policy_data:
        raise GovernanceError(
            E_REPLAY, "快照缺少策略版本，无法复现历史决策",
            [Issue("policy.version", "字段缺失")],
            {"snapshot_path": path},
        )
    try:
        snapshot_policy = _policy_from_dict(policy_data)
    except (KeyError, TypeError) as exc:
        raise GovernanceError(
            E_POLICY, f"快照内嵌策略结构损坏: {exc}",
            [Issue("policy", str(exc))]) from exc

    version_replaced = snapshot_policy.version != current_policy.version
    diffs = diff_policies(snapshot_policy, current_policy) if version_replaced else []

    # 以快照内嵌策略 + 快照网格重建引擎，保证重放可复现
    engine = GovernanceEngine(policy=snapshot_policy, grid=grid)
    state = data.get("state", {})
    for service_id, bucket_data in state.get("rate_limiters", {}).items():
        if service_id in engine.buckets:
            engine.buckets[service_id] = TokenBucket.restore(bucket_data, grid)
    for service_id, breaker_data in state.get("breakers", {}).items():
        if service_id in engine.breakers:
            service_policy = snapshot_policy.services[service_id]
            engine.breakers[service_id] = CircuitBreaker.restore(
                breaker_data, service_policy.breaker, grid)
    engine.decisions = []  # 轨迹由重放模块重建，避免直接信任旧轨迹
    return RestoreResult(
        engine=engine,
        version_replaced=version_replaced,
        policy_diffs=diffs,
        snapshot_version=snapshot_policy.version,
        current_version=current_policy.version,
    )
