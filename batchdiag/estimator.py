from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

import numpy as np

from .jsonutil import decimal_string
from .precision import DECIMAL_PRECISION, DualBuilder, DualNumber
from .spec import ModelSpec, PrecisionConfig, ReplicaSpec, RequestSpec


FORMULA_TERMS = (
    "input_token",
    "output_token",
    "parameter",
    "batch_size",
    "queue_depth",
    "carry",
    "queue_carry",
)


def _previous_number(
    previous: dict[str, Any] | None, metric: str
) -> tuple[DualNumber | None, str | None, list[str]]:
    if not previous:
        return None, None, []
    record = previous.get(metric)
    if not record:
        return None, None, []
    batch_id = str(record["batch_id"])
    source = f"batch:{batch_id}.{metric}"
    value = DualNumber(
        exact=Decimal(record["pre_conversion_raw"]),
        fixed_scaled=int(record["fixed_scaled"]),
        fixed_bound=Decimal(record["fixed_bound"]),
        float_value=np.float64(float(record["float64"])),
        float_bound=Decimal(record["float_bound"]),
        sources=frozenset((source,)),
    )
    return value, batch_id, list(record.get("accumulation_chain", ()))


def estimate_metric(
    *,
    metric: str,
    requests: list[RequestSpec],
    model: ModelSpec,
    replica: ReplicaSpec,
    config: PrecisionConfig,
    queue_depth: int,
    previous: dict[str, Any] | None,
    batch_id: str,
) -> dict[str, Any]:
    """Evaluate one estimate through fixed-point and NumPy float64 paths."""
    formula = config.formulas[metric]
    scale = config.scales[metric]
    builder = DualBuilder(metric=metric, scale=scale, rounding=config.rounding)
    input_tokens = sum(request.input_tokens for request in requests)
    output_tokens = sum(request.output_tokens for request in requests)
    request_ids = [request.request_id for request in requests]
    request_source = "requests:" + ",".join(str(value) for value in request_ids)
    queue_source = f"queue_watermark:{queue_depth}"

    value = builder.constant(formula["base"], f"formula:{metric}.base", "base")

    def add_product(
        current: DualNumber,
        coefficient_name: str,
        count: int,
        count_source: str,
        label: str,
    ) -> DualNumber:
        coefficient = builder.constant(
            formula[coefficient_name],
            f"formula:{metric}.{coefficient_name}",
            f"coefficient.{coefficient_name}",
        )
        count_value = builder.integer(count, count_source, f"count.{label}")
        product = builder.multiply(coefficient, count_value, f"{label}.multiply")
        return builder.add(current, product, f"{label}.add")

    value = add_product(value, "input_token", input_tokens, request_source, "input_tokens")
    value = add_product(value, "output_token", output_tokens, request_source, "output_tokens")
    value = add_product(
        value, "parameter", model.total_parameters, "formula:model.total_parameters", "parameters"
    )
    value = add_product(value, "batch_size", len(requests), request_source, "batch_size")
    value = add_product(value, "queue_depth", queue_depth, queue_source, "queue_depth")

    previous_value, previous_batch_id, previous_chain = _previous_number(previous, metric)
    if previous_value is not None and formula["carry"] != 0:
        coefficient = builder.constant(
            formula["carry"], f"formula:{metric}.carry", "coefficient.carry"
        )
        carried = builder.multiply(
            coefficient, previous_value, "previous_batch.carry_multiply"
        )
        value = builder.add(value, carried, "previous_batch.carry_add")
    if previous_value is not None and formula["queue_carry"] != 0 and queue_depth > 0:
        coefficient = builder.constant(
            formula["queue_carry"],
            f"formula:{metric}.queue_carry",
            "coefficient.queue_carry",
        )
        depth = builder.integer(queue_depth, queue_source, "count.queue_watermark")
        scaled_previous = builder.multiply(
            depth, previous_value, "previous_batch.watermark_multiply"
        )
        carried = builder.multiply(
            coefficient, scaled_previous, "previous_batch.watermark_carry_multiply"
        )
        value = builder.add(value, carried, "previous_batch.watermark_carry_add")

    finalized = builder.finalize(value)
    fixed = finalized["fixed"]
    float64 = finalized["float64"]
    chain = list(previous_chain)
    if batch_id not in chain:
        chain.append(batch_id)

    formula_sources = sorted(
        source
        for source in value.sources
        if source.startswith("formula:")
    )
    propagation = {
        "previous_batch": (
            {
                "batch_id": previous_batch_id,
                "metric": metric,
                "source": f"batch:{previous_batch_id}.{metric}",
                "fixed_input_bound": decimal_string(previous_value.fixed_bound),
                "float64_input_bound": decimal_string(previous_value.float_bound),
            }
            if previous_value is not None
            else None
        ),
        "queue_watermark": {
            "depth": queue_depth,
            "source": queue_source,
            "used_multiplicatively": bool(
                previous_value is not None
                and formula["queue_carry"] != 0
                and queue_depth > 0
            ),
        },
        "formula_parameters": formula_sources,
        "request_ids": request_ids,
    }
    record = {
        "metric": metric,
        "fixed": fixed,
        "float64": float64,
        "cumulative_error_bound": finalized["cumulative_error_bound"],
        "bound_basis": finalized["bound_basis"],
        "propagation": propagation,
        "propagation_sources": finalized["propagation_sources"],
        "accumulation_chain": chain,
        "previous_batch_id": previous_batch_id,
        "scale": scale,
        "budget": decimal_string(config.error_budget[metric]),
    }
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        record["value"] = float(float64["value"])
        record["fixed_value"] = float(Decimal(fixed["scaled_string"]) / Decimal(scale))
        record["exact_value"] = float(value.exact)
    return record


def estimate_batch(
    *,
    batch_id: str,
    requests: list[RequestSpec],
    model: ModelSpec,
    replica: ReplicaSpec,
    config: PrecisionConfig,
    queue_depth: int,
    previous: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    return {
        metric: estimate_metric(
            metric=metric,
            requests=requests,
            model=model,
            replica=replica,
            config=config,
            queue_depth=queue_depth,
            previous=previous,
            batch_id=batch_id,
        )
        for metric in ("compute", "memory", "queue_wait")
    }


def compact_cumulative(
    estimates: dict[str, dict[str, Any]], batch_id: str
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for metric, estimate in estimates.items():
        result[metric] = {
            "batch_id": batch_id,
            "fixed_scaled": estimate["fixed"]["scaled_string"],
            "fixed_value": estimate["fixed"]["value"],
            "fixed_bound": estimate["fixed"]["cumulative_error_bound"],
            "float64": estimate["float64"]["value"],
            "float_bound": estimate["float64"]["cumulative_error_bound"],
            "pre_conversion_raw": estimate["fixed"]["pre_conversion_raw"],
            "cumulative_error_bound": estimate["cumulative_error_bound"],
            "accumulation_chain": estimate["accumulation_chain"],
        }
    return result


def estimate_compute_for_replica(
    *,
    requests: list[RequestSpec],
    model: ModelSpec,
    replica: ReplicaSpec,
    config: PrecisionConfig,
    queue_depth: int,
    previous: dict[str, Any] | None,
    batch_id: str,
) -> dict[str, Any]:
    return estimate_metric(
        metric="compute",
        requests=requests,
        model=model,
        replica=replica,
        config=config,
        queue_depth=queue_depth,
        previous=previous,
        batch_id=batch_id,
    )
