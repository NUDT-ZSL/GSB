from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Iterable

from .jsonutil import (
    canonical_decimal,
    fraction_string,
)
from .model import (
    DERIVED_ORDER,
    PARAM_FOR_DERIVED,
    PARAM_ORDER,
    Snapshot,
)
from .numeric import (
    FixedValue,
    add_values,
    converted_value,
    divide_values,
    entry_from_operands,
    fixed_from_conversion_entry,
    min_values,
    multiply_values,
    rescale_value,
)


@dataclass(frozen=True)
class ConversionReuse:
    entry: dict[str, Any]
    fixed: FixedValue


@dataclass
class ServiceComputeResult:
    payload: dict[str, Any]
    entries: dict[str, dict[str, Any]]
    new_carries: dict[str, Fraction]
    reused_derived: list[str]
    recomputed_derived: list[str]
    reused_conversions: dict[str, int]
    recomputed_conversions: list[str]


class ServiceLedger:
    def __init__(self, service: str) -> None:
        self.service = service
        self.entries: dict[str, dict[str, Any]] = {}
        self.by_number: dict[int, dict[str, Any]] = {}
        self.next_no = 1

    def add(
        self,
        entry: dict[str, Any],
        *,
        registration_no: int | None = None,
    ) -> None:
        key = entry["registration_key"]
        if key in self.entries:
            if self.entries[key] != entry:
                raise ValueError(f"ledger registration conflict for {key}")
            return
        if registration_no is None:
            registration_no = self.next_no
        number = int(registration_no)
        if number in self.by_number:
            raise ValueError(
                f"duplicate registration number {number} for {self.service}"
            )
        stored = dict(entry)
        stored["registration_no"] = number
        self.entries[key] = stored
        self.by_number[number] = stored
        self.next_no = max(self.next_no, number + 1)

    def add_reused(self, entry: dict[str, Any]) -> None:
        self.add(entry, registration_no=int(entry["registration_no"]))

    def next_registration(self) -> int:
        return self.next_no

    def get(self, key: str) -> dict[str, Any] | None:
        return self.entries.get(key)

    def ordered(self) -> list[dict[str, Any]]:
        return [self.by_number[number] for number in sorted(self.by_number)]


def compute_truth(snapshot: Snapshot) -> dict[str, dict[str, Decimal]]:
    context_rounding = getattr(decimal, snapshot.config.truth_rounding)
    precision = snapshot.config.truth_precision
    by_name = snapshot.service_map()
    results: dict[str, dict[str, Decimal]] = {}

    with localcontext() as context:
        context.prec = precision
        context.rounding = context_rounding
        for name in snapshot.topological_order():
            service = by_name[name]
            values = snapshot.layers[service.layer]
            timeout = _decimal(values["timeout_ms"])
            retry = _decimal(values["retry_multiplier"])
            sample_rate = _decimal(values["sample_rate"])
            capacity = _decimal(values["capacity"])
            weight = _decimal(values["weight"])

            upstream = [
                (dependency, results[dependency])
                for dependency in service.depends_on
            ]
            local_timeout = timeout * retry
            worst_timeout = local_timeout
            if upstream:
                worst_timeout += max(
                    item["worst_chain_timeout"] for _, item in upstream
                )

            if upstream:
                effective_capacity = min(
                    capacity,
                    sum(
                        (item["effective_capacity"] for _, item in upstream),
                        Decimal(0),
                    ),
                )
            else:
                effective_capacity = capacity

            numerator = sample_rate * weight
            denominator = weight
            for dependency, item in upstream:
                dependency_weight = _decimal(
                    snapshot.layers[
                        by_name[dependency].layer
                    ]["weight"]
                )
                numerator += item["weighted_sample_rate"] * dependency_weight
                denominator += dependency_weight
            weighted_sample_rate = (
                numerator / denominator if denominator != 0 else Decimal(0)
            )
            results[name] = {
                "effective_capacity": effective_capacity,
                "weighted_sample_rate": weighted_sample_rate,
                "worst_chain_timeout": worst_timeout,
            }
    return results


def _decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


class MetricOps:
    def __init__(
        self,
        *,
        ledger: ServiceLedger,
        service: str,
        metric: str,
        config_budget: Fraction,
        derived_scale: int,
    ) -> None:
        self.ledger = ledger
        self.service = service
        self.metric = metric
        self.config_budget = config_budget
        self.derived_scale = derived_scale
        self.op_index = 0
        self.entries: list[dict[str, Any]] = []

    def _key(self, suffix: str) -> str:
        return f"derive:{self.metric}:{suffix}"

    def _register(
        self,
        *,
        suffix: str,
        operation: str,
        weight_position: str,
        output: FixedValue,
        remainder: Fraction,
        operands: list[FixedValue],
    ) -> dict[str, Any]:
        self.op_index += 1
        key = self._key(f"{self.op_index}:{suffix}")
        entry = entry_from_operands(
            service=self.service,
            registration_no=self.ledger.next_registration(),
            registration_key=key,
            parameter=self.metric,
            operation=operation,
            weight_position=weight_position,
            output=output,
            remainder=remainder,
            operands=operands,
            configured_budget=self.config_budget,
        )
        self.ledger.add(entry)
        self.entries.append(entry)
        return entry

    def add(
        self,
        left: FixedValue,
        right: FixedValue,
        *,
        weight_position: str,
    ) -> FixedValue:
        output, remainder = add_values(
            left, right, target_scale=self.derived_scale
        )
        self._register(
            suffix="add",
            operation="ADD",
            weight_position=weight_position,
            output=output,
            remainder=remainder,
            operands=[left, right],
        )
        return output

    def multiply(
        self,
        left: FixedValue,
        right: FixedValue,
        *,
        weight_position: str,
    ) -> FixedValue:
        output, remainder = multiply_values(
            left, right, target_scale=self.derived_scale
        )
        self._register(
            suffix="mul",
            operation="MUL",
            weight_position=weight_position,
            output=output,
            remainder=remainder,
            operands=[left, right],
        )
        return output

    def minimum(
        self,
        left: FixedValue,
        right: FixedValue,
        *,
        weight_position: str,
    ) -> FixedValue:
        output, remainder = min_values(
            left, right, target_scale=self.derived_scale
        )
        self._register(
            suffix="min",
            operation="MIN",
            weight_position=weight_position,
            output=output,
            remainder=remainder,
            operands=[left, right],
        )
        return output

    def divide(
        self,
        numerator: FixedValue,
        denominator: FixedValue,
        *,
        weight_position: str,
    ) -> FixedValue:
        output, remainder, zero = divide_values(
            numerator, denominator, target_scale=self.derived_scale
        )
        self._register(
            suffix="div",
            operation="DIV",
            weight_position=weight_position,
            output=output,
            remainder=remainder,
            operands=[numerator, denominator],
        )
        return output

    def rescale(
        self, value: FixedValue, *, weight_position: str
    ) -> FixedValue:
        output, remainder = rescale_value(value, target_scale=self.derived_scale)
        self._register(
            suffix="rescale",
            operation="RESCALE",
            weight_position=weight_position,
            output=output,
            remainder=remainder,
            operands=[value],
        )
        return output


def compute_service(
    snapshot: Snapshot,
    service_name: str,
    *,
    truth: dict[str, Any],
    upstream_payloads: dict[str, dict[str, Any]],
    previous_payload: dict[str, Any] | None = None,
    previous_entries: dict[str, dict[str, Any]] | None = None,
    reuse_metrics: Iterable[str] = (),
    changed_parameters: Iterable[str] = (),
    carry_inputs: dict[str, Fraction] | None = None,
    forced_conversion_entries: dict[str, dict[str, Any]] | None = None,
) -> ServiceComputeResult:
    service = snapshot.service_map()[service_name]
    config = snapshot.config
    previous_entries = previous_entries or {}
    carry_inputs = carry_inputs or {}
    reuse_set = set(reuse_metrics)
    changed_set = set(changed_parameters)
    ledger = ServiceLedger(service_name)
    conversions: dict[str, FixedValue] = {}
    conversion_entries: dict[str, dict[str, Any]] = {}
    new_carries: dict[str, Fraction] = {}
    reused_conversions: dict[str, int] = {}
    recomputed_conversions: list[str] = []

    for index, parameter in enumerate(PARAM_ORDER):
        key = f"convert:{parameter}"
        prior = previous_entries.get(key)
        forced = (
            forced_conversion_entries.get(parameter)
            if forced_conversion_entries is not None
            else None
        )
        if forced is not None:
            ledger.add_reused(forced)
            conversion_entries[parameter] = forced
            conversions[parameter] = fixed_from_conversion_entry(
                forced,
                error_budget=config.parameters[parameter].error_budget,
            )
            continue
        can_reuse = (
            prior is not None
            and parameter not in changed_set
            and previous_payload is not None
        )
        if can_reuse:
            ledger.add_reused(prior)
            conversion_entries[parameter] = prior
            conversions[parameter] = fixed_from_conversion_entry(
                prior,
                error_budget=config.parameters[parameter].error_budget,
            )
            reused_conversions[parameter] = int(prior["registration_no"])
            continue

        value = snapshot.layers[service.layer][parameter]
        carry_in = carry_inputs.get(parameter, Fraction(0))
        spec = config.parameters[parameter]
        fixed, details = converted_value(
            value,
            scale=spec.scale,
            carry_in=carry_in,
            error_budget=spec.error_budget,
        )
        entry = {
            "bound_after": canonical_decimal(fixed.bound, precision=120),
            "configured_budget": canonical_decimal(spec.error_budget),
            "direction": details["direction"],
            "discarded_units": details["discarded_units"],
            "discarded_value": details["discarded_value"],
            "operation": "CONVERT",
            "output_scale": fixed.scale,
            "output_units": fixed.units,
            "parameter": parameter,
            "registration_key": key,
            "registration_no": index + 1,
            "service": service_name,
            "carry_in": details["carry_in"],
            "carry_out": details["carry_out"],
            "input": details["input"],
            "weight_position": f"service.{service_name}.{parameter}",
        }
        ledger.add(entry, registration_no=index + 1)
        conversion_entries[parameter] = entry
        conversions[parameter] = fixed
        new_carries[parameter] = Fraction(
            int(details["carry_out"].split("/", 1)[0]),
            int(details["carry_out"].split("/", 1)[1]),
        )
        recomputed_conversions.append(parameter)

    derived_payload: dict[str, Any] = {}
    reused_derived: list[str] = []
    recomputed_derived: list[str] = []
    own_metric_entries: dict[str, list[dict[str, Any]]] = {}

    for metric in DERIVED_ORDER:
        if metric in reuse_set and previous_payload is not None:
            metric_payload = previous_payload["derived"][metric]
            derived_payload[metric] = metric_payload
            reused_derived.append(metric)
            for source in metric_payload["sources"]:
                if source["service"] == service_name:
                    ledger.add_reused(source)
            continue

        ops = MetricOps(
            ledger=ledger,
            service=service_name,
            metric=metric,
            config_budget=(
                sum(
                    (
                        config.parameters[name].error_budget
                        for name in PARAM_FOR_DERIVED[metric]
                    ),
                    Fraction(0),
                )
            ),
            derived_scale=config.derived_scales[metric],
        )
        if metric == "worst_chain_timeout":
            fixed_value, upstream_sources = _timeout_metric(
                ops,
                service,
                conversions,
                upstream_payloads,
            )
        elif metric == "effective_capacity":
            fixed_value, upstream_sources = _capacity_metric(
                ops,
                service,
                conversions,
                upstream_payloads,
            )
        else:
            fixed_value, upstream_sources = _sample_rate_metric(
                ops,
                service,
                conversions,
                upstream_payloads,
                snapshot,
            )

        sources = _collect_sources(
            service_name=service_name,
            metric=metric,
            conversion_entries=conversion_entries,
            upstream_sources=upstream_sources,
            arithmetic_entries=ops.entries,
        )
        metric_payload = _metric_payload(
            fixed_value=fixed_value,
            truth_value=truth[metric],
            sources=sources,
            config=config,
        )
        derived_payload[metric] = metric_payload
        own_metric_entries[metric] = ops.entries
        recomputed_derived.append(metric)

    payload = {
        "derived": derived_payload,
        "layer": service.layer,
        "service": service_name,
    }
    return ServiceComputeResult(
        payload=payload,
        entries=ledger.entries,
        new_carries=new_carries,
        reused_derived=reused_derived,
        recomputed_derived=recomputed_derived,
        reused_conversions=reused_conversions,
        recomputed_conversions=recomputed_conversions,
    )


def _timeout_metric(
    ops: MetricOps,
    service: Any,
    conversions: dict[str, FixedValue],
    upstream_payloads: dict[str, dict[str, Any]],
) -> tuple[FixedValue, list[dict[str, Any]]]:
    local = ops.multiply(
        conversions["timeout_ms"],
        conversions["retry_multiplier"],
        weight_position=f"service.{service.name}.timeout_ms*retry_multiplier",
    )
    if not service.depends_on:
        return local, []
    upstream = [
        upstream_payloads[name]["derived"]["worst_chain_timeout"]
        for name in service.depends_on
    ]
    chosen_index = 0
    chosen_real = Fraction(
        int(upstream[0]["fixed_units"]), 10 ** int(upstream[0]["fixed_scale"])
    )
    for index in range(1, len(upstream)):
        candidate = Fraction(
            int(upstream[index]["fixed_units"]),
            10 ** int(upstream[index]["fixed_scale"]),
        )
        if candidate > chosen_real:
            chosen_index = index
            chosen_real = candidate
    chosen_name = service.depends_on[chosen_index]
    chosen_payload = upstream[chosen_index]
    upstream_fixed = FixedValue(
        units=int(chosen_payload["fixed_units"]),
        scale=int(chosen_payload["fixed_scale"]),
        low=Fraction(chosen_payload["error_interval"]["low"]),
        high=Fraction(chosen_payload["error_interval"]["high"]),
    )
    output = ops.add(
        local,
        upstream_fixed,
        weight_position=f"upstream.{chosen_name}.worst_chain_timeout",
    )
    return output, [chosen_payload]


def _capacity_metric(
    ops: MetricOps,
    service: Any,
    conversions: dict[str, FixedValue],
    upstream_payloads: dict[str, dict[str, Any]],
) -> tuple[FixedValue, list[dict[str, Any]]]:
    own = ops.rescale(
        conversions["capacity"],
        weight_position=f"service.{service.name}.capacity",
    )
    if not service.depends_on:
        return own, []
    upstream_payloads_list = [
        upstream_payloads[name]["derived"]["effective_capacity"]
        for name in service.depends_on
    ]
    total = FixedValue(
        units=int(upstream_payloads_list[0]["fixed_units"]),
        scale=int(upstream_payloads_list[0]["fixed_scale"]),
        low=Fraction(upstream_payloads_list[0]["error_interval"]["low"]),
        high=Fraction(upstream_payloads_list[0]["error_interval"]["high"]),
    )
    for index in range(1, len(upstream_payloads_list)):
        payload = upstream_payloads_list[index]
        value = FixedValue(
            units=int(payload["fixed_units"]),
            scale=int(payload["fixed_scale"]),
            low=Fraction(payload["error_interval"]["low"]),
            high=Fraction(payload["error_interval"]["high"]),
        )
        total = ops.add(
            total,
            value,
            weight_position=f"upstream.{service.depends_on[index]}.effective_capacity",
        )
    output = ops.minimum(
        own,
        total,
        weight_position=f"service.{service.name}.capacity_vs_upstream_sum",
    )
    return output, upstream_payloads_list


def _sample_rate_metric(
    ops: MetricOps,
    service: Any,
    conversions: dict[str, FixedValue],
    upstream_payloads: dict[str, dict[str, Any]],
    snapshot: Snapshot,
) -> tuple[FixedValue, list[dict[str, Any]]]:
    own_weight = conversions["weight"]
    own_term = ops.multiply(
        conversions["sample_rate"],
        own_weight,
        weight_position=f"service.{service.name}.sample_rate*weight",
    )
    numerator = own_term
    denominator = ops.rescale(
        own_weight,
        weight_position=f"service.{service.name}.weight",
    )
    upstream_sources: list[dict[str, Any]] = []
    for dependency in service.depends_on:
        payload = upstream_payloads[dependency]["derived"]["weighted_sample_rate"]
        upstream_sources.append(payload)
        upstream_weight_entry = _conversion_entry_for(
            upstream_payloads, dependency, "weight"
        )
        upstream_weight = fixed_from_conversion_entry(
            upstream_weight_entry,
            error_budget=snapshot.config.parameters["weight"].error_budget,
        )
        upstream_rate = FixedValue(
            units=int(payload["fixed_units"]),
            scale=int(payload["fixed_scale"]),
            low=Fraction(payload["error_interval"]["low"]),
            high=Fraction(payload["error_interval"]["high"]),
        )
        term = ops.multiply(
            upstream_rate,
            upstream_weight,
            weight_position=f"upstream.{dependency}.weighted_sample_rate*weight",
        )
        numerator = ops.add(
            numerator,
            term,
            weight_position=f"upstream.{dependency}.sample_rate_term",
        )
        denominator = ops.add(
            denominator,
            upstream_weight,
            weight_position=f"upstream.{dependency}.weight",
        )
    output = ops.divide(
        numerator,
        denominator,
        weight_position=f"service.{service.name}.weighted_average",
    )
    return output, upstream_sources


def _conversion_entry_for(
    upstream_payloads: dict[str, dict[str, Any]],
    dependency: str,
    parameter: str,
) -> dict[str, Any]:
    payload = upstream_payloads[dependency]
    for source in payload["derived"]["weighted_sample_rate"]["sources"]:
        if (
            source["service"] == dependency
            and source["operation"] == "CONVERT"
            and source["parameter"] == parameter
        ):
            return source
    raise ValueError(f"missing conversion source {dependency}.{parameter}")


def _collect_sources(
    *,
    service_name: str,
    metric: str,
    conversion_entries: dict[str, dict[str, Any]],
    upstream_sources: list[dict[str, Any]],
    arithmetic_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def append(entry: dict[str, Any]) -> None:
        key = (entry["service"], int(entry["registration_no"]))
        if key in seen:
            return
        seen.add(key)
        result.append(entry)

    for parameter in PARAM_FOR_DERIVED[metric]:
        append(conversion_entries[parameter])
    for source_payload in upstream_sources:
        for source in source_payload["sources"]:
            append(source)
    for entry in arithmetic_entries:
        append(entry)
    return result


def _metric_payload(
    *,
    fixed_value: FixedValue,
    truth_value: Decimal,
    sources: list[dict[str, Any]],
    config: Any,
) -> dict[str, Any]:
    precision = config.truth_precision
    rounding = getattr(decimal, config.truth_rounding)
    with localcontext() as context:
        context.prec = precision
        context.rounding = rounding
        fixed_decimal = Decimal(fixed_value.units) / Decimal(10**fixed_value.scale)
        abs_deviation = abs(fixed_decimal - truth_value)
        relative_deviation = (
            abs_deviation / abs(truth_value) if truth_value != 0 else Decimal(0)
        )
        bound_decimal = Decimal(fixed_value.bound.numerator) / Decimal(
            fixed_value.bound.denominator
        )
        ledger_ok = Fraction(abs_deviation) <= fixed_value.bound
        first_defect = None
        if not ledger_ok:
            first_defect = _first_defect(
                sources=sources,
                abs_deviation=abs_deviation,
            )
    return {
        "abs_deviation": canonical_decimal(abs_deviation),
        "error_bound": canonical_decimal(bound_decimal),
        "error_interval": {
            "high": fraction_string(fixed_value.high),
            "low": fraction_string(fixed_value.low),
        },
        "first_defect": first_defect,
        "fixed": canonical_decimal(fixed_decimal),
        "fixed_scale": fixed_value.scale,
        "fixed_units": fixed_value.units,
        "ledger_ok": ledger_ok,
        "rel_deviation": canonical_decimal(relative_deviation),
        "sources": sources,
        "truth": canonical_decimal(truth_value),
    }


def _first_defect(
    *, sources: list[dict[str, Any]], abs_deviation: Decimal
) -> dict[str, Any] | None:
    chosen = sources[0] if sources else None
    for source in sources:
        bound = Decimal(source["bound_after"])
        if abs_deviation > bound:
            chosen = source
            break
    if chosen is None:
        return None
    excess = abs_deviation - Decimal(chosen["bound_after"])
    return {
        "bound_after": chosen["bound_after"],
        "operation": chosen["operation"],
        "parameter": chosen["parameter"],
        "registration_no": chosen["registration_no"],
        "service": chosen["service"],
        "unexplained_excess": canonical_decimal(excess),
    }


def compare_sensitive_pairs(
    snapshot: Snapshot,
    service_payloads: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    config = snapshot.config
    tolerance = Decimal(config.compare_tolerance.numerator) / Decimal(
        config.compare_tolerance.denominator
    )
    names = sorted(service_payloads)
    pairs: list[dict[str, Any]] = []
    with localcontext() as context:
        context.prec = config.truth_precision
        context.rounding = getattr(decimal, config.truth_rounding)
        for metric in DERIVED_ORDER:
            scale = config.derived_scales[metric]
            higher_scale = scale + config.higher_scale_increment
            for left_index, left in enumerate(names):
                for right in names[left_index + 1 :]:
                    left_metric = service_payloads[left]["derived"][metric]
                    right_metric = service_payloads[right]["derived"][metric]
                    left_truth = Decimal(left_metric["truth"])
                    right_truth = Decimal(right_metric["truth"])
                    truth_delta = right_truth - left_truth
                    if abs(truth_delta) >= tolerance:
                        continue
                    left_fixed = _fixed_decimal(left_metric)
                    right_fixed = _fixed_decimal(right_metric)
                    fixed_delta = right_fixed - left_fixed
                    higher_left_units = _higher_units(left_truth, higher_scale)
                    higher_right_units = _higher_units(right_truth, higher_scale)
                    higher_left = Decimal(higher_left_units) / Decimal(
                        10**higher_scale
                    )
                    higher_right = Decimal(higher_right_units) / Decimal(
                        10**higher_scale
                    )
                    fixed_order = _ordering(left_fixed, right_fixed)
                    truth_order = _ordering(left_truth, right_truth)
                    higher_order = _ordering(higher_left, higher_right)
                    pair_key = f"{metric}|{left}|{right}"
                    pairs.append(
                        {
                            "comparison": metric,
                            "fixed_delta": canonical_decimal(fixed_delta),
                            "fixed_order": fixed_order,
                            "higher_scale": higher_scale,
                            "higher_scale_fixed_delta": canonical_decimal(
                                higher_right - higher_left
                            ),
                            "higher_scale_order": higher_order,
                            "higher_scale_values": {
                                "left": canonical_decimal(higher_left),
                                "right": canonical_decimal(higher_right),
                            },
                            "ledger_refs": {
                                "left": _ledger_refs(left_metric),
                                "right": _ledger_refs(right_metric),
                            },
                            "left": left,
                            "left_abs_deviation": left_metric["abs_deviation"],
                            "left_error_bound": left_metric["error_bound"],
                            "ordering_depends_on_delta": truth_order != "EQ",
                            "pair_key": pair_key,
                            "rel_deviation_left": left_metric["rel_deviation"],
                            "rel_deviation_right": right_metric["rel_deviation"],
                            "right": right,
                            "right_abs_deviation": right_metric["abs_deviation"],
                            "right_error_bound": right_metric["error_bound"],
                            "tolerance": canonical_decimal(tolerance),
                            "truth_delta": canonical_decimal(truth_delta),
                            "truth_order": truth_order,
                            "within_tolerance": True,
                            "would_flip_at_higher_scale": (
                                higher_order != fixed_order
                            ),
                        }
                    )
    return sorted(pairs, key=lambda pair: pair["pair_key"])


def _fixed_decimal(metric: dict[str, Any]) -> Decimal:
    return Decimal(metric["fixed"])


def _higher_units(value: Decimal, scale: int) -> int:
    scaled = value * (Decimal(10) ** scale)
    return int(scaled.to_integral_value(rounding=decimal.ROUND_FLOOR))


def _ordering(left: Decimal, right: Decimal) -> str:
    if left < right:
        return "LT"
    if left > right:
        return "GT"
    return "EQ"


def _ledger_refs(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "registration_no": int(source["registration_no"]),
            "service": source["service"],
        }
        for source in metric["sources"]
    ]
