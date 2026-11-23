"""预算冲突的取舍裁决与三预算准入。

裁决显式建模三类冲突：

* 增大并发槽位缩短完成时间，但放大单波驻留缓存；
* 延长目标生成长度提升质量，但放大采样步数预算；
* 收紧 top-p 减少候选，但可能淘汰本应保留的 token。

对每个候选策略，在同一份观测（请求集合）上确定性地模拟「波次打包」准入，
再按固定、可复现的优先级字典选择最优策略。整个过程不使用随机源或时间源。

准入模型（确定性、可静态复算）：

* 请求按显式优先级排序后依次装入波次（wave）；
* 每个波次的并发数受 ``slot_budget`` 限制、波内驻留缓存受 ``cache_budget`` 限制；
* 采样步数为全局累计预算，受 ``step_budget`` 限制；
* 当前波次装不下的请求尝试开启下一波；无波次可装或步数超限时被拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .budget import (
    BudgetLimits,
    request_cache_entries,
    request_step_demand,
)
from .errors import E_CACHE_BUDGET, E_SLOT_BUDGET, E_STEP_BUDGET
from .models import Request, SamplingParams

VIOLATION_CODE = {
    "steps": E_STEP_BUDGET,
    "cache": E_CACHE_BUDGET,
    "slots": E_SLOT_BUDGET,
}
VIOLATION_FIELD = {
    "steps": "step_budget",
    "cache": "cache_budget",
    "slots": "slot_budget",
}


@dataclass(frozen=True)
class StrategyProfile:
    """一个可裁决的调度策略配置。"""

    name: str
    slot_budget: int
    cache_budget: int
    step_budget: int
    top_p_cap: float
    length_scale: float
    description: str

    def effective_params(self, params: SamplingParams) -> SamplingParams:
        return SamplingParams(
            temperature=params.temperature,
            top_k=params.top_k,
            top_p=min(params.top_p, self.top_p_cap),
        )

    def effective_length(self, request: Request) -> int:
        return max(1, int(round(request.target_length * self.length_scale)))


DEFAULT_PROFILES: tuple[StrategyProfile, ...] = (
    StrategyProfile(
        name="balanced",
        slot_budget=4,
        cache_budget=10_000,
        step_budget=10_000,
        top_p_cap=1.0,
        length_scale=1.0,
        description="基准策略：不收紧 top-p，不调整目标长度。",
    ),
    StrategyProfile(
        name="conservative",
        slot_budget=2,
        cache_budget=10_000,
        step_budget=10_000,
        top_p_cap=0.8,
        length_scale=0.75,
        description="保守策略：降低并发与目标长度，收紧 top-p，节省缓存与步数。",
    ),
    StrategyProfile(
        name="aggressive",
        slot_budget=8,
        cache_budget=10_000,
        step_budget=10_000,
        top_p_cap=1.0,
        length_scale=1.0,
        description="激进策略：提高并发槽位以缩短完成时间，缓存峰值随之放大。",
    ),
)


@dataclass
class RequestDecision:
    request_id: str
    admitted: bool
    code: str | None
    reason: str
    dimension: str | None
    priority: int
    target_length: int
    cache_entries: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "admitted": self.admitted,
            "code": self.code,
            "reason": self.reason,
            "dimension": self.dimension,
            "priority": self.priority,
            "target_length": self.target_length,
            "cache_entries": self.cache_entries,
        }


@dataclass
class StrategyEvaluation:
    profile: StrategyProfile
    decisions: list[RequestDecision]
    admitted_ids: list[str]
    rejected: list[RequestDecision]
    peak_cache: int
    peak_slots: int
    total_steps: int
    waves: list[list[str]] = field(default_factory=list)
    top_p_regrets: list[dict[str, Any]] = field(default_factory=list)

    def score(self) -> tuple[int, int, int, int]:
        """确定性裁决字典：准入越多越好，其次缓存/步数/槽位峰值越小越好。"""
        return (
            -len(self.admitted_ids),
            self.peak_cache,
            self.total_steps,
            self.peak_slots,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.name,
            "description": self.profile.description,
            "admitted_count": len(self.admitted_ids),
            "peak_cache": self.peak_cache,
            "peak_slots": self.peak_slots,
            "total_steps": self.total_steps,
            "waves": self.waves,
            "top_p_regrets": self.top_p_regrets,
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


def priority_order(requests: list[Request]) -> list[Request]:
    """显式优先级规则：priority 升序、目标长度升序、缓存规模升序、id 字典序。"""
    return sorted(
        requests,
        key=lambda req: (
            req.priority,
            req.target_length,
            request_cache_entries(req),
            req.request_id,
        ),
    )


def evaluate_profile(
    requests: list[Request],
    profile: StrategyProfile,
    base_limits: BudgetLimits,
    max_waves: int,
) -> StrategyEvaluation:
    """在给定策略下做波次打包准入，返回逐请求裁决与资源峰值。"""
    limits = BudgetLimits(
        step_budget=min(base_limits.step_budget, profile.step_budget),
        cache_budget=min(base_limits.cache_budget, profile.cache_budget),
        slot_budget=min(base_limits.slot_budget, profile.slot_budget),
    )
    decisions: list[RequestDecision] = []
    admitted_ids: list[str] = []
    rejected: list[RequestDecision] = []
    waves: list[list[str]] = [[]]
    wave_cache = 0
    wave_slots = 0
    peak_cache = 0
    total_steps = 0

    for request in priority_order(requests):
        effective_length = profile.effective_length(request)
        projected = Request(
            request_id=request.request_id,
            prompt_tokens=request.prompt_tokens,
            sampling_params=request.sampling_params,
            target_length=effective_length,
            seed=request.seed,
            priority=request.priority,
        )
        demand_steps = request_step_demand(projected)
        demand_cache = request_cache_entries(projected)

        # 全局步数预算优先裁决。
        if total_steps + demand_steps > limits.step_budget:
            decision = _reject(request, profile, "steps", effective_length, demand_cache)
            decisions.append(decision)
            rejected.append(decision)
            continue

        # 当前波次装不下则尝试开新波；新波仍装不下则按 cache/slots 拒绝。
        exceeds_slot = wave_slots + 1 > limits.slot_budget
        exceeds_cache = wave_cache + demand_cache > limits.cache_budget
        if exceeds_slot or exceeds_cache:
            # 当前波次已满：若还有波次预算则开新波，否则按受限维度拒绝。
            if len(waves) >= max_waves:
                dimension = "slots" if exceeds_slot else "cache"
                decision = _reject(
                    request, profile, dimension, effective_length, demand_cache
                )
                decisions.append(decision)
                rejected.append(decision)
                continue
            waves.append([])
            wave_cache = 0
            wave_slots = 0

        # 新波/当前波仍无法容纳单请求：属于硬性超限。
        if demand_cache > limits.cache_budget:
            decision = _reject(request, profile, "cache", effective_length, demand_cache)
            decisions.append(decision)
            rejected.append(decision)
            continue
        if 1 > limits.slot_budget:
            decision = _reject(request, profile, "slots", effective_length, demand_cache)
            decisions.append(decision)
            rejected.append(decision)
            continue

        # 落入当前（可能是新开的）波次。
        waves[-1].append(request.request_id)
        wave_slots += 1
        wave_cache += demand_cache
        peak_cache = max(peak_cache, wave_cache)
        total_steps += demand_steps
        admitted_ids.append(request.request_id)
        decisions.append(
            RequestDecision(
                request_id=request.request_id,
                admitted=True,
                code=None,
                reason=f"admitted under profile={profile.name}",
                dimension=None,
                priority=request.priority,
                target_length=effective_length,
                cache_entries=demand_cache,
            )
        )

    # 清理空波次并按请求 id 稳定化波内顺序（展示用）。
    waves = [sorted(wave) for wave in waves if wave]
    peak_slots = max((len(wave) for wave in waves), default=0)
    return StrategyEvaluation(
        profile=profile,
        decisions=decisions,
        admitted_ids=admitted_ids,
        rejected=rejected,
        peak_cache=peak_cache,
        peak_slots=peak_slots,
        total_steps=total_steps,
        waves=waves,
    )


def _reject(
    request: Request,
    profile: StrategyProfile,
    dimension: str,
    effective_length: int,
    demand_cache: int,
) -> RequestDecision:
    return RequestDecision(
        request_id=request.request_id,
        admitted=False,
        code=VIOLATION_CODE[dimension],
        reason=(
            f"rejected under profile={profile.name}: "
            f"{dimension} budget exceeded"
        ),
        dimension=dimension,
        priority=request.priority,
        target_length=effective_length,
        cache_entries=demand_cache,
    )


def detect_top_p_regrets(
    requests: list[Request],
    profile: StrategyProfile,
    sampler_fn,
    vocab,
    logits_loader,
) -> list[dict[str, Any]]:
    """检测收紧 top-p 是否淘汰了基准策略本会抽取的 token。

    ``sampler_fn`` 与 ``logits_loader`` 以依赖注入方式传入，避免模块环依赖。
    """
    regrets: list[dict[str, Any]] = []
    for request in requests:
        capped = profile.effective_params(request.sampling_params)
        if capped.top_p >= request.sampling_params.top_p:
            continue
        logits = logits_loader(request.request_id)
        baseline = sampler_fn(request, vocab, logits)
        tightened = sampler_fn(request, vocab, logits, param_override=capped)
        for base_step, tight_step in zip(baseline.steps, tightened.steps):
            if base_step.chosen_token_id not in tight_step.top_p_kept:
                regrets.append(
                    {
                        "request_id": request.request_id,
                        "step": base_step.step,
                        "baseline_token": base_step.chosen_token_id,
                        "capped_top_p": capped.top_p,
                        "kept_after_cap": list(tight_step.top_p_kept),
                    }
                )
    return regrets


@dataclass
class AdmissionPlan:
    selected_profile: StrategyProfile
    evaluations: list[StrategyEvaluation]
    selected: StrategyEvaluation
    selection_rule: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_rule": self.selection_rule,
            "selected_profile": self.selected_profile.name,
            "selected_limits": {
                "step_budget": self.selected_profile.step_budget,
                "cache_budget": self.selected_profile.cache_budget,
                "slot_budget": self.selected_profile.slot_budget,
                "top_p_cap": self.selected_profile.top_p_cap,
                "length_scale": self.selected_profile.length_scale,
            },
            "evaluations": [evaluation.to_dict() for evaluation in self.evaluations],
            "admitted_ids": list(self.selected.admitted_ids),
            "rejected": [decision.to_dict() for decision in self.selected.rejected],
        }


SELECTION_RULE = (
    "maximize admitted count; ties broken by min peak_cache, min total_steps, "
    "min peak_slots, then profile declaration order"
)


def arbitrate(
    requests: list[Request],
    limits: BudgetLimits,
    profiles: tuple[StrategyProfile, ...] = DEFAULT_PROFILES,
    max_waves: int = 1_000,
) -> AdmissionPlan:
    """对全部策略做确定性评估并选出唯一最优策略。"""
    evaluations = [
        evaluate_profile(requests, profile, limits, max_waves) for profile in profiles
    ]
    ranked = sorted(
        range(len(evaluations)),
        key=lambda index: (evaluations[index].score(), index),
    )
    best_index = ranked[0]
    return AdmissionPlan(
        selected_profile=profiles[best_index],
        evaluations=evaluations,
        selected=evaluations[best_index],
        selection_rule=SELECTION_RULE,
    )
