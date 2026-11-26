from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any

from .hashing import sha256_object


def decimal_to_fraction(value: Any) -> Fraction:
    if isinstance(value, Decimal):
        return Fraction(format(value, "f"))
    if isinstance(value, float):
        return Fraction(value).limit_denominator(10**12)
    return Fraction(str(value))


def fraction_to_decimal(value: Fraction, precision: int = 60) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = precision
        return Decimal(value.numerator) / Decimal(value.denominator)


@dataclass
class Ledger:
    entries: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    def add(
        self,
        *,
        window_no: int,
        service_name: str,
        instance_id: str,
        metric: str,
        stage: str,
        op_type: str,
        result: Fraction | int | None = None,
        operand_a: Any = None,
        operand_b: Any = None,
        numerator: int | None = None,
        denominator: int | None = None,
        floor_quotient: int | None = None,
        remainder: int = 0,
        rounded_value: int | None = None,
        rounding_mode: str = "exact",
        rounding_direction: str = "exact",
        error_bound_fixed_units: str = "0",
        entry_key: str | None = None,
    ) -> dict[str, Any]:
        self._seq += 1
        if result is not None and not isinstance(result, int):
            result_value = format(fraction_to_decimal(result, 40), "f")
        else:
            result_value = result
        entry = {
            "seq": self._seq,
            "entry_key": entry_key or f"seq-{self._seq}",
            "window_no": window_no,
            "service_name": service_name,
            "instance_id": instance_id,
            "metric": metric,
            "stage": stage,
            "op_type": op_type,
            "operand_a": str(operand_a) if operand_a is not None else None,
            "operand_b": str(operand_b) if operand_b is not None else None,
            "numerator": numerator,
            "denominator": denominator,
            "floor_quotient": floor_quotient,
            "discarded_remainder": remainder,
            "remainder_fraction": f"{remainder}/{denominator}" if denominator else "0",
            "rounded_value": rounded_value if rounded_value is not None else result_value,
            "result_value": result_value,
            "rounding_mode": rounding_mode,
            "rounding_direction": rounding_direction,
            "error_bound_fixed_units": error_bound_fixed_units,
        }
        entry["entry_digest"] = sha256_object({k: v for k, v in entry.items() if k != "entry_digest"})
        self.entries.append(entry)
        return entry

    def seed_seq(self, value: int) -> None:
        self._seq = max(self._seq, value)


def round_scaled_fraction(
    value: Fraction,
    scale: int,
    mode: str,
    ledger: Ledger,
    *,
    window_no: int,
    service_name: str,
    instance_id: str,
    metric: str,
    stage: str = "fixed_scale",
) -> tuple[int, dict[str, Any]]:
    scaled = value * scale
    numerator = scaled.numerator
    denominator = scaled.denominator
    floor_quotient, remainder = divmod(numerator, denominator)
    quotient = floor_quotient
    increment = False
    if remainder:
        if mode in {"ceil", "up"}:
            increment = True
        elif mode == "half_up":
            increment = 2 * remainder >= denominator
        elif mode == "half_even":
            twice = 2 * remainder
            increment = twice > denominator or (twice == denominator and floor_quotient % 2 == 1)
        elif mode in {"floor", "down"}:
            increment = False
        else:
            raise ValueError(f"unsupported rounding mode {mode}")
    if increment:
        quotient += 1
    if remainder == 0:
        direction = "exact"
        bound = Fraction(0)
    elif mode in {"half_up", "half_even"}:
        direction = "up" if increment else "down"
        bound = Fraction(1, 2)
    else:
        direction = "up" if increment else "down"
        bound = Fraction(1)
    key = f"w{window_no}:{instance_id}:{metric}:{stage}"
    entry = ledger.add(
        window_no=window_no,
        service_name=service_name,
        instance_id=instance_id,
        metric=metric,
        stage=stage,
        op_type="rounded_division",
        operand_a=value,
        operand_b=scale,
        numerator=numerator,
        denominator=denominator,
        floor_quotient=floor_quotient,
        remainder=remainder,
        rounded_value=quotient,
        rounding_mode=mode,
        rounding_direction=direction,
        error_bound_fixed_units=f"{bound.numerator}/{bound.denominator}",
        entry_key=key,
    )
    return quotient, entry


def log_exact_binop(
    ledger: Ledger,
    *,
    window_no: int,
    service_name: str,
    instance_id: str,
    metric: str,
    stage: str,
    op_type: str,
    left: Fraction,
    right: Fraction,
    result: Fraction,
) -> None:
    ledger.add(
        window_no=window_no,
        service_name=service_name,
        instance_id=instance_id,
        metric=metric,
        stage=stage,
        op_type=op_type,
        operand_a=left,
        operand_b=right,
        result=result,
        numerator=result.numerator,
        denominator=result.denominator,
        rounding_mode="exact",
        rounding_direction="exact",
        error_bound_fixed_units="0",
        entry_key=f"w{window_no}:{instance_id}:{metric}:{stage}:{op_type}",
    )


def nearest_rank_p95(durations: list[Decimal]) -> tuple[Decimal, int, int, int]:
    ordered = sorted(durations)
    count = len(ordered)
    rank = -((-95 * count) // 100)
    index = rank - 1
    return ordered[index], rank, count, index


def compute_raw_fixed(
    observation: dict[str, Any],
    metric_name: str,
    spec: dict[str, Any],
    ledger: Ledger,
) -> Fraction:
    w = observation["window_no"]
    service = observation["service_name"]
    instance = observation["instance_id"]

    def log(stage: str, op: str, left: Fraction, right: Fraction, result: Fraction) -> None:
        log_exact_binop(
            ledger,
            window_no=w,
            service_name=service,
            instance_id=instance,
            metric=metric_name,
            stage=stage,
            op_type=op,
            left=left,
            right=right,
            result=result,
        )

    count = Fraction(observation["request_count"])
    window_ms = Fraction(observation["end_ms"] - observation["start_ms"])
    total_duration = sum((decimal_to_fraction(value) for value in observation["durations"]), Fraction(0))

    if metric_name == "cpu_utilization":
        per_ms = decimal_to_fraction(spec.get("duration_coefficient_per_ms", "0"))
        per_request = decimal_to_fraction(spec.get("per_request_percent", "0"))
        baseline = decimal_to_fraction(spec.get("baseline_percent", "0"))
        duration_part = total_duration * per_ms
        log("cpu_duration_product", "multiplication", total_duration, per_ms, duration_part)
        request_part = count * per_request
        log("cpu_request_product", "multiplication", count, per_request, request_part)
        result = baseline + duration_part + request_part
        return result

    if metric_name == "memory_utilization":
        baseline = decimal_to_fraction(spec.get("baseline_percent", "0"))
        per_request = decimal_to_fraction(spec.get("per_request_percent", "0"))
        duration_coefficient = decimal_to_fraction(spec.get("duration_coefficient_per_ms", "0"))
        request_part = count * per_request
        log("memory_request_product", "multiplication", count, per_request, request_part)
        duration_part = total_duration * duration_coefficient
        log("memory_duration_product", "multiplication", total_duration, duration_coefficient, duration_part)
        return baseline + request_part + duration_part

    if metric_name == "latency_p95":
        p95, rank, n, index = nearest_rank_p95(observation["durations"])
        numerator = 95 * n
        floor_q, remainder = divmod(numerator, 100)
        ledger.add(
            window_no=w,
            service_name=service,
            instance_id=instance,
            metric=metric_name,
            stage="p95_nearest_rank",
            op_type="rounded_division",
            operand_a=numerator,
            operand_b=100,
            numerator=numerator,
            denominator=100,
            floor_quotient=floor_q,
            remainder=remainder,
            rounded_value=rank,
            rounding_mode="ceil",
            rounding_direction="up" if remainder else "exact",
            error_bound_fixed_units="0",
            entry_key=f"w{w}:{instance}:{metric_name}:rank",
        )
        observation["p95_rank"] = rank
        observation["p95_index"] = index
        return decimal_to_fraction(p95)

    if metric_name == "throughput_attainment":
        expected_rate = decimal_to_fraction(observation["expected_rate_rps"])
        expected_requests = expected_rate * window_ms / 1000
        log("expected_count_rate_times_ms", "multiplication", expected_rate, window_ms, expected_rate * window_ms)
        divisor_part = expected_rate * window_ms
        result_expected = divisor_part / 1000
        log("expected_count_ms_to_seconds", "division", divisor_part, Fraction(1000), result_expected)
        success = Fraction(observation["success_count"])
        if expected_requests == 0:
            raise ValueError("expected request volume must be positive")
        result = success / expected_requests
        log("attainment_division", "division", success, expected_requests, result)
        return result

    raise KeyError(metric_name)


def compute_decimal_metric(observation: dict[str, Any], metric_name: str, spec: dict[str, Any]) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 60
        count = Decimal(observation["request_count"])
        window_ms = Decimal(observation["end_ms"] - observation["start_ms"])
        total_duration = sum((Decimal(str(value)) for value in observation["durations"]), Decimal(0))
        if metric_name == "cpu_utilization":
            return (
                Decimal(str(spec.get("baseline_percent", "0")))
                + total_duration * Decimal(str(spec.get("duration_coefficient_per_ms", "0")))
                + count * Decimal(str(spec.get("per_request_percent", "0")))
            )
        if metric_name == "memory_utilization":
            return (
                Decimal(str(spec.get("baseline_percent", "0")))
                + count * Decimal(str(spec.get("per_request_percent", "0")))
                + total_duration * Decimal(str(spec.get("duration_coefficient_per_ms", "0")))
            )
        if metric_name == "latency_p95":
            p95, _, _, _ = nearest_rank_p95(observation["durations"])
            return Decimal(str(p95))
        if metric_name == "throughput_attainment":
            expected_rate = Decimal(str(observation["expected_rate_rps"]))
            expected_requests = expected_rate * window_ms / Decimal(1000)
            return Decimal(observation["success_count"]) / expected_requests
    raise KeyError(metric_name)


def convert_observation_metric(
    observation: dict[str, Any],
    metric_name: str,
    spec: dict[str, Any],
    ledger: Ledger,
    *,
    parameter_revision: int = 0,
) -> dict[str, Any]:
    raw = compute_raw_fixed(observation, metric_name, spec, ledger)
    scale = int(spec["scale"])
    mode = spec.get("rounding", "half_up")
    fixed_value, scale_entry = round_scaled_fraction(
        raw,
        scale,
        mode,
        ledger,
        window_no=observation["window_no"],
        service_name=observation["service_name"],
        instance_id=observation["instance_id"],
        metric=metric_name,
    )
    decimal_truth = compute_decimal_metric(observation, metric_name, spec)
    fixed_physical = Decimal(fixed_value) / Decimal(scale)
    absolute_deviation = abs(fixed_physical - decimal_truth)
    relative_denominator = abs(decimal_truth)
    relative_deviation = (
        absolute_deviation / relative_denominator if relative_denominator != 0 else absolute_deviation
    )
    bound_entry = Fraction(scale_entry["error_bound_fixed_units"]) / scale
    error_bound_physical = fraction_to_decimal(bound_entry, 50)
    ledger_seqs = [
        entry["seq"]
        for entry in ledger.entries
        if entry["window_no"] == observation["window_no"]
        and entry["instance_id"] == observation["instance_id"]
        and entry["metric"] == metric_name
    ]
    return {
        "obs_key": f"W{observation['window_no']}:{observation['instance_id']}",
        "window_no": observation["window_no"],
        "slot_index": observation["slot_index"],
        "service_name": observation["service_name"],
        "instance_id": observation["instance_id"],
        "service_level": observation["service_level"],
        "metric": metric_name,
        "request_seq_min": observation["request_seq_min"],
        "request_seq_max": observation["request_seq_max"],
        "request_count": observation["request_count"],
        "request_seqs": observation["request_seqs"],
        "scale": scale,
        "rounding": mode,
        "raw_value_decimal": format(fraction_to_decimal(raw, 60), "f"),
        "decimal_value": format(decimal_truth, "f"),
        "fixed_value": fixed_value,
        "fixed_physical_value": format(fixed_physical, "f"),
        "absolute_deviation": format(absolute_deviation, "f"),
        "relative_deviation": format(relative_deviation, "f"),
        "error_upper_bound_physical": format(error_bound_physical, "f"),
        "error_upper_bound_fixed_units": scale_entry["error_bound_fixed_units"],
        "ledger_seqs": ledger_seqs,
        "parameter_revision": parameter_revision,
        "value_digest": sha256_object(
            {
                "obs_key": f"W{observation['window_no']}:{observation['instance_id']}",
                "metric": metric_name,
                "fixed_value": fixed_value,
                "scale": scale,
                "decimal_value": format(decimal_truth, "f"),
            }
        ),
    }
