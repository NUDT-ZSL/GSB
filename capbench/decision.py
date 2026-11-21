from __future__ import annotations

from typing import Any, Sequence

from .scenario import SLO


def judge_window(summary: dict[str, Any], slo: SLO) -> dict[str, Any]:
    success_rate = float(summary["success_rate"])
    p95 = summary.get("p95_latency_ms")
    interrupted = int(
        summary.get("error_counts", {}).get("target_service_interrupted", 0)
    )
    hard_success_floor = slo.min_success_rate - slo.degraded_success_margin
    hard_latency_ceiling = slo.max_p95_latency_ms * slo.degraded_latency_ratio

    reasons: list[str] = []
    if p95 is None:
        verdict = "abort"
        reasons.append("no_latency_observations")
    elif interrupted > 0:
        verdict = "abort"
        reasons.append("target_service_interrupted")
    elif success_rate < hard_success_floor:
        verdict = "abort"
        reasons.append("success_rate_below_hard_floor")
    elif float(p95) > hard_latency_ceiling:
        verdict = "abort"
        reasons.append("p95_above_hard_latency_ceiling")
    elif success_rate >= slo.min_success_rate and float(p95) <= slo.max_p95_latency_ms:
        verdict = "pass"
    else:
        verdict = "degraded_pass"
        if success_rate < slo.min_success_rate:
            reasons.append("success_rate_in_degraded_band")
        if float(p95) > slo.max_p95_latency_ms:
            reasons.append("p95_in_degraded_band")
        if not reasons:
            reasons.append("within_degraded_policy")

    return {
        "tenant": summary["tenant"],
        "gradient_index": summary["gradient_index"],
        "window_index": summary["window_index"],
        "verdict": verdict,
        "reasons": reasons,
        "success_rate": success_rate,
        "min_success_rate": slo.min_success_rate,
        "degraded_success_margin": slo.degraded_success_margin,
        "hard_success_floor": round(hard_success_floor, 6),
        "p95_latency_ms": p95,
        "max_p95_latency_ms": slo.max_p95_latency_ms,
        "degraded_latency_ratio": slo.degraded_latency_ratio,
        "hard_latency_ceiling_ms": round(hard_latency_ceiling, 6),
        "target_service_interrupted": interrupted,
        "request_seq_start": summary.get("request_seq_start"),
        "request_seq_end": summary.get("request_seq_end"),
    }


def judge_gradient(
    *,
    summaries: Sequence[dict[str, Any]],
    slo: SLO,
    total_windows: int,
    allow_early_termination: bool,
) -> dict[str, Any]:
    ordered = sorted(summaries, key=lambda item: int(item["window_index"]))
    evidence = [judge_window(item, slo) for item in ordered]
    abort_evidence = [item for item in evidence if item["verdict"] == "abort"]
    degraded = any(item["verdict"] == "degraded_pass" for item in evidence)

    terminated = False
    trigger = None
    if abort_evidence:
        verdict = "abort"
        if allow_early_termination:
            terminated = True
            trigger = abort_evidence[0]
    elif len(ordered) == total_windows:
        verdict = "degraded_pass" if degraded else "pass"
    else:
        verdict = "pending"

    return {
        "verdict": verdict,
        "terminated_early": terminated,
        "trigger": trigger,
        "degraded": degraded and verdict in ("degraded_pass", "abort"),
        "evidence": evidence,
        "completed_windows": len(ordered),
        "total_windows": total_windows,
    }

