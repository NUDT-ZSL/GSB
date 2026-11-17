"""档位自适应选择、去噪步预算调度与首帧预览预算评估。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from .config import Config, Tier
from .denoise import checkpoint_for_tier
from .errors import ErrorCode, Failure

SERIAL = "frame_serial"
FRAME_BATCH = "frame_batch"
CROSS_CLIP = "cross_clip_merge"
POLICY_TABLE = (SERIAL, FRAME_BATCH, CROSS_CLIP)

# 相对于逐帧串行基准的算力权重与单帧内存（MB）权重。
COMPUTE_WEIGHT = {SERIAL: 1.00, FRAME_BATCH: 1.04, CROSS_CLIP: 1.10}
MEMORY_WEIGHT = {SERIAL: 1.00, FRAME_BATCH: 1.60, CROSS_CLIP: 2.40}
BASE_FRAME_MB = 4.0


@dataclass(frozen=True)
class TierPlan:
    tier: str
    tier_index: int
    checkpoint_step: int          # 复用低档位的检查点（-1 表示无复用）
    reused_steps: int
    new_steps: int                # 本档需真实计算的步数（每帧）
    per_frame_cost_ms: float
    tier_cost_ms: float
    tier_step_units: int          # 本档去噪步预算消耗（新步 * 帧数 * 片段数）
    feasible: bool
    pending_reason: str = ""


@dataclass(frozen=True)
class SchedulingDecision:
    selected_tiers: List[TierPlan]
    pending_tiers: List[TierPlan]
    batch_mode: str
    candidates: List[Dict[str, Any]]
    reference_metrics: Dict[str, Any]
    rationale: List[str]
    failures: List[Failure]
    step_budget_used: int
    step_budget_total: int
    memory_peak_mb: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_mode": self.batch_mode,
            "reference_metrics": self.reference_metrics,
            "candidate_policies": self.candidates,
            "rationale": self.rationale,
            "selected_tiers": [_plan_dict(plan) for plan in self.selected_tiers],
            "pending_tiers": [_plan_dict(plan) for plan in self.pending_tiers],
            "step_budget_used": self.step_budget_used,
            "step_budget_total": self.step_budget_total,
            "memory_peak_mb": round(self.memory_peak_mb, 3),
            "failures": [item.to_dict() for item in self.failures],
        }


def _plan_dict(plan: TierPlan) -> Dict[str, Any]:
    return {
        "tier": plan.tier,
        "tier_index": plan.tier_index,
        "checkpoint_step": plan.checkpoint_step,
        "reused_steps": plan.reused_steps,
        "new_steps": plan.new_steps,
        "per_frame_cost_ms": round(plan.per_frame_cost_ms, 4),
        "tier_cost_ms": round(plan.tier_cost_ms, 4),
        "tier_step_units": plan.tier_step_units,
        "feasible": plan.feasible,
        "pending_reason": plan.pending_reason,
    }


def per_frame_cost_ms(config: Config, tier: Tier) -> float:
    """单帧单步去噪代价估计，与分辨率像素数成比例。"""
    reference_pixels = config.tiers[0].pixels
    return config.preview_cost_per_step_ms * tier.pixels / reference_pixels


def choose_batch_mode(config: Config, clip_count: int, frames: int,
                      tier: Tier) -> Tuple[str, List[Dict[str, Any]], float]:
    latent_mb = (config.latent_channels * tier.latent_shape[1]
                 * tier.latent_shape[2] * 4) / (1024 * 1024)
    candidates: List[Dict[str, Any]] = []
    feasible_modes: List[str] = []
    # 吞吐收益：批量越大调度开销越低；以串行基准为 0 的相对收益。
    throughput_gain = {
        SERIAL: 0.0, FRAME_BATCH: 0.35, CROSS_CLIP: 0.60}
    for mode in POLICY_TABLE:
        peak_mb = BASE_FRAME_MB * MEMORY_WEIGHT[mode] * latent_mb * frames
        feasible = peak_mb <= config.memory_cap_mb
        pressure = peak_mb / config.memory_cap_mb
        # 分数越低越优：吞吐收益（负贡献）+ 算力冗余 + 内存压力惩罚。
        score = (-throughput_gain[mode]
                 + 0.10 * (COMPUTE_WEIGHT[mode] - 1.0)
                 + 0.40 * pressure)
        candidates.append({
            "mode": mode,
            "compute_weight": COMPUTE_WEIGHT[mode],
            "throughput_gain": throughput_gain[mode],
            "peak_memory_mb": round(peak_mb, 3),
            "memory_cap_mb": config.memory_cap_mb,
            "score": round(float(score), 5),
            "feasible": bool(feasible),
        })
        if feasible:
            feasible_modes.append(mode)
    if not feasible_modes:
        # 内存上限极端受限时退化为逐帧串行（峰值最低）。
        return SERIAL, candidates, BASE_FRAME_MB * latent_mb * frames
    selected = min(
        feasible_modes,
        key=lambda mode: next(item["score"] for item in candidates
                              if item["mode"] == mode))
    peak = next(item["peak_memory_mb"] for item in candidates
                if item["mode"] == selected)
    return selected, candidates, peak


def schedule(config: Config, clip_count: int) -> SchedulingDecision:
    frames = config.frames
    reference_metrics = {
        "clip_count": clip_count,
        "frames_per_clip": frames,
        "denoise_steps": config.denoise_steps,
        "tier_count": len(config.tiers),
        "step_budget": config.denoise_step_budget,
        "memory_cap_mb": config.memory_cap_mb,
        "concurrency": config.concurrency,
    }
    # 批量模式按最高档内存压力选择，全批次统一模式，保证跨档位可审计。
    top_tier = config.tiers[-1]
    batch_mode, candidates, peak_mb = choose_batch_mode(
        config, clip_count, frames, top_tier)

    failures: List[Failure] = []
    selected: List[TierPlan] = []
    pending: List[TierPlan] = []
    budget_remaining = config.denoise_step_budget
    rationale: List[str] = [
        f"批量策略 {batch_mode} 在内存上限 {config.memory_cap_mb}MB 内候选评分最优"]

    for index, tier in enumerate(config.tiers):
        checkpoint = checkpoint_for_tier(
            index, config.img2img_replace_steps, config.denoise_steps)
        reused = 0 if checkpoint < 0 else checkpoint + 1
        new_steps = config.denoise_steps - reused
        cost_each = per_frame_cost_ms(config, tier)
        units = new_steps * frames * clip_count
        plan = TierPlan(
            tier=tier.name, tier_index=index, checkpoint_step=checkpoint,
            reused_steps=reused, new_steps=new_steps,
            per_frame_cost_ms=cost_each,
            tier_cost_ms=cost_each * new_steps * frames * clip_count,
            tier_step_units=units,
            feasible=False)
        if config.denoise_step_budget == 0:
            pending_plan = TierPlan(**{**plan.__dict__,
                                       "feasible": False,
                                       "pending_reason": "全局去噪步预算为 0"})
            pending.append(pending_plan)
            failures.append(Failure(
                code=ErrorCode.E_DENOISE_BUDGET_PENDING,
                message=f"档位 {tier.name} 因去噪步预算为 0 挂起",
                tier=tier.name, payload=_plan_dict(pending_plan)))
            continue
        if units > budget_remaining:
            pending_plan = TierPlan(**{**plan.__dict__,
                                       "feasible": False,
                                       "pending_reason": (
                                           f"需要 {units} 步预算，剩余 "
                                           f"{budget_remaining}")})
            pending.append(pending_plan)
            failures.append(Failure(
                code=ErrorCode.E_DENOISE_BUDGET_PENDING,
                message=(f"档位 {tier.name} 预算不足挂起：需要 {units}，"
                         f"剩余 {budget_remaining}"),
                tier=tier.name, payload=_plan_dict(pending_plan)))
            continue
        object.__setattr__(plan, "feasible", True)
        selected.append(plan)
        budget_remaining -= units

    if selected:
        rationale.append(
            "按档位单调升级选择 "
            + ", ".join(plan.tier for plan in selected)
            + "，高档位通过检查点复用低档位已验证步")
    if pending:
        rationale.append(
            "挂起档位保留中间状态，可在追加预算后从检查点继续: "
            + ", ".join(plan.tier for plan in pending))

    return SchedulingDecision(
        selected_tiers=selected, pending_tiers=pending,
        batch_mode=batch_mode, candidates=candidates,
        reference_metrics=reference_metrics, rationale=rationale,
        failures=failures,
        step_budget_used=config.denoise_step_budget - budget_remaining,
        step_budget_total=config.denoise_step_budget,
        memory_peak_mb=peak_mb)


def preview_plan(config: Config, clip_count: int) -> Dict[str, Any]:
    """估算最低档位首帧预览是否能在首帧预算内交付。"""
    tier = config.tiers[0]
    cost_each = per_frame_cost_ms(config, tier)
    init_cost = cost_each * 0.5
    denoise_cost = cost_each * config.denoise_steps
    decode_cost = cost_each * 0.5
    total = init_cost + denoise_cost + decode_cost
    eligible = total <= config.first_frame_budget_ms
    result = {
        "tier": tier.name,
        "init_cost_ms": round(init_cost, 4),
        "denoise_cost_ms": round(denoise_cost, 4),
        "decode_cost_ms": round(decode_cost, 4),
        "estimated_cost_ms": round(total, 4),
        "budget_ms": config.first_frame_budget_ms,
        "eligible": bool(eligible),
    }
    if not eligible:
        result["code"] = ErrorCode.E_PREVIEW_BUDGET_PENDING
        result["message"] = (
            f"最低档首帧预估 {total:.3f}ms 超出首帧预算 "
            f"{config.first_frame_budget_ms}ms，返回中间状态而非阻塞")
    return result
