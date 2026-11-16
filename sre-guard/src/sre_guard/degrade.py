"""降级链决策与误差沿链传播。

确定性权重选择
==============
选择器只依赖整数输入（事件序号、时间戳、服务标识长度、权重），
按降级链**数组顺序**累计权重区间并用整数取模命中，不依赖 dict/hash 遍历顺序，
同一输入逐位复现。

误差沿链传播
============
每级成功概率以定点值参与一次 :func:`mul_fixed`，floor 截断误差上界 1 ulp；
若该级概率由窗口计数现场量化，则另计 1 ulp 输入量化误差。
保守线性合成::

    e_m = e_{m-1} + input_quant_m + 1

当某级累计误差区间可能改变结论（误差 >= 端到端成功概率与参考边界的最小距离）
时，标注 ``error_dominated=True``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .errors import E_DEGRADE, GovernanceError, Issue
from .fixedpoint import (
    Grid,
    LevelContribution,
    compose_chain_probability,
    format_fixed,
)
from .models import DegradeTarget

#: 误差主导判定的参考边界（端到端成功概率 50% 的定点位置）
DOMINANCE_REFERENCE_PERMILLE = 500


@dataclass
class DegradeLevel:
    level: int
    selector: int
    weight_sum: int
    chosen_target: str | None
    available: bool
    success_fixed: int
    success_decimal: str
    input_quant_ulp: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "selector": self.selector,
            "weight_sum": self.weight_sum,
            "chosen_target": self.chosen_target,
            "available": self.available,
            "success_fixed": self.success_fixed,
            "success_decimal": self.success_decimal,
            "input_quantization_error_ulp": self.input_quant_ulp,
        }


@dataclass
class DegradeDecision:
    service_id: str
    event_index: int
    selected_target: str
    levels: list[DegradeLevel]
    end_to_end_success_fixed: int
    end_to_end_success_decimal: str
    error_trace: list[LevelContribution]
    total_error_ulp: int
    error_dominated: bool
    dominance_gap_ulp: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "event_index": self.event_index,
            "selected_target": self.selected_target,
            "levels": [level.to_dict() for level in self.levels],
            "end_to_end_success_fixed": self.end_to_end_success_fixed,
            "end_to_end_success_decimal": self.end_to_end_success_decimal,
            "error_trace": [contrib.to_dict() for contrib in self.error_trace],
            "total_error_bound_ulp": self.total_error_ulp,
            "error_dominated": self.error_dominated,
            "dominance_gap_ulp": self.dominance_gap_ulp,
        }


def deterministic_selector(service_id: str, event_index: int, timestamp_ms: int,
                           level: int) -> int:
    """仅依赖整数与字符串长度的确定性选择器（无 hash/dict 顺序）。"""
    return (event_index * 1_000_003 + timestamp_ms + len(service_id) * 97 + level * 31)


def choose_degrade(
    service_id: str,
    event_index: int,
    timestamp_ms: int,
    chain: list[DegradeTarget],
    grid: Grid,
    is_available: Callable[[str], bool],
    observed_success: Callable[[str], tuple[int, int] | None],
) -> DegradeDecision:
    """执行降级链；权重和为零或链条耗尽抛 E_DEGRADE，绝不静默放行。"""
    remaining: list[DegradeTarget] = list(chain)
    level_records: list[DegradeLevel] = []
    probability_steps: list[tuple[str, int, int]] = []
    selected_target: str | None = None
    level = 0

    while remaining:
        level += 1
        weight_sum = sum(target.weight for target in remaining)
        if weight_sum <= 0:
            raise GovernanceError(
                E_DEGRADE,
                f"服务 {service_id} 降级链剩余权重和为零，无法选择降级目标",
                [Issue(f"degrade_chain.level{level}", "权重和非正", value=weight_sum)],
                {"service_id": service_id, "event_index": event_index},
            )
        selector = deterministic_selector(service_id, event_index, timestamp_ms, level)
        slot = selector % weight_sum
        cumulative = 0
        chosen: DegradeTarget | None = None
        chosen_index: int | None = None
        # 严格按数组顺序累计，确定性命中
        for index, target in enumerate(remaining):
            cumulative += target.weight
            if slot < cumulative:
                chosen = target
                chosen_index = index
                break
        available = chosen is not None and is_available(chosen.target)
        if chosen is not None and available:
            observed = observed_success(chosen.target)
            if observed is not None:
                success_fixed, input_quant = observed
            else:
                # 配置的千分比 -> 网格定点值（整数换算，无截断损失时量化误差为 0）
                success_fixed = chosen.success_permille * grid.scale // 1000
                input_quant = 0
            level_records.append(DegradeLevel(
                level=level, selector=selector, weight_sum=weight_sum,
                chosen_target=chosen.target, available=True,
                success_fixed=success_fixed,
                success_decimal=format_fixed(success_fixed, grid),
                input_quant_ulp=input_quant,
            ))
            probability_steps.append((chosen.target, success_fixed, input_quant))
            selected_target = chosen.target
            break
        # 该级目标不可用：记录后按链条顺序移除，进入下一级试探
        level_records.append(DegradeLevel(
            level=level, selector=selector, weight_sum=weight_sum,
            chosen_target=None if chosen is None else chosen.target,
            available=False, success_fixed=0,
            success_decimal=format_fixed(0, grid), input_quant_ulp=0,
        ))
        if chosen_index is not None:
            remaining.pop(chosen_index)

    if selected_target is None:
        raise GovernanceError(
            E_DEGRADE,
            f"服务 {service_id} 降级链耗尽，无可用降级目标",
            [Issue("degrade_chain", "全部目标不可用")],
            {"service_id": service_id, "event_index": event_index,
             "levels_attempted": level},
        )

    end_to_end, trace = compose_chain_probability(probability_steps, grid)
    total_error = trace.total_error_ulp
    reference = DOMINANCE_REFERENCE_PERMILLE * grid.scale // 1000
    gap = abs(end_to_end - reference)
    # 误差界主导：误差区间跨越参考边界，结论可能被量化误差翻转
    error_dominated = total_error >= gap if gap > 0 else True

    return DegradeDecision(
        service_id=service_id,
        event_index=event_index,
        selected_target=selected_target,
        levels=level_records,
        end_to_end_success_fixed=end_to_end,
        end_to_end_success_decimal=format_fixed(end_to_end, grid),
        error_trace=trace.levels,
        total_error_ulp=total_error,
        error_dominated=error_dominated,
        dominance_gap_ulp=gap,
    )
