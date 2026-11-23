"""代价漂移判定与重优化触发裁决。

以初始统计快照估计值与实测观测计算定点偏差比，按漂移阈值与冷却逻辑毫秒
裁决是否触发重优化。同一观测序列在任意运行次数下得到相同的触发结论。
"""

from __future__ import annotations

from dataclasses import dataclass

from .executor import Observation
from .ledger import ErrorLedger


@dataclass(frozen=True)
class DriftVerdict:
    triggered: bool
    step_idx: int
    source_table: str      # 漂移来源表（该连接步的右表）
    join_key: str          # 漂移来源连接键
    ratio_fp: int          # 定点偏差比
    window_start: int      # 观测批次区间（含）
    window_end: int        # 观测批次区间（含）
    threshold_fp: int      # 定点触发阈值
    margin_fp: int         # 阈值余量 = ratio_fp - threshold_fp
    blocked_by_cooldown: bool = False


def deviation_ratio_fp(measured_fp: int, estimated_fp: int,
                       ledger: ErrorLedger, context: str) -> int:
    """|实测 - 估计| / 估计 的定点偏差比，全程整数运算并记账。"""
    diff = abs(measured_fp - estimated_fp)
    return ledger.to_fp(diff, estimated_fp, context)


def evaluate_step(observations: list[Observation], est_selectivity_fp: int,
                  threshold_fp: int, window: int, ledger: ErrorLedger,
                  step_idx: int, source_table: str, join_key: str,
                  clock_ms: int, last_trigger_clock: int | None,
                  cooldown_ms: int) -> DriftVerdict | None:
    """对单个连接步在最近 window 个观测批次上评估漂移。"""
    if len(observations) < window:
        return None
    recent = observations[-window:]
    sum_out = sum(o.output_rows for o in recent)
    sum_prod = sum(o.product for o in recent)
    ledger.record_add(f"drift:step={step_idx}:add_out", [0] * len(recent))
    ledger.record_add(f"drift:step={step_idx}:add_prod", [0] * len(recent))
    context = f"drift:step={step_idx}:seq={recent[-1].seq}"
    measured_fp = ledger.to_fp(sum_out, sum_prod, context + ":sel")
    ratio_fp = deviation_ratio_fp(measured_fp, est_selectivity_fp, ledger,
                                  context + ":ratio")
    margin_fp = ratio_fp - threshold_fp
    verdict = DriftVerdict(
        triggered=margin_fp > 0,
        step_idx=step_idx,
        source_table=source_table,
        join_key=join_key,
        ratio_fp=ratio_fp,
        window_start=recent[0].seq,
        window_end=recent[-1].seq,
        threshold_fp=threshold_fp,
        margin_fp=margin_fp,
    )
    if not verdict.triggered:
        return verdict
    if last_trigger_clock is not None and clock_ms - last_trigger_clock < cooldown_ms:
        return DriftVerdict(**{**verdict.__dict__, "blocked_by_cooldown": True})
    return verdict
