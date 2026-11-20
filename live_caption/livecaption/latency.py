"""延迟预算观测与等待时长裁决。

- 每块记录：首事件到达时刻、定稿时刻、等待时长、块内事件数、窗口边界、
  以及本地重排循环的实际处理耗时（仅观测记录，不参与确定性决策）；
- 等待超过 max_wait_ms -> E_LATENCY_BUDGET，附带已完成块数与当前临时版本；
- 取舍裁决的显式优先级规则（声明）：
    P1 延迟预算：所选等待时长不得超过 max_wait_ms；
    P2 改写稳定性：在 P1 约束内尽量覆盖观测到的迟到分布，压低改写深度；
  即延迟预算优先于改写稳定性。每块处理计划记录所选等待时长与依据。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List

from .config import DisplayPolicy
from .errors import E_LATENCY_BUDGET, BusinessError

PRIORITY_RULE = "P1: wait<=max_wait_ms (latency budget) > P2: rewrite stability"


@dataclass(frozen=True)
class ChunkPlan:
    chunk_seq: int
    window_start_ms: int
    window_end_ms: int
    chosen_wait_ms: int
    rationale: str


@dataclass(frozen=True)
class ChunkTiming:
    chunk_seq: int
    event_count: int
    window_start_ms: int
    window_end_ms: int
    first_arrival_ms: int
    finalized_ms: int
    wait_ms: int
    process_us: int
    chosen_wait_ms: int


class LatencyController:
    """按块做等待时长裁决与预算校验。"""

    def __init__(self, policy: DisplayPolicy) -> None:
        self.policy = policy
        self.plans: List[ChunkPlan] = []
        self.timings: List[ChunkTiming] = []
        self._lateness_samples: List[int] = []

    def observe_lateness(self, late_ms: int) -> None:
        """记录迟到样本（事件实际到达时刻 - 时间轴顺序期望到达时刻）。"""
        if late_ms > 0:
            self._lateness_samples.append(late_ms)

    def plan_chunk(self, chunk_seq: int, window_start_ms: int,
                   window_end_ms: int) -> ChunkPlan:
        """按显式优先级规则为该块选择等待时长并记录依据。"""
        if self._lateness_samples:
            samples = sorted(self._lateness_samples)
            p95 = samples[min(len(samples) - 1, (len(samples) * 95) // 100)]
        else:
            p95 = 0
        wanted = p95
        chosen = min(wanted, self.policy.max_wait_ms)
        if wanted > self.policy.max_wait_ms:
            rationale = (
                f"p95 lateness {wanted}ms exceeds budget; clamped to "
                f"{chosen}ms per priority rule ({PRIORITY_RULE}); "
                f"expect deeper rewrites")
        else:
            rationale = (
                f"chosen wait {chosen}ms covers observed p95 lateness "
                f"{wanted}ms within budget {self.policy.max_wait_ms}ms")
        plan = ChunkPlan(chunk_seq=chunk_seq, window_start_ms=window_start_ms,
                         window_end_ms=window_end_ms, chosen_wait_ms=chosen,
                         rationale=rationale)
        self.plans.append(plan)
        return plan

    def record_timing(self, chunk_seq: int, event_count: int,
                      window_start_ms: int, window_end_ms: int,
                      first_arrival_ms: int, finalized_ms: int,
                      process_us: int, chosen_wait_ms: int,
                      completed_chunks: int, temp_version: int) -> ChunkTiming:
        wait_ms = finalized_ms - first_arrival_ms
        timing = ChunkTiming(
            chunk_seq=chunk_seq, event_count=event_count,
            window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            first_arrival_ms=first_arrival_ms, finalized_ms=finalized_ms,
            wait_ms=wait_ms, process_us=process_us,
            chosen_wait_ms=chosen_wait_ms)
        self.timings.append(timing)
        if wait_ms > self.policy.max_wait_ms:
            raise BusinessError(
                E_LATENCY_BUDGET,
                f"chunk {chunk_seq} wait {wait_ms}ms exceeds "
                f"max_wait_ms {self.policy.max_wait_ms}ms",
                chunk_seq=chunk_seq, wait_ms=wait_ms,
                max_wait_ms=self.policy.max_wait_ms,
                completed_chunks=completed_chunks,
                temp_version=temp_version,
            )
        return timing


class ProcessTimer:
    """本地重排循环实际处理计时（仅观测，不影响确定性结果）。"""

    def __init__(self) -> None:
        self._start = time.perf_counter_ns()

    def elapsed_us(self) -> int:
        return (time.perf_counter_ns() - self._start) // 1000
