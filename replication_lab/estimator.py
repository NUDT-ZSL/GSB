"""Integer fixed-point replication-delay estimation and error accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Any

from .errors import ParameterError


ESTIMATOR_PARAMETERS_VERSION = "fixed-point-v1"
DEFAULT_FIXED_SCALE = 1000
DEFAULT_MIN_SAMPLES = 5


@dataclass(frozen=True)
class EstimatorParameters:
    version: str = ESTIMATOR_PARAMETERS_VERSION
    fixed_scale: int = DEFAULT_FIXED_SCALE
    min_samples: int = DEFAULT_MIN_SAMPLES
    p95_method: str = "nearest-rank"
    unknown_policy: str = "retain-and-report"

    def __post_init__(self) -> None:
        if self.version != ESTIMATOR_PARAMETERS_VERSION:
            raise ParameterError(f"unsupported estimator parameter version {self.version}")
        if self.fixed_scale <= 0:
            raise ParameterError("fixed_scale must be a positive integer")
        if self.min_samples < 0:
            raise ParameterError("min_samples must be non-negative")
        if self.p95_method != "nearest-rank":
            raise ParameterError("only nearest-rank P95 is supported")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "fixed_scale": self.fixed_scale,
            "min_samples": self.min_samples,
            "p95_method": self.p95_method,
            "unknown_policy": self.unknown_policy,
        }


def estimator_parameters_from_dict(value: Mapping[str, Any]) -> EstimatorParameters:
    return EstimatorParameters(
        version=str(value.get("version", ESTIMATOR_PARAMETERS_VERSION)),
        fixed_scale=int(value.get("fixed_scale", DEFAULT_FIXED_SCALE)),
        min_samples=int(value.get("min_samples", DEFAULT_MIN_SAMPLES)),
        p95_method=str(value.get("p95_method", "nearest-rank")),
        unknown_policy=str(value.get("unknown_policy", "retain-and-report")),
    )


@dataclass(frozen=True)
class ReplayParameters:
    """Deterministic schedule parameters used both live and in reproduction."""

    replay_rate: int
    concurrency: int
    clock_origin_ms: int
    version: str = "deterministic-batch-v1"

    def __post_init__(self) -> None:
        if self.replay_rate <= 0:
            raise ParameterError("replay_rate must be a positive integer (txn/s)")
        if self.concurrency <= 0:
            raise ParameterError("concurrency must be a positive integer")
        if self.clock_origin_ms < 0:
            raise ParameterError("clock_origin_ms must be non-negative")

    @property
    def batch_duration_ms(self) -> int:
        # One batch contains `concurrency` transactions.  Aggregate throughput
        # is replay_rate, so the ideal batch duration is 1000*C/R milliseconds.
        return max(
            1,
            (1000 * self.concurrency + self.replay_rate - 1) // self.replay_rate,
        )

    def batch_count(self, record_count: int) -> int:
        if record_count <= 0:
            return 0
        return (record_count + self.concurrency - 1) // self.concurrency

    def to_dict(self) -> dict[str, int | str]:
        return {
            "version": self.version,
            "replay_rate": self.replay_rate,
            "concurrency": self.concurrency,
            "clock_origin_ms": self.clock_origin_ms,
            "batch_duration_ms": self.batch_duration_ms,
        }


def _sample_delay(sample: Mapping[str, Any] | Any) -> int | None:
    if isinstance(sample, Mapping):
        value = sample.get("delay_ms")
    else:
        value = getattr(sample, "delay_ms", None)
    return None if value is None else int(value)


def _round_half_up(numerator: int, denominator: int) -> int:
    quotient, remainder = divmod(numerator, denominator)
    if remainder * 2 >= denominator:
        quotient += 1
    return quotient


def _rational(numerator: int, denominator: int) -> dict[str, int | float | str]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "ms": numerator / denominator,
        "text": f"{numerator}/{denominator} ms",
    }


def _parameter_sensitivity(
    replay_parameters: ReplayParameters,
    record_count: int,
) -> dict[str, object]:
    rate = replay_parameters.replay_rate
    concurrency = replay_parameters.concurrency
    duration = replay_parameters.batch_duration_ms
    batches = replay_parameters.batch_count(record_count)
    if rate > 1:
        lower_rate_duration = max(1, (1000 * concurrency + (rate - 1) - 1) // (rate - 1))
        rate_span_delta_ms = abs(lower_rate_duration - duration) * batches
    else:
        rate_span_delta_ms = None
    new_concurrency = concurrency + 1
    new_concurrency_duration = max(1, (1000 * new_concurrency + rate - 1) // rate)
    new_concurrency_batches = (
        (record_count + new_concurrency - 1) // new_concurrency if record_count else 0
    )
    concurrency_span = new_concurrency_duration * new_concurrency_batches
    current_span = duration * batches
    return {
        "formula": "D=ceil(1000*C/R), B=ceil(n/C), span=B*D",
        "replay_rate": rate,
        "concurrency": concurrency,
        "batch_duration_ms": duration,
        "batch_count": batches,
        "schedule_span_ms": current_span,
        "rate_minus_1_span_delta_ms": rate_span_delta_ms,
        "concurrency_plus_1_span_delta_ms": abs(concurrency_span - current_span),
        "interpretation": (
            "Span deltas quantify how integer rate/concurrency parameter changes "
            "move the last replayed transaction when the record set is fixed."
        ),
    }


def estimate_delays(
    samples: Iterable[Mapping[str, Any] | Any],
    *,
    parameters: EstimatorParameters | None = None,
    replay_parameters: ReplayParameters | None = None,
    incomplete_unknown_batches: int = 0,
) -> dict[str, object]:
    """Estimate delay statistics without dropping unknown-delay samples.

    The sample list includes one entry per confirmed replayed record.  Entries
    with `delay_ms=None` remain present and force DEGRADED_PARTIAL status.  An
    unfinished batch has no confirmed sample at all and is counted separately.
    """

    params = parameters or EstimatorParameters()
    materialized = list(samples)
    known: list[int] = []
    unknown_delay_count = 0
    for sample in materialized:
        delay = _sample_delay(sample)
        if delay is None:
            unknown_delay_count += 1
        else:
            if delay < 0:
                raise ParameterError("replication delay cannot be negative")
            known.append(delay)
    known.sort()
    sample_count = len(materialized)
    known_count = len(known)

    if known_count == 0:
        estimate_status = "INSUFFICIENT"
        confidence = "NONE"
        status_reason = "NO_KNOWN_DELAY_SAMPLES"
    elif known_count < params.min_samples:
        estimate_status = "INSUFFICIENT"
        confidence = "NONE"
        status_reason = "BELOW_MIN_SAMPLES"
    elif unknown_delay_count > 0 or incomplete_unknown_batches > 0:
        estimate_status = "DEGRADED_PARTIAL"
        confidence = "LOW"
        status_reason = "UNKNOWN_DELAYS_OR_BATCHES_RETAINED"
    else:
        estimate_status = "COMPLETE"
        confidence = "HIGH"
        status_reason = "ALL_DELAYS_KNOWN_AND_MIN_SAMPLES_MET"

    if known_count:
        delay_sum = sum(known)
        mean_fixed = _round_half_up(delay_sum * params.fixed_scale, known_count)
        p95_rank = (95 * known_count + 99) // 100
        p95_ms = known[p95_rank - 1]
        p95_fixed = p95_ms * params.fixed_scale
        maximum_ms = known[-1]
        maximum_fixed = maximum_ms * params.fixed_scale
    else:
        mean_fixed = None
        p95_ms = None
        p95_fixed = None
        maximum_ms = None
        maximum_fixed = None
        p95_rank = None

    if replay_parameters is not None:
        sensitivity = _parameter_sensitivity(replay_parameters, sample_count)
        schedule_bound_ms = int(sensitivity["batch_count"])
    else:
        sensitivity = None
        schedule_bound_ms = None

    mean_rounding_error = _rational(1, 2 * params.fixed_scale)
    if schedule_bound_ms is None:
        total_bound = None
    else:
        total_bound = _rational(
            2 * params.fixed_scale * schedule_bound_ms + 1,
            2 * params.fixed_scale,
        )

    error_steps = [
        "延迟按 completion_ts_ms - commit_ts_ms 计算；两个时钟值均为整数逻辑毫秒。",
        (
            f"均值乘以定点尺度 Q={params.fixed_scale} 后做整数四舍五入，"
            f"绝对量化误差不超过 1/(2Q)={mean_rounding_error['text']}。"
        ),
        "P95 使用 nearest-rank 选择整数毫秒样本，最大延迟直接取整数样本；二者无额外插值量化误差。",
        (
            "回放按 B=ceil(n/C) 批执行，批次时长 D=ceil(1000*C/R) 毫秒；"
            "每个批次向上取整误差小于 1 ms，末批累计调度量化误差保守上界为 B 毫秒。"
        ),
        (
            "缺少提交时间戳的样本不填补、不删除，只计入 unknown_delay_count；"
            "未完成批次不产生已确认样本，另计入 incomplete_unknown_batches。"
        ),
        "当存在未知项时，统计量只描述已确认子集，置信标记降级，不能解释为全体样本无偏结果。",
    ]

    return {
        "estimator_parameters": params.to_dict(),
        "sample_count": sample_count,
        "known_delay_count": known_count,
        "unknown_delay_count": unknown_delay_count,
        "incomplete_unknown_batches": int(incomplete_unknown_batches),
        "mean_fixed": mean_fixed,
        "mean_fixed_scale": params.fixed_scale,
        "mean_ms": (mean_fixed / params.fixed_scale if mean_fixed is not None else None),
        "p95_ms": p95_ms,
        "p95_fixed": p95_fixed,
        "p95_rank": p95_rank,
        "p95_method": params.p95_method,
        "max_delay_ms": maximum_ms,
        "max_delay_fixed": maximum_fixed,
        "estimate_status": estimate_status,
        "status_reason": status_reason,
        "confidence": confidence,
        "unknown_policy": params.unknown_policy,
        "quantization_error_upper_bound": {
            "mean": mean_rounding_error,
            "p95": _rational(0, 1),
            "max_delay": _rational(0, 1),
            "unit": "ms",
            "scope": "fixed-point aggregation only; unknown samples are not imputed",
        },
        "schedule_error_upper_bound_ms": schedule_bound_ms,
        "total_estimate_error_upper_bound": total_bound,
        "parameter_sensitivity": sensitivity,
        "error_bound_steps": error_steps,
    }
