from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, localcontext
from typing import Any, Iterable

from .errors import bad_config
from .fixed import (
    DECIMAL_PRECISION,
    INPUT_SCALE,
    convert_input_decimal,
    convert_decl,
    decimal_abs,
    decimal_value,
    fixed_decimal,
    floor_div,
    stable_decimal,
)
from .models import ConfigDecl, ensure_required_roles
from .records import ConfigBundle, StoredConfig
from .util import sha256_json


@dataclass(frozen=True)
class Event:
    event_id: str
    value: Decimal
    weight: Decimal
    value_text: str
    weight_text: str


def normalize_events(raw_events: Any) -> list[Event]:
    if not isinstance(raw_events, list) or not raw_events:
        raise bad_config("events 必须是非空数组", field="events")
    events: list[Event] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_events):
        if not isinstance(item, dict):
            raise bad_config("event 必须是对象", index=index)
        event_id = item.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise bad_config("event.id 必须是非空字符串", index=index)
        if event_id in seen:
            raise bad_config("event.id 必须唯一", event_id=event_id)
        seen.add(event_id)
        value = decimal_value(item.get("value"))
        weight = decimal_value(item.get("weight"))
        if not value.is_finite() or not weight.is_finite():
            raise bad_config("event value/weight 必须有限", event_id=event_id)
        events.append(
            Event(
                event_id=event_id,
                value=value,
                weight=weight,
                value_text=stable_decimal(value),
                weight_text=stable_decimal(weight),
            )
        )
    return sorted(events, key=lambda event: event.event_id)


def _cfg(bundle: ConfigBundle, role: str) -> StoredConfig:
    try:
        cfg = bundle.by_role(role)
    except KeyError as exc:
        raise bad_config("计算所需配置 role 缺失", role=role) from exc
    if cfg.value_decimal is None:
        raise bad_config("计算所需配置不是数值类型", role=role, config_key=cfg.key)
    return cfg


def _fixed_int(cfg: StoredConfig) -> int:
    if cfg.fixed_int is None:
        raise bad_config("配置缺少定点换算结果", config_key=cfg.key)
    return cfg.fixed_int


def _cfg_fixed_value(cfg: StoredConfig) -> Decimal:
    return fixed_decimal(_fixed_int(cfg), cfg.scale)


def _cfg_error(cfg: StoredConfig) -> Decimal:
    return decimal_abs(_cfg_fixed_value(cfg) - cfg.value_decimal)  # type: ignore[operator]


def _source_entry(
    cfg: StoredConfig,
    *,
    sensitivity: Decimal,
    contribution: Decimal,
    note: str,
) -> dict[str, Any]:
    converted_value = (
        _cfg_fixed_value(cfg) if cfg.fixed_int is not None else None
    )
    return {
        "config_key": cfg.key,
        "role": cfg.role,
        "version": cfg.conversion_version,
        "conversion_id": cfg.conversion_id,
        "scale": cfg.scale,
        "carried_in_units": stable_decimal(cfg.carried_in_units),
        "discarded_units": stable_decimal(cfg.discarded_units),
        "residual_units": stable_decimal(cfg.residual_units),
        "residual": stable_decimal(cfg.residual_value),
        "declared_value": cfg.value_text,
        "converted_value": (
            stable_decimal(converted_value) if converted_value is not None else None
        ),
        "conversion_error": (
            stable_decimal(converted_value - cfg.value_decimal)
            if converted_value is not None and cfg.value_decimal is not None
            else None
        ),
        "sensitivity": stable_decimal(sensitivity),
        "contribution": stable_decimal(contribution),
        "note": note,
    }


def _merge_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, int | None], dict[str, Any]] = {}
    for source in sources:
        key = (source["config_key"], source["conversion_id"])
        if key not in merged:
            merged[key] = dict(source)
            continue
        target = merged[key]
        target["contribution"] = stable_decimal(
            Decimal(target["contribution"]) + Decimal(source["contribution"])
        )
        if source["note"] not in target["note"]:
            target["note"] = target["note"] + "; " + source["note"]
    return sorted(
        merged.values(),
        key=lambda item: (
            item["config_key"],
            item["version"] if item["version"] is not None else -1,
            item["conversion_id"] if item["conversion_id"] is not None else -1,
        ),
    )


def _metric(
    *,
    metric: str,
    formula: str,
    fixed: Decimal | int,
    exact: Decimal | int,
    bound: Decimal,
    sources: list[dict[str, Any]],
    fixed_int: int | None = None,
    scale: int | None = None,
    bound_components: dict[str, Any] | None = None,
    boundary_events: Iterable[str] = (),
) -> dict[str, Any]:
    fixed_dec = Decimal(fixed)
    exact_dec = Decimal(exact)
    deviation = fixed_dec - exact_dec
    fixed_text = (
        stable_decimal(fixed_dec)
        if not isinstance(fixed, int)
        else str(fixed)
    )
    exact_text = (
        stable_decimal(exact_dec)
        if not isinstance(exact, int)
        else str(exact)
    )
    return {
        "metric": metric,
        "formula": formula,
        "fixed": fixed_text,
        "exact": exact_text,
        "deviation": stable_decimal(deviation),
        "absolute_deviation": stable_decimal(decimal_abs(deviation)),
        "error_bound": stable_decimal(bound),
        "bound_ok": decimal_abs(deviation) <= bound + Decimal("1e-30"),
        "fixed_int": str(fixed_int) if fixed_int is not None else None,
        "scale": scale,
        "sources": _merge_sources(sources),
        "bound_components": bound_components or {},
        "boundary_events": sorted(set(boundary_events)),
    }


def _input_weighted_term(
    event: Event,
    *,
    input_scale: int,
    effective_weight_int: int,
    effective_weight_scale: int,
) -> tuple[int, Decimal]:
    value_int, value_residual = convert_input_decimal(event.value, input_scale)
    weight_int, weight_residual = convert_input_decimal(event.weight, input_scale)
    product = floor_div(value_int * weight_int, input_scale)
    weighted = floor_div(product * effective_weight_int, effective_weight_scale)
    quant_error = (
        decimal_abs(Decimal(value_int) / input_scale - event.value)
        * decimal_abs(event.weight)
        + decimal_abs(Decimal(weight_int) / input_scale - event.weight)
        * decimal_abs(event.value)
        + Decimal(2) / Decimal(input_scale)
    ) * decimal_abs(Decimal(effective_weight_int) / Decimal(effective_weight_scale))
    return weighted, quant_error


def compute_bundle(
    bundle: ConfigBundle,
    events: list[Event],
    *,
    tolerance: Decimal,
    logical_clock_ms: int,
    input_digest: str,
    input_scale_factor: int = 1,
    probe: bool = False,
) -> dict[str, Any]:
    ensure_required_roles(
        {key: cfg.declaration() for key, cfg in bundle.configs.items()}
    )
    timeout = _cfg(bundle, "timeout_ms")
    retry = _cfg(bundle, "retry_factor")
    sample_rate = _cfg(bundle, "sample_rate")
    rate_limit = _cfg(bundle, "rate_limit_qps")
    capacity = _cfg(bundle, "capacity_limit")
    threshold_cfg = _cfg(bundle, "filter_threshold")
    alpha = _cfg(bundle, "weight_alpha")
    beta = _cfg(bundle, "weight_beta")
    gamma = next(
        (cfg for cfg in bundle.configs.values() if cfg.role == "weight_gamma"),
        None,
    )
    output_ratio = _cfg(bundle, "output_ratio")

    input_scale = INPUT_SCALE * input_scale_factor
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION

        timeout_exact = timeout.value_decimal  # type: ignore[assignment]
        retry_exact = retry.value_decimal  # type: ignore[assignment]
        sample_exact = sample_rate.value_decimal  # type: ignore[assignment]
        rate_exact = rate_limit.value_decimal  # type: ignore[assignment]
        capacity_exact = capacity.value_decimal  # type: ignore[assignment]
        threshold_exact_config = threshold_cfg.value_decimal  # type: ignore[assignment]
        alpha_exact = alpha.value_decimal  # type: ignore[assignment]
        beta_exact = beta.value_decimal  # type: ignore[assignment]
        gamma_exact = gamma.value_decimal if gamma is not None else Decimal(0)
        output_exact = output_ratio.value_decimal  # type: ignore[assignment]

        timeout_fixed = _fixed_int(timeout)
        retry_fixed = _fixed_int(retry)
        sample_fixed = _fixed_int(sample_rate)
        rate_fixed = _fixed_int(rate_limit)
        capacity_fixed = _fixed_int(capacity)
        threshold_fixed = _fixed_int(threshold_cfg)
        alpha_fixed = _fixed_int(alpha)
        beta_fixed = _fixed_int(beta)
        gamma_fixed = _fixed_int(gamma) if gamma is not None else 0

        threshold_int = floor_div(threshold_fixed * retry_fixed, retry.scale)
        threshold_fixed_value = fixed_decimal(threshold_int, threshold_cfg.scale)
        exact_threshold = threshold_exact_config * retry_exact

        effective_weight_int = alpha_fixed + beta_fixed + gamma_fixed
        effective_weight_value = fixed_decimal(effective_weight_int, alpha.scale)
        exact_effective_weight = alpha_exact + beta_exact + gamma_exact

        raw_batch_exact = rate_exact * timeout_exact / Decimal(1000)
        raw_batch_fixed = floor_div(rate_fixed * timeout_fixed, 1000)
        base_batch_fixed = min(capacity_fixed, raw_batch_fixed)
        expected_batch_int = floor_div(base_batch_fixed * sample_fixed, sample_rate.scale)
        expected_batch_value = Decimal(expected_batch_int)
        exact_base_batch = min(capacity_exact, raw_batch_exact)
        exact_expected_batch = exact_base_batch * sample_exact

        threshold_input_fixed = floor_div(
            threshold_int * input_scale, threshold_cfg.scale
        )
        fixed_events: list[tuple[Event, int, int, Decimal]] = []
        for event in events:
            value_fixed, value_residual = convert_input_decimal(event.value, input_scale)
            weight_fixed, weight_residual = convert_input_decimal(
                event.weight, input_scale
            )
            fixed_events.append(
                (event, value_fixed, weight_fixed, value_residual + weight_residual)
            )

        fixed_filtered = [
            item for item in fixed_events if item[1] >= threshold_input_fixed
        ]
        exact_filtered = [
            event for event in events if event.value >= exact_threshold
        ]
        fixed_processed = fixed_filtered[: max(0, expected_batch_int)]
        exact_batch_items = max(
            0,
            int(
                exact_expected_batch.to_integral_value(rounding=ROUND_FLOOR)
            ),
        )
        exact_processed = exact_filtered[:exact_batch_items]

        threshold_bound = (
            decimal_abs(retry_exact) * _cfg_error(threshold_cfg)
            + decimal_abs(threshold_exact_config) * _cfg_error(retry)
            + Decimal(1) / Decimal(threshold_cfg.scale)
        )
        threshold_sources = [
            _source_entry(
                threshold_cfg,
                sensitivity=retry_exact,
                contribution=decimal_abs(retry_exact) * _cfg_error(threshold_cfg),
                note="过滤阈值换算",
            ),
            _source_entry(
                retry,
                sensitivity=threshold_exact_config,
                contribution=decimal_abs(threshold_exact_config) * _cfg_error(retry),
                note="重试倍率参与阈值乘算",
            ),
        ]
        threshold_metric = _metric(
            metric="threshold",
            formula="floor(filter_threshold_int * retry_factor_int / retry_scale) / filter_scale",
            fixed=threshold_fixed_value,
            exact=exact_threshold,
            fixed_int=threshold_int,
            scale=threshold_cfg.scale,
            bound=threshold_bound,
            sources=threshold_sources,
            bound_components={
                "conversion_terms": stable_decimal(
                    threshold_bound - Decimal(1) / Decimal(threshold_cfg.scale)
                ),
                "operation_quantization": stable_decimal(
                    Decimal(1) / Decimal(threshold_cfg.scale)
                ),
            },
            boundary_events=_boundary_ids(
                events,
                exact_threshold=exact_threshold,
                threshold_bound=threshold_bound,
                threshold_input_fixed=threshold_input_fixed,
                input_scale=input_scale,
            ),
        )

        weight_bound = (
            _cfg_error(alpha)
            + _cfg_error(beta)
            + (_cfg_error(gamma) if gamma is not None else Decimal(0))
        )
        weight_sources = [
            _source_entry(alpha, sensitivity=Decimal(1), contribution=_cfg_error(alpha), note="权重项 alpha"),
            _source_entry(beta, sensitivity=Decimal(1), contribution=_cfg_error(beta), note="权重项 beta"),
        ]
        if gamma is not None:
            weight_sources.append(
                _source_entry(
                    gamma,
                    sensitivity=Decimal(1),
                    contribution=_cfg_error(gamma),
                    note="权重项 gamma",
                )
            )
        weight_metric = _metric(
            metric="effective_weight",
            formula="(weight_alpha_int + weight_beta_int + weight_gamma_int) / weight_scale",
            fixed=effective_weight_value,
            exact=exact_effective_weight,
            fixed_int=effective_weight_int,
            scale=alpha.scale,
            bound=weight_bound,
            sources=weight_sources,
            bound_components={
                "conversion_terms": stable_decimal(weight_bound),
                "operation_quantization": "0",
            },
        )

        if raw_batch_exact <= capacity_exact:
            batch_sources = [
                _source_entry(
                    rate_limit,
                    sensitivity=timeout_exact / Decimal(1000) * sample_exact,
                    contribution=_cfg_error(rate_limit)
                    * decimal_abs(timeout_exact / Decimal(1000) * sample_exact),
                    note="限流阈值参与批大小",
                ),
                _source_entry(
                    timeout,
                    sensitivity=rate_exact / Decimal(1000) * sample_exact,
                    contribution=_cfg_error(timeout)
                    * decimal_abs(rate_exact / Decimal(1000) * sample_exact),
                    note="超时参与批大小",
                ),
                _source_entry(
                    capacity,
                    sensitivity=Decimal(0),
                    contribution=Decimal(0),
                    note="容量上限未成为 min 分支",
                ),
                _source_entry(
                    sample_rate,
                    sensitivity=exact_base_batch,
                    contribution=_cfg_error(sample_rate) * decimal_abs(exact_base_batch),
                    note="采样率缩减批大小",
                ),
            ]
            batch_conversion_bound = (
                _cfg_error(rate_limit)
                * decimal_abs(timeout_exact / Decimal(1000) * sample_exact)
                + _cfg_error(timeout)
                * decimal_abs(rate_exact / Decimal(1000) * sample_exact)
                + _cfg_error(sample_rate) * decimal_abs(exact_base_batch)
            )
        else:
            batch_sources = [
                _source_entry(
                    capacity,
                    sensitivity=sample_exact,
                    contribution=_cfg_error(capacity) * decimal_abs(sample_exact),
                    note="容量上限成为 min 分支",
                ),
                _source_entry(
                    sample_rate,
                    sensitivity=capacity_exact,
                    contribution=_cfg_error(sample_rate) * decimal_abs(capacity_exact),
                    note="采样率缩减批大小",
                ),
                _source_entry(
                    rate_limit,
                    sensitivity=Decimal(0),
                    contribution=Decimal(0),
                    note="限流阈值未成为 min 分支",
                ),
                _source_entry(
                    timeout,
                    sensitivity=Decimal(0),
                    contribution=Decimal(0),
                    note="超时未成为 min 分支",
                ),
            ]
            batch_conversion_bound = (
                _cfg_error(capacity) * decimal_abs(sample_exact)
                + _cfg_error(sample_rate) * decimal_abs(capacity_exact)
            )
        batch_quantization = Decimal(2)
        batch_bound = batch_conversion_bound + batch_quantization
        batch_metric = _metric(
            metric="expected_batch",
            formula="min(capacity, floor(rate_limit * timeout_ms / 1000)) * sample_rate",
            fixed=expected_batch_value,
            exact=exact_expected_batch,
            fixed_int=expected_batch_int,
            scale=1,
            bound=batch_bound,
            sources=batch_sources,
            bound_components={
                "conversion_terms": stable_decimal(batch_conversion_bound),
                "operation_quantization": stable_decimal(batch_quantization),
            },
        )

        boundary_ids = set(threshold_metric["boundary_events"])
        fixed_filtered_ids = [event.event_id for event, _, _, _ in fixed_filtered]
        exact_filtered_ids = [event.event_id for event in exact_filtered]
        eligible_count_bound = Decimal(len(boundary_ids))
        eligible_metric = _metric(
            metric="eligible_count",
            formula="count(event.value >= threshold)",
            fixed=len(fixed_filtered),
            exact=len(exact_filtered),
            bound=eligible_count_bound,
            sources=[
                _source_entry(
                    threshold_cfg,
                    sensitivity=retry_exact,
                    contribution=Decimal(0),
                    note="阈值边界事件集合来源",
                ),
                _source_entry(
                    retry,
                    sensitivity=threshold_exact_config,
                    contribution=Decimal(0),
                    note="阈值边界事件集合来源",
                ),
            ],
            bound_components={
                "discrete_boundary_events": len(boundary_ids),
                "operation_quantization": "0",
            },
            boundary_events=boundary_ids,
        )

        processed_count_bound = Decimal(len(boundary_ids) + 1)
        processed_metric = _metric(
            metric="processed_count",
            formula="count(first expected_batch eligible events by id)",
            fixed=len(fixed_processed),
            exact=len(exact_processed),
            bound=processed_count_bound,
            sources=_merge_sources(
                [
                    _source_entry(threshold_cfg, sensitivity=retry_exact, contribution=Decimal(0), note="过滤阈值影响处理集合"),
                    _source_entry(retry, sensitivity=threshold_exact_config, contribution=Decimal(0), note="重试倍率影响过滤阈值"),
                    _source_entry(capacity, sensitivity=sample_exact, contribution=Decimal(0), note="容量上限影响批大小"),
                    _source_entry(sample_rate, sensitivity=capacity_exact, contribution=Decimal(0), note="采样率影响批大小"),
                    _source_entry(rate_limit, sensitivity=timeout_exact, contribution=Decimal(0), note="限流阈值影响批大小"),
                    _source_entry(timeout, sensitivity=rate_exact, contribution=Decimal(0), note="超时影响批大小"),
                ]
            ),
            bound_components={
                "discrete_boundary_events": len(boundary_ids),
                "batch_rounding": 1,
            },
            boundary_events=boundary_ids,
        )

        exact_weighted_sum = sum(
            (event.value * event.weight * exact_effective_weight for event in exact_processed),
            Decimal(0),
        )
        fixed_weighted_units = 0
        input_quantization_bound = Decimal(0)
        for event, value_fixed, weight_fixed, _ in fixed_processed:
            product = floor_div(value_fixed * weight_fixed, input_scale)
            fixed_weighted_units += floor_div(
                product * effective_weight_int, alpha.scale
            )
            _, one_bound = _input_weighted_term(
                event,
                input_scale=input_scale,
                effective_weight_int=effective_weight_int,
                effective_weight_scale=alpha.scale,
            )
            input_quantization_bound += one_bound
        fixed_weighted_sum = Decimal(fixed_weighted_units) / Decimal(input_scale)

        exact_processed_ids = {event.event_id for event in exact_processed}
        fixed_processed_ids = {event.event_id for event, _, _, _ in fixed_processed}
        boundary_contribution = Decimal(0)
        boundary_weighted_ids: set[str] = set()
        for event in events:
            if event.event_id in (exact_processed_ids ^ fixed_processed_ids):
                boundary_weighted_ids.add(event.event_id)
                boundary_contribution += (
                    decimal_abs(event.value * event.weight * exact_effective_weight)
                )
            elif event.event_id in boundary_ids:
                boundary_contribution += (
                    decimal_abs(event.value * event.weight * exact_effective_weight)
                )
                boundary_weighted_ids.add(event.event_id)
        next_cutoff_contribution = Decimal(0)
        for event in exact_filtered:
            if event.event_id not in exact_processed_ids and len(boundary_weighted_ids) < (
                len(boundary_ids) + abs(len(exact_processed) - len(fixed_processed)) + 1
            ):
                next_cutoff_contribution += decimal_abs(
                    event.value * event.weight * exact_effective_weight
                )

        total_exact_for_weights = sum(
            (
                event.value * event.weight
                for event in exact_processed
            ),
            Decimal(0),
        )
        weighted_sources = [
            _source_entry(
                alpha,
                sensitivity=total_exact_for_weights,
                contribution=_cfg_error(alpha)
                * decimal_abs(total_exact_for_weights),
                note="聚合权重 alpha",
            ),
            _source_entry(
                beta,
                sensitivity=total_exact_for_weights,
                contribution=_cfg_error(beta)
                * decimal_abs(total_exact_for_weights),
                note="聚合权重 beta",
            ),
            _source_entry(
                threshold_cfg,
                sensitivity=Decimal(0),
                contribution=Decimal(0),
                note="过滤阈值决定聚合事件集合",
            ),
            _source_entry(
                retry,
                sensitivity=Decimal(0),
                contribution=Decimal(0),
                note="重试倍率决定过滤阈值",
            ),
            _source_entry(
                sample_rate,
                sensitivity=Decimal(0),
                contribution=Decimal(0),
                note="采样率决定处理事件批",
            ),
            _source_entry(
                timeout,
                sensitivity=Decimal(0),
                contribution=Decimal(0),
                note="超时决定处理事件批",
            ),
            _source_entry(
                rate_limit,
                sensitivity=Decimal(0),
                contribution=Decimal(0),
                note="限流阈值决定处理事件批",
            ),
            _source_entry(
                capacity,
                sensitivity=Decimal(0),
                contribution=Decimal(0),
                note="容量上限决定处理事件批",
            ),
        ]
        if gamma is not None:
            weighted_sources.append(
                _source_entry(
                    gamma,
                    sensitivity=total_exact_for_weights,
                    contribution=_cfg_error(gamma)
                    * decimal_abs(total_exact_for_weights),
                    note="聚合权重 gamma",
                )
            )
        weighted_conversion_bound = (
            decimal_abs(total_exact_for_weights)
            * (
                _cfg_error(alpha)
                + _cfg_error(beta)
                + (_cfg_error(gamma) if gamma is not None else Decimal(0))
            )
        )
        weighted_bound = (
            weighted_conversion_bound
            + boundary_contribution
            + next_cutoff_contribution
            + input_quantization_bound
        )
        weighted_metric = _metric(
            metric="weighted_sum",
            formula="sum(floor(value*weight*effective_weight) in fixed path over first batch)",
            fixed=fixed_weighted_sum,
            exact=exact_weighted_sum,
            fixed_int=fixed_weighted_units,
            scale=input_scale,
            bound=weighted_bound,
            sources=weighted_sources,
            bound_components={
                "conversion_terms": stable_decimal(weighted_conversion_bound),
                "discrete_event_set": stable_decimal(boundary_contribution),
                "batch_cutoff": stable_decimal(next_cutoff_contribution),
                "input_quantization": stable_decimal(input_quantization_bound),
            },
            boundary_events=boundary_weighted_ids,
        )

        output_count_int = floor_div(len(fixed_processed) * _fixed_int(output_ratio), output_ratio.scale)
        exact_output_count = Decimal(len(exact_processed)) * output_exact
        output_bound = (
            Decimal(len(boundary_ids) + 1) * decimal_abs(output_exact)
            + Decimal(len(exact_processed)) * _cfg_error(output_ratio)
            + Decimal(1)
        )
        output_metric = _metric(
            metric="output_count",
            formula="floor(processed_count * output_ratio)",
            fixed=output_count_int,
            exact=exact_output_count,
            fixed_int=output_count_int,
            scale=1,
            bound=output_bound,
            sources=[
                _source_entry(
                    output_ratio,
                    sensitivity=Decimal(len(exact_processed)),
                    contribution=Decimal(len(exact_processed))
                    * _cfg_error(output_ratio),
                    note="输出比例",
                ),
                _source_entry(
                    sample_rate,
                    sensitivity=Decimal(0),
                    contribution=Decimal(0),
                    note="采样率影响处理事件数",
                ),
                _source_entry(
                    threshold_cfg,
                    sensitivity=Decimal(0),
                    contribution=Decimal(0),
                    note="过滤阈值影响处理事件数",
                ),
            ],
            bound_components={
                "discrete_event_set": stable_decimal(
                    Decimal(len(boundary_ids) + 1) * decimal_abs(output_exact)
                ),
                "output_ratio_conversion": stable_decimal(
                    Decimal(len(exact_processed)) * _cfg_error(output_ratio)
                ),
                "operation_quantization": "1",
            },
            boundary_events=boundary_ids,
        )

        metrics = {
            "threshold": threshold_metric,
            "effective_weight": weight_metric,
            "expected_batch": batch_metric,
            "eligible_count": eligible_metric,
            "processed_count": processed_metric,
            "weighted_sum": weighted_metric,
            "output_count": output_metric,
        }

        fixed_conclusion = {
            "filtered_ids": fixed_filtered_ids,
            "processed_ids": [event.event_id for event, _, _, _ in fixed_processed],
            "eligible_count": len(fixed_filtered),
            "processed_count": len(fixed_processed),
            "output_count": output_count_int,
        }
        exact_conclusion = {
            "filtered_ids": exact_filtered_ids,
            "processed_ids": [event.event_id for event in exact_processed],
            "eligible_count": len(exact_filtered),
            "processed_count": len(exact_processed),
            "output_count": int(
                exact_output_count.to_integral_value(rounding=ROUND_FLOOR)
            ),
        }
        sensitivity = _sensitivity_report(
            metrics=metrics,
            fixed_conclusion=fixed_conclusion,
            exact_conclusion=exact_conclusion,
            tolerance=tolerance,
        )

        result: dict[str, Any] = {
            "namespace": bundle.namespace,
            "bound_version": bundle.version,
            "bound_digest": bundle.digest,
            "logical_clock_ms": logical_clock_ms,
            "input_digest": input_digest,
            "input_scale": input_scale,
            "probe": probe,
            "events": [
                {
                    "id": event.event_id,
                    "value": event.value_text,
                    "weight": event.weight_text,
                }
                for event in events
            ],
            "metric_order": [
                "threshold",
                "effective_weight",
                "expected_batch",
                "eligible_count",
                "processed_count",
                "weighted_sum",
                "output_count",
            ],
            "metrics": metrics,
            "fixed_conclusion": fixed_conclusion,
            "exact_conclusion": exact_conclusion,
            "sensitivity": sensitivity,
        }
        result["error_ledger"] = _flatten_error_ledger(metrics)
        digest_payload = {key: value for key, value in result.items() if key != "result_digest"}
        result["result_digest"] = sha256_json(digest_payload)
        return result


def _boundary_ids(
    events: list[Event],
    *,
    exact_threshold: Decimal,
    threshold_bound: Decimal,
    threshold_input_fixed: int,
    input_scale: int,
) -> list[str]:
    result: list[str] = []
    for event in events:
        value_fixed, _ = convert_input_decimal(event.value, input_scale)
        fixed_member = value_fixed >= threshold_input_fixed
        exact_member = event.value >= exact_threshold
        near_boundary = (
            decimal_abs(event.value - exact_threshold)
            <= threshold_bound + Decimal("1e-30")
        )
        if fixed_member != exact_member or near_boundary:
            result.append(event.event_id)
    return result


def _sensitivity_report(
    *,
    metrics: dict[str, dict[str, Any]],
    fixed_conclusion: dict[str, Any],
    exact_conclusion: dict[str, Any],
    tolerance: Decimal,
) -> dict[str, Any]:
    differences: list[dict[str, Any]] = []
    mapping = [
        ("threshold", "filtered_ids", "过滤阈值"),
        ("expected_batch", "processed_ids", "期望批大小"),
        ("output_count", "output_count", "输出比例"),
    ]
    for metric_name, conclusion_key, label in mapping:
        fixed_value = fixed_conclusion[conclusion_key]
        exact_value = exact_conclusion[conclusion_key]
        if fixed_value == exact_value:
            continue
        metric = metrics[metric_name]
        delta = Decimal(metric["absolute_deviation"])
        differences.append(
            {
                "metric": metric_name,
                "label": label,
                "fixed": fixed_value,
                "exact": exact_value,
                "fixed_vs_exact_gap": metric["absolute_deviation"],
                "tolerance": stable_decimal(tolerance),
                "within_tolerance": delta <= tolerance,
                "detail": (
                    f"{label}在两通路产生不同的离散结论；"
                    f"派生量绝对偏差 {metric['absolute_deviation']} "
                    f"{'小于' if delta <= tolerance else '不小于'}容差 {stable_decimal(tolerance)}。"
                ),
            }
        )
    reasons = [item for item in differences if item["within_tolerance"]]
    return {
        "tolerance": stable_decimal(tolerance),
        "precision_sensitive": bool(reasons),
        "reasons": reasons,
        "differences": differences,
        "fixed_conclusion": fixed_conclusion,
        "exact_conclusion": exact_conclusion,
    }


def _flatten_error_ledger(metrics: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for metric_name, metric in metrics.items():
        for source in metric["sources"]:
            rows.append(
                {
                    "metric": metric_name,
                    "formula": metric["formula"],
                    **source,
                }
            )
    return rows


def make_probe_bundle(bundle: ConfigBundle, factor: int) -> ConfigBundle:
    if factor <= 0:
        raise ValueError("probe factor must be positive")
    probe_configs: dict[str, StoredConfig] = {}
    for key, cfg in bundle.configs.items():
        if not cfg.declaration().is_numeric or cfg.value_decimal is None:
            probe_configs[key] = StoredConfig(
                key=cfg.key,
                value_type=cfg.value_type,
                value_text=cfg.value_text,
                value_decimal=cfg.value_decimal,
                scale=cfg.scale,
                role=cfg.role,
                fixed_int=cfg.fixed_int,
                carried_in_units=cfg.carried_in_units,
                discarded_units=cfg.discarded_units,
                residual_units=cfg.residual_units,
                residual_value=cfg.residual_value,
                conversion_id=None,
                conversion_version=None,
            )
            continue
        decl = ConfigDecl(
            key=cfg.key,
            value_type=cfg.value_type,
            value_text=cfg.value_text,
            value_decimal=cfg.value_decimal,
            scale=cfg.scale * factor,
            role=cfg.role,
        )
        record = convert_decl(decl, Decimal(0))
        probe_configs[key] = StoredConfig(
            key=cfg.key,
            value_type=cfg.value_type,
            value_text=cfg.value_text,
            value_decimal=cfg.value_decimal,
            scale=decl.scale,
            role=cfg.role,
            fixed_int=record.fixed_int,
            carried_in_units=record.carried_in,
            discarded_units=record.discarded,
            residual_units=record.carried_out,
            residual_value=record.residual_value,
            conversion_id=None,
            conversion_version=None,
        )
    return ConfigBundle(
        namespace=bundle.namespace,
        version=bundle.version,
        digest=bundle.digest,
        content_digest=bundle.content_digest,
        logical_clock_ms=bundle.logical_clock_ms,
        configs=probe_configs,
    )


def compare_metric_sets(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    tolerance: Decimal,
    left_label: str,
    right_label: str,
) -> dict[str, Any]:
    metric_names = [
        "threshold",
        "effective_weight",
        "expected_batch",
        "eligible_count",
        "processed_count",
        "weighted_sum",
        "output_count",
    ]
    rows: list[dict[str, Any]] = []
    sensitive = False
    for name in metric_names:
        lm = left["metrics"][name]
        rm = right["metrics"][name]
        left_fixed = Decimal(lm["fixed"])
        right_fixed = Decimal(rm["fixed"])
        left_exact = Decimal(lm["exact"])
        right_exact = Decimal(rm["exact"])
        fixed_delta = left_fixed - right_fixed
        exact_delta = left_exact - right_exact
        below_tolerance = decimal_abs(fixed_delta) <= tolerance
        rounding_only = exact_delta == 0
        conclusion_changed = _metric_conclusion(left, right, name)
        precision_sensitive = (
            below_tolerance
            and fixed_delta != 0
            and conclusion_changed
            and (rounding_only or decimal_abs(exact_delta) <= tolerance)
        )
        sensitive = sensitive or precision_sensitive
        rows.append(
            {
                "metric": name,
                "left_fixed": lm["fixed"],
                "right_fixed": rm["fixed"],
                "fixed_delta": stable_decimal(fixed_delta),
                "left_exact": lm["exact"],
                "right_exact": rm["exact"],
                "exact_delta": stable_decimal(exact_delta),
                "absolute_fixed_delta": stable_decimal(decimal_abs(fixed_delta)),
                "tolerance": stable_decimal(tolerance),
                "within_tolerance": below_tolerance,
                "rounding_only": rounding_only,
                "conclusion_changed": conclusion_changed,
                "precision_sensitive": precision_sensitive,
            }
        )
    return {
        "left": left_label,
        "right": right_label,
        "tolerance": stable_decimal(tolerance),
        "metrics": rows,
        "precision_sensitive": sensitive,
    }


def _metric_conclusion(
    left: dict[str, Any],
    right: dict[str, Any],
    metric_name: str,
) -> bool:
    if metric_name == "threshold":
        return left["fixed_conclusion"]["filtered_ids"] != right["fixed_conclusion"]["filtered_ids"]
    if metric_name == "expected_batch":
        return left["fixed_conclusion"]["processed_ids"] != right["fixed_conclusion"]["processed_ids"]
    if metric_name == "output_count":
        return left["fixed_conclusion"]["output_count"] != right["fixed_conclusion"]["output_count"]
    if metric_name == "eligible_count":
        return left["fixed_conclusion"]["eligible_count"] != right["fixed_conclusion"]["eligible_count"]
    if metric_name == "processed_count":
        return left["fixed_conclusion"]["processed_count"] != right["fixed_conclusion"]["processed_count"]
    return False


def conclusion_signature(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "filtered_ids": sorted(result["exact_conclusion"]["filtered_ids"]),
        "processed_ids": sorted(result["exact_conclusion"]["processed_ids"]),
        "output_count": result["exact_conclusion"]["output_count"],
    }

