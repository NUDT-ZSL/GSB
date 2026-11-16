"""历史决策重放校验与不可复现报告。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .engine import REPLAY_FIELDS, GovernanceEngine
from .errors import E_GRID, E_REPLAY, GovernanceError, Issue
from .events import _parse_event  # 复用严格解析
from .fixedpoint import Grid
from .models import Event
from .snapshot import load_snapshot


@dataclass
class ReplayReport:
    matched: bool
    events_compared: int
    first_mismatch_index: int | None = None
    mismatched_fields: list[str] = field(default_factory=list)
    mismatch_detail: dict[str, Any] | None = None
    grid_exponent: int = 0
    policy_version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "events_compared": self.events_compared,
            "first_mismatch_index": self.first_mismatch_index,
            "mismatched_fields": self.mismatched_fields,
            "mismatch_detail": self.mismatch_detail,
            "grid_exponent": self.grid_exponent,
            "policy_version": self.policy_version,
        }


def _events_from_snapshot(raw_events: list[dict[str, Any]]) -> list[Event]:
    events: list[Event] = []
    for offset, payload in enumerate(raw_events, start=1):
        event, issues = _parse_event(payload, offset)
        if issues:
            raise GovernanceError(
                E_REPLAY, "快照内嵌事件无法解析",
                [Issue(f"events[{offset - 1}]", issue.message, line=offset) for issue in issues])
        events.append(event)
    return events


def replay_snapshot(path: str, expected_grid_exponent: int | None = None) -> ReplayReport:
    """按快照记录的网格与策略重放全部事件并逐项比对决策轨迹。"""
    data = load_snapshot(path)
    grid_data = data.get("grid")
    if not isinstance(grid_data, dict) or "exponent" not in grid_data:
        raise GovernanceError(
            E_GRID, "快照缺少定点缩放因子，重放被拒绝",
            [Issue("grid.exponent", "字段缺失")],
            {"snapshot_path": path})
    exponent = grid_data["exponent"]
    if expected_grid_exponent is not None and exponent != expected_grid_exponent:
        raise GovernanceError(
            E_REPLAY,
            f"缩放因子变化导致不可复现：快照 exponent={exponent}，当前 exponent={expected_grid_exponent}",
            [Issue("grid.exponent", "缩放因子不一致",
                   value={"snapshot": exponent, "current": expected_grid_exponent})])
    policy_data = data.get("policy")
    if not isinstance(policy_data, dict) or "version" not in policy_data:
        raise GovernanceError(
            E_REPLAY, "策略版本缺失导致不可复现",
            [Issue("policy.version", "字段缺失")])

    from .snapshot import _policy_from_dict
    policy = _policy_from_dict(policy_data)
    grid = Grid.from_exponent(exponent)
    events = _events_from_snapshot(data.get("events", []))

    # 先恢复快照断点状态，再从断点继续重放全部事件（冷引擎等价重建后逐事件推进）
    fresh = GovernanceEngine(policy=policy, grid=grid)
    stored_decisions = data.get("decisions", [])

    compared = 0
    for index, event in enumerate(events):
        record = fresh.process_event(event, index)
        if index >= len(stored_decisions):
            return ReplayReport(
                matched=False, events_compared=compared,
                first_mismatch_index=index, mismatched_fields=["$"],
                mismatch_detail={"reason": "快照轨迹缺少该事件",
                                 "event_index": index},
                grid_exponent=exponent, policy_version=policy.version)
        stored = stored_decisions[index]
        actual = record.to_dict()
        mismatched = [field_name for field_name in REPLAY_FIELDS
                      if actual.get(field_name) != stored.get(field_name)]
        compared += 1
        if mismatched:
            return ReplayReport(
                matched=False, events_compared=compared,
                first_mismatch_index=index, mismatched_fields=mismatched,
                mismatch_detail={
                    "event_index": index,
                    "event": event.to_dict(),
                    "fixed_point_inputs": {
                        "grid": grid.to_dict(),
                        "tokens_before_grid": actual["rate_limit"].get("tokens_before_grid"),
                        "cost_grid": actual["rate_limit"].get("cost_grid"),
                        "policy_version": policy.version,
                    },
                    "snapshot_output": {key: stored.get(key) for key in REPLAY_FIELDS},
                    "replay_output": {key: actual.get(key) for key in REPLAY_FIELDS},
                },
                grid_exponent=exponent, policy_version=policy.version)
    return ReplayReport(matched=True, events_compared=compared,
                        grid_exponent=exponent, policy_version=policy.version)
