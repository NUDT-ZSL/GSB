"""读取延迟装配与误差上界累计。

每次读取的耗时由分片摘要校验、拼接与缓存查找各环节的定点量累加得到。
每步换算与加法都累计误差上界；当实际耗时与预算之差小于比对容差、
结论依赖该差值时，标记为精度敏感并输出误差来源链。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from .fixedpoint import FixedAccumulator, to_fixed


@dataclass
class LatencyReport:
    stages: List[dict]                 # 各环节换算记录
    total_fixed: int                   # 定点总耗时
    total_ms: float                    # 换算回毫秒
    true_total_ms: float               # 含逐项余量的真值（毫秒）
    error_bound_ms: float              # 累计误差上界（毫秒）
    budget_ms: float
    budget_fixed: int
    within_budget: bool
    precision_sensitive: bool          # 结论依赖小于容差的差值
    error_budget_exceeded: bool        # 累计误差上界超出预算
    error_chain: List[dict] = field(default_factory=list)  # 环节名/余量/累加位置

    def to_dict(self):
        return {
            "stages": self.stages,
            "total_fixed": self.total_fixed,
            "total_ms": self.total_ms,
            "true_total_ms": self.true_total_ms,
            "error_bound_ms": self.error_bound_ms,
            "budget_ms": self.budget_ms,
            "within_budget": self.within_budget,
            "precision_sensitive": self.precision_sensitive,
            "error_budget_exceeded": self.error_budget_exceeded,
            "error_chain": self.error_chain,
        }


def assemble_latency(stage_values_ms: List[Tuple[str, float]], config) -> LatencyReport:
    """把各环节毫秒浮点值按声明标度装配为定点总量并给出诊断。"""
    scale = config.fixed_scale
    acc = FixedAccumulator(scale=scale)
    for name, value_ms in stage_values_ms:
        acc.add(to_fixed(value_ms, scale, stage=name))
    budget_rec = to_fixed(config.read_latency_budget_ms, scale, stage="budget")
    tolerance_rec = to_fixed(config.compare_tolerance, scale, stage="tolerance")
    diff_fixed = abs(acc.total_fixed - budget_rec.fixed)
    # 结论依赖差值：within_budget 的判定恰好落在容差带内
    sensitive = diff_fixed < tolerance_rec.fixed
    error_bound_ms = float(acc.error_bound_units())
    return LatencyReport(
        stages=[rec.to_dict() for rec in (step.record for step in acc.steps)],
        total_fixed=acc.total_fixed,
        total_ms=float(acc.total_units()),
        true_total_ms=float(acc.true_scaled / scale),
        error_bound_ms=error_bound_ms,
        budget_ms=config.read_latency_budget_ms,
        budget_fixed=budget_rec.fixed,
        within_budget=acc.total_fixed <= budget_rec.fixed,
        precision_sensitive=sensitive,
        error_budget_exceeded=error_bound_ms > config.error_bound_budget,
        error_chain=acc.error_chain() if sensitive else [],
    )
