from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Iterable

from .clock import RequestPlan, WindowPlan
from .ledger import LedgerBuilder, LedgerEntry, scaled_tolerance
from .scene import PrecisionConfig
from .util import canonical_json, fraction_dict, sha256_json


SUCCESS = "SUCCESS"


@dataclass(frozen=True)
class Observation:
    request_id: str
    request_index: int
    wave_index: int
    release_ms: int
    window_index: int
    http_status: int | None
    success: bool
    latency_ms: Decimal | None
    error_code: str | None
    error_message: str | None
    error_fields: dict[str, Any]
    raw_body: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "request_index": self.request_index,
            "wave_index": self.wave_index,
            "release_ms": self.release_ms,
            "window_index": self.window_index,
            "http_status": self.http_status,
            "success": self.success,
            "latency_ms": str(self.latency_ms) if self.latency_ms is not None else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "error_fields": self.error_fields,
        }


def observation_from_plan(request: RequestPlan, **kwargs: Any) -> Observation:
    return Observation(
        request_id=request.request_id,
        request_index=request.request_index,
        wave_index=request.wave_index,
        release_ms=request.release_ms,
        window_index=request.window_index,
        **kwargs,
    )


def build_window_summary(
    window: WindowPlan,
    observations: Iterable[Observation],
    precision: PrecisionConfig,
) -> tuple[dict[str, Any], dict[str, Any], list[LedgerEntry]]:
    observation_list = list(observations)
    ledger = LedgerBuilder(window.window_key)
    request_count = len(observation_list)
    success_count = sum(1 for item in observation_list if item.success)
    failure_count = request_count - success_count
    latency_samples = [
        (item.request_id, item.latency_ms)
        for item in observation_list
        if item.latency_ms is not None
    ]
    error_counts: dict[str, int] = {}
    for item in observation_list:
        if item.error_code:
            error_counts[item.error_code] = error_counts.get(item.error_code, 0) + 1

    ledger.add_exact_count("window_request_count", request_count, window.window_key, window_index=window.window_index)
    ledger.add_exact_count("window_success_count", success_count, window.window_key, window_index=window.window_index)
    ledger.add_exact_count("window_failure_count", failure_count, window.window_key, window_index=window.window_index)
    if request_count:
        success_entry = ledger.add_ratio(
            "window_success_rate",
            success_count,
            request_count,
            precision.percent_scale,
            window.window_key,
            window_index=window.window_index,
        )
    else:
        success_entry = ledger.add_conversion(
            "window_success_rate",
            Fraction(0),
            precision.percent_scale,
            window.window_key,
            window_index=window.window_index,
        )
    p95_entry = ledger.add_p95(
        "window_p95_latency",
        latency_samples,
        precision.latency_scale,
        window.window_key,
        window_index=window.window_index,
    )

    budget_success_scaled = _decimal_scale(precision.error_budget.success_rate, precision.percent_scale)
    budget_p95_scaled = _decimal_scale(precision.error_budget.p95_latency_ms, precision.latency_scale)
    success_error = success_entry.cumulative_error
    p95_error = p95_entry.cumulative_error if p95_entry is not None else Fraction(0)
    request_errors = [
        {
            "request_id": item.request_id,
            "code": item.error_code,
            "message": item.error_message,
            "fields": item.error_fields,
        }
        for item in observation_list
        if item.error_code is not None
    ]
    summary = {
        "window_key": window.window_key,
        "window_index": window.window_index,
        "start_ms": window.start_ms,
        "end_ms": window.end_ms,
        "request_count": request_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "success_rate_scaled": success_entry.result_scaled,
        "success_rate_scale": precision.percent_scale,
        "p95_latency_scaled": p95_entry.result_scaled if p95_entry is not None else None,
        "p95_latency_scale": precision.latency_scale,
        "success_rate_error_bound": fraction_dict(success_error),
        "p95_latency_error_bound": fraction_dict(p95_error),
        "success_error_budget_scaled": fraction_dict(budget_success_scaled),
        "p95_error_budget_scaled": fraction_dict(budget_p95_scaled),
        "within_error_budget": success_error <= budget_success_scaled and p95_error <= budget_p95_scaled,
        "errors_by_code": dict(sorted(error_counts.items())),
        "request_errors": request_errors,
        "ledger_local_seq_start": ledger.entries[0].local_seq,
        "ledger_local_seq_end": ledger.entries[-1].local_seq,
        "request_ids": [item.request_id for item in observation_list],
        "latency_sample_count": len(latency_samples),
    }
    payload = {
        "window_key": window.window_key,
        "window_index": window.window_index,
        "start_ms": window.start_ms,
        "end_ms": window.end_ms,
        "observations": [item.as_dict() for item in observation_list],
        "summary_base": {
            key: value
            for key, value in summary.items()
            if key not in {"ledger_seq_start", "ledger_seq_end"}
        },
    }
    summary["payload_hash"] = sha256_json(payload)
    return summary, payload, ledger.entries


def aggregate_gradient_summary(
    unit_key: str,
    windows: list[dict[str, Any]],
    payloads: list[dict[str, Any]],
    precision: PrecisionConfig,
) -> tuple[dict[str, Any], list[LedgerEntry]]:
    ledger = LedgerBuilder(f"gradient:{unit_key}")
    request_count = sum(int(item["request_count"]) for item in windows)
    success_count = sum(int(item["success_count"]) for item in windows)
    failure_count = request_count - success_count
    observations = [
        observation
        for payload in payloads
        for observation in payload.get("observations", [])
    ]
    latency_samples = [
        (str(item["request_id"]), Decimal(str(item["latency_ms"])))
        for item in observations
        if item.get("latency_ms") is not None
    ]
    error_counts: dict[str, int] = {}
    for item in observations:
        code = item.get("error_code")
        if code:
            error_counts[code] = error_counts.get(code, 0) + 1

    ledger.add_exact_count("gradient_window_count", len(windows), unit_key)
    ledger.add_exact_count("gradient_request_count_sum", request_count, unit_key)
    ledger.add_exact_count("gradient_success_count_sum", success_count, unit_key)
    ledger.add_exact_count("gradient_failure_count_sum", failure_count, unit_key)
    success_entry = ledger.add_ratio(
        "gradient_success_rate",
        success_count,
        request_count,
        precision.percent_scale,
        unit_key,
    )
    p95_entry = ledger.add_p95(
        "gradient_p95_latency",
        latency_samples,
        precision.latency_scale,
        unit_key,
    )
    budget_success_scaled = _decimal_scale(precision.error_budget.success_rate, precision.percent_scale)
    budget_p95_scaled = _decimal_scale(precision.error_budget.p95_latency_ms, precision.latency_scale)
    success_error = success_entry.cumulative_error
    p95_error = p95_entry.cumulative_error if p95_entry is not None else Fraction(0)
    summary = {
        "unit_key": unit_key,
        "window_count": len(windows),
        "request_count": request_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "success_rate_scaled": success_entry.result_scaled,
        "success_rate_scale": precision.percent_scale,
        "p95_latency_scaled": p95_entry.result_scaled if p95_entry is not None else None,
        "p95_latency_scale": precision.latency_scale,
        "success_rate_error_bound": fraction_dict(success_error),
        "p95_latency_error_bound": fraction_dict(p95_error),
        "success_error_budget_scaled": fraction_dict(budget_success_scaled),
        "p95_error_budget_scaled": fraction_dict(budget_p95_scaled),
        "within_error_budget": success_error <= budget_success_scaled and p95_error <= budget_p95_scaled,
        "errors_by_code": dict(sorted(error_counts.items())),
        "window_keys": sorted(str(item["window_key"]) for item in windows),
        "success_ledger_local_seq": success_entry.local_seq,
        "p95_ledger_local_seq": p95_entry.local_seq if p95_entry is not None else None,
    }
    return summary, ledger.entries


def decide_gradient(
    metric: dict[str, Any],
    tenant_success_min: Decimal,
    tenant_p95_max: Decimal,
    precision: PrecisionConfig,
) -> dict[str, Any]:
    success_scale = int(metric["success_rate_scale"])
    p95_scale = int(metric["p95_latency_scale"])
    success_rate_scaled = int(metric["success_rate_scaled"])
    p95_scaled = metric["p95_latency_scaled"]
    threshold_success_scaled = _decimal_scale(tenant_success_min, success_scale)
    threshold_p95_scaled = _decimal_scale(tenant_p95_max, p95_scale)
    success_margin_scaled = scaled_tolerance(precision.compare_tolerance.degraded_success_rate, success_scale)
    p95_margin_scaled = scaled_tolerance(precision.compare_tolerance.degraded_p95_latency_ms, p95_scale)

    success_pass = success_rate_scaled >= threshold_success_scaled
    success_violation = max(0, threshold_success_scaled - success_rate_scaled)
    success_degraded = not success_pass and success_violation <= success_margin_scaled
    p95_pass = p95_scaled is not None and int(p95_scaled) <= threshold_p95_scaled
    p95_violation = max(0, int(p95_scaled) - threshold_p95_scaled) if p95_scaled is not None else threshold_p95_scaled
    p95_degraded = not p95_pass and p95_violation <= p95_margin_scaled

    if success_pass and p95_pass:
        status = "PASS"
        reason = "both_slo_met"
    elif (success_pass or success_degraded) and (p95_pass or p95_degraded):
        status = "DEGRADED_PASS"
        reason = "slo_shortfall_within_degraded_margin"
    else:
        status = "ABORT"
        reason = "slo_shortfall_exceeds_degraded_margin"

    return {
        "status": status,
        "reason": reason,
        "success_rate_scaled": success_rate_scaled,
        "success_rate_scale": success_scale,
        "success_rate_threshold_scaled": threshold_success_scaled,
        "success_rate_violation_scaled": success_violation,
        "success_rate_degraded_margin_scaled": success_margin_scaled,
        "success_pass": success_pass,
        "success_degraded": success_degraded,
        "p95_latency_scaled": p95_scaled,
        "p95_latency_scale": p95_scale,
        "p95_latency_threshold_scaled": threshold_p95_scaled,
        "p95_latency_violation_scaled": p95_violation,
        "p95_latency_degraded_margin_scaled": p95_margin_scaled,
        "p95_pass": p95_pass,
        "p95_degraded": p95_degraded,
    }


def _decimal_scale(value: Decimal, scale: int) -> Fraction:
    return Fraction(value) * scale

