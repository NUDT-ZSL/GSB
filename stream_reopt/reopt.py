"""Drift detection, candidate costing, and reproducible re-optimization."""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, localcontext
from typing import Any

from .join import ExecutionResult, StageObservation
from .models import BatchSpec, ExecutionShape, Scenario
from .precision import drift_ledger, observation_ledger, precision_evidence


def _predicate_ids_for_stage(scenario: Scenario, stage: StageObservation) -> tuple[str, ...]:
    return stage.predicate_ids


def _estimate_for_predicates(scenario: Scenario, predicate_ids: Sequence[str]) -> float:
    selectivities = scenario.initial_stats.selectivity_map
    estimate = 1.0
    for predicate_id in predicate_ids:
        estimate *= selectivities[predicate_id]
    return estimate


def _participating_tables(scenario: Scenario, predicate_ids: Sequence[str]) -> list[str]:
    predicate_map = scenario.plan.predicate_map()
    names: set[str] = set()
    for predicate_id in predicate_ids:
        predicate = predicate_map[predicate_id]
        names.add(predicate.left_table)
        names.add(predicate.right_table)
    return sorted(names)


def observation_records(
    scenario: Scenario,
    result: ExecutionResult,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    scale = scenario.config.fixed_scale
    for stage in result.stages:
        estimate = _estimate_for_predicates(scenario, stage.predicate_ids)
        point = observation_ledger(stage, scale)
        point["estimate_selectivity"] = estimate
        point["participating_tables"] = _participating_tables(scenario, stage.predicate_ids)
        records.append(
            {
                "batch_id": result.batch_id,
                "join_id": stage.join_id,
                "predicate_ids": list(stage.predicate_ids),
                "source_table": stage.source_table,
                "source_key": stage.source_key,
                "added_table": stage.added_table,
                "participating_tables": point["participating_tables"],
                "algorithm": stage.algorithm,
                "left_rows": stage.left_rows,
                "right_rows": stage.right_rows,
                "input_rows": stage.input_rows,
                "pair_count": stage.pair_count,
                "output_rows": stage.output_rows,
                "actual_numerator": stage.selectivity_numerator,
                "actual_denominator": stage.selectivity_denominator,
                "estimate_selectivity": estimate,
                "observation_ledger": point,
                "shape_version": result.shape.version,
            }
        )
    if not records:
        from .errors import BusinessError, ErrorCode

        raise BusinessError(
            ErrorCode.EMPTY_OBSERVATION,
            "execution produced no join observations",
            details={"batch_id": result.batch_id},
        )
    return records


def assess_drift(
    scenario: Scenario,
    *,
    current_records: Sequence[Mapping[str, Any]],
    history_records: Sequence[Mapping[str, Any]],
    current_batch: BatchSpec,
    last_decision_ms: int | None,
) -> dict[str, Any]:
    if not current_records:
        from .errors import BusinessError, ErrorCode

        raise BusinessError(
            ErrorCode.EMPTY_OBSERVATION,
            "drift observation sample count is zero",
            details={"batch_id": current_batch.batch_id},
        )
    scale = scenario.config.fixed_scale
    threshold = scenario.config.drift_threshold
    window_size = scenario.config.observation_window
    current_time_ms = current_batch.arrival_seq * scenario.config.logical_clock_step_ms
    all_records = list(history_records) + list(current_records)
    sources: list[dict[str, Any]] = []
    for current in current_records:
        join_id = str(current["join_id"])
        candidates = [
            record
            for record in all_records
            if record.get("join_id") == join_id
            and int(record.get("arrival_seq", -1)) <= current_batch.arrival_seq
        ]
        candidates.sort(key=lambda record: (int(record.get("arrival_seq", -1)), str(record.get("batch_id"))))
        window = candidates[-window_size:]
        if not window:
            continue
        actual_numerator = sum(int(record["actual_numerator"]) for record in window)
        actual_denominator = sum(int(record["actual_denominator"]) for record in window)
        estimate = float(current["estimate_selectivity"])
        batch_ids = [str(record["batch_id"]) for record in window]
        seqs = [int(record.get("arrival_seq", -1)) for record in window]
        ratio, ledger = drift_ledger(
            actual_numerator=actual_numerator,
            actual_denominator=actual_denominator,
            estimate=estimate,
            scale=scale,
            batch_interval=batch_ids,
            observation_points=[record["observation_ledger"] for record in window],
            participating_tables=current["participating_tables"],
            source_table=str(current["source_table"]),
            source_key=str(current["source_key"]),
            join_id=join_id,
        )
        sources.append(
            {
                "join_id": join_id,
                "predicate_ids": current["predicate_ids"],
                "source_table": current["source_table"],
                "source_key": current["source_key"],
                "participating_tables": current["participating_tables"],
                "actual_selectivity": actual_numerator / actual_denominator,
                "estimated_selectivity": estimate,
                "deviation_ratio": ratio,
                "threshold": threshold,
                "threshold_margin": ratio - threshold,
                "exceeds_threshold": ratio > threshold,
                "sample_count": len(window),
                "batch_interval": batch_ids,
                "arrival_seq_interval": [min(seqs), max(seqs)],
                "error_ledger": ledger,
                "observed_at_logical_ms": current_time_ms,
            }
        )
    if not sources:
        from .errors import BusinessError, ErrorCode

        raise BusinessError(
            ErrorCode.EMPTY_OBSERVATION,
            "drift observation sample count is zero",
            details={"batch_id": current_batch.batch_id},
        )
    sources.sort(key=lambda item: (-item["deviation_ratio"], item["join_id"]))
    primary = sources[0]
    exceeds = [source for source in sources if source["exceeds_threshold"]]
    if not exceeds:
        return {
            "batch_id": current_batch.batch_id,
            "arrival_seq": current_batch.arrival_seq,
            "logical_time_ms": current_time_ms,
            "triggered": False,
            "suppressed": False,
            "reason": "BELOW_THRESHOLD",
            "threshold": threshold,
            "primary": primary,
            "sources": sources,
            "max_deviation_ratio": primary["deviation_ratio"],
            "threshold_margin": primary["threshold_margin"],
            "drift_increment": max(0.0, primary["deviation_ratio"] - threshold),
        }
    if last_decision_ms is not None:
        elapsed = current_time_ms - last_decision_ms
        if elapsed < scenario.config.cooldown_ms:
            suppressed = {
                "batch_id": current_batch.batch_id,
                "arrival_seq": current_batch.arrival_seq,
                "logical_time_ms": current_time_ms,
                "triggered": True,
                "suppressed": True,
                "reason": "COOLDOWN_ACTIVE",
                "remaining_cooldown_ms": scenario.config.cooldown_ms - elapsed,
                "threshold": threshold,
                "primary": primary,
                "sources": sources,
                "max_deviation_ratio": primary["deviation_ratio"],
                "threshold_margin": primary["threshold_margin"],
                "drift_increment": max(0.0, primary["deviation_ratio"] - threshold),
            }
            return suppressed
    return {
        "batch_id": current_batch.batch_id,
        "arrival_seq": current_batch.arrival_seq,
        "logical_time_ms": current_time_ms,
        "triggered": True,
        "suppressed": False,
        "reason": "THRESHOLD_EXCEEDED",
        "threshold": threshold,
        "primary": primary,
        "sources": sources,
        "max_deviation_ratio": primary["deviation_ratio"],
        "threshold_margin": primary["threshold_margin"],
        "drift_increment": max(0.0, primary["deviation_ratio"] - threshold),
    }


def _base_rows(scenario: Scenario, batch: BatchSpec) -> dict[str, int]:
    return batch.row_map


def _actual_selectivities(records: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    latest_by_join: dict[str, dict[str, Any]] = {}
    for record in records:
        join_id = str(record["join_id"])
        previous = latest_by_join.get(join_id)
        if previous is None or int(record.get("arrival_seq", -1)) >= int(previous.get("arrival_seq", -1)):
            latest_by_join[join_id] = dict(record)
    result: dict[str, dict[str, Any]] = {}
    for join_id, record in latest_by_join.items():
        result[join_id] = {
            "selectivity": float(record["actual_selectivity"])
            if "actual_selectivity" in record
            else float(record["actual_numerator"]) / float(record["actual_denominator"]),
            "numerator": int(record["actual_numerator"]),
            "denominator": int(record["actual_denominator"]),
        }
    return result


def _selectivity_for_predicates(
    scenario: Scenario,
    predicate_ids: Sequence[str],
    actual: Mapping[str, Mapping[str, Any]],
) -> tuple[float, Decimal, str]:
    join_id = "+".join(sorted(predicate_ids))
    if join_id in actual:
        item = actual[join_id]
        numerator = item["numerator"]
        denominator = item["denominator"]
        return (
            numerator / denominator,
            Decimal(numerator) / Decimal(denominator),
            "observed",
        )
    selectivities = scenario.initial_stats.selectivity_map
    float_value = 1.0
    decimal_value = Decimal(1)
    for predicate_id in predicate_ids:
        value = selectivities[predicate_id]
        float_value *= value
        decimal_value *= Decimal(str(value))
    return float_value, decimal_value, "initial"


def _row_width(scenario: Scenario, tables: Sequence[str]) -> int:
    table_map = scenario.table_map
    return sum(
        column.width
        for table in tables
        for column in table_map[table].columns
    )


def _algorithm_cost(left: float, right: float, output: float, width: int, algorithm: str) -> float:
    io = (left + right) * max(1, width) / 1024.0 * 0.02
    if algorithm == "hash":
        return left + right + 1.15 * output + io
    if algorithm == "sort_merge":
        return 1.10 * (left * math.log2(left + 2) + right * math.log2(right + 2)) + left + right + 1.10 * output + io
    if algorithm == "nested_loop":
        return left * right + 1.20 * output + io
    raise AssertionError(f"unknown algorithm {algorithm}")


def _algorithm_cost_decimal(left: Decimal, right: Decimal, output: Decimal, width: int, algorithm: str) -> Decimal:
    io = (left + right) * Decimal(max(1, width)) / Decimal(1024) * Decimal("0.02")
    if algorithm == "hash":
        return left + right + Decimal("1.15") * output + io
    if algorithm == "sort_merge":
        return (
            Decimal("1.10")
            * (left * (left + 2).ln() + right * (right + 2).ln())
            + left
            + right
            + Decimal("1.10") * output
            + io
        )
    if algorithm == "nested_loop":
        return left * right + Decimal("1.20") * output + io
    raise AssertionError(f"unknown algorithm {algorithm}")


def _candidate_cost_float(
    scenario: Scenario,
    shape: ExecutionShape,
    base_rows: Mapping[str, int],
    actual: Mapping[str, Mapping[str, Any]],
) -> tuple[float, list[dict[str, Any]]]:
    left = float(base_rows[shape.join_order[0]])
    total = 0.0
    stage_estimates: list[dict[str, Any]] = []
    joined: list[str] = [shape.join_order[0]]
    for step, next_table in enumerate(shape.join_order[1:]):
        right = float(base_rows[next_table])
        predicates = scenario.plan.predicates_for_step(joined, next_table)
        predicate_ids = sorted(predicate.id for predicate in predicates)
        selectivity, _, source = _selectivity_for_predicates(scenario, predicate_ids, actual)
        output = left * right * selectivity
        if selectivity > 0.0 and output < 1.0:
            output = 1.0
        width = _row_width(scenario, joined + [next_table])
        cost = _algorithm_cost(left, right, output, width, shape.algorithms[step])
        stage_estimates.append(
            {
                "step": step,
                "added_table": next_table,
                "algorithm": shape.algorithms[step],
                "predicate_ids": predicate_ids,
                "selectivity": selectivity,
                "selectivity_source": source,
                "left_rows": left,
                "right_rows": right,
                "output_rows": output,
                "cost": cost,
            }
        )
        total += cost
        joined.append(next_table)
        left = output
    return total, stage_estimates


def _candidate_cost_decimal(
    scenario: Scenario,
    shape: ExecutionShape,
    base_rows: Mapping[str, int],
    actual: Mapping[str, Mapping[str, Any]],
) -> Decimal:
    left = Decimal(base_rows[shape.join_order[0]])
    total = Decimal(0)
    joined: list[str] = [shape.join_order[0]]
    with localcontext() as ctx:
        ctx.prec = 50
        for step, next_table in enumerate(shape.join_order[1:]):
            right = Decimal(base_rows[next_table])
            predicates = scenario.plan.predicates_for_step(joined, next_table)
            predicate_ids = sorted(predicate.id for predicate in predicates)
            _, selectivity, _ = _selectivity_for_predicates(scenario, predicate_ids, actual)
            output = left * right * selectivity
            if selectivity > 0 and output < 1:
                output = Decimal(1)
            width = _row_width(scenario, joined + [next_table])
            total += _algorithm_cost_decimal(left, right, output, width, shape.algorithms[step])
            left = output
            joined.append(next_table)
    return +total


def _switch_cost(
    scenario: Scenario,
    current_shape: ExecutionShape,
    base_rows: Mapping[str, int],
    actual: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    _, stages = _candidate_cost_float(scenario, current_shape, base_rows, actual)
    retained_rows = sum(round(stage["output_rows"]) for stage in stages)
    base = scenario.config.switch_base_cost
    state_component = 0.02 * retained_rows
    return {
        "base": base,
        "retained_state_rows": retained_rows,
        "state_component": state_component,
        "total": base + state_component,
        "formula": "switch_base_cost + 0.02 * sum(current_stage_estimated_rows)",
    }


def _candidate_shapes(scenario: Scenario, current: ExecutionShape) -> list[ExecutionShape]:
    names = list(scenario.plan.table_order)
    primary_algorithms = ("hash", "sort_merge", "nested_loop")
    seen: set[str] = set()
    shapes: list[ExecutionShape] = []
    for order in itertools.permutations(names):
        if not scenario.plan.valid_order(order):
            continue
        for algorithms in itertools.product(primary_algorithms, repeat=max(0, len(names) - 1)):
            shape = ExecutionShape(version=current.version + 1, join_order=order, algorithms=algorithms, reason="candidate")
            if shape.content_digest in seen:
                continue
            seen.add(shape.content_digest)
            shapes.append(shape)
            if len(shapes) >= scenario.config.max_candidates:
                break
        if len(shapes) >= scenario.config.max_candidates:
            break
    return shapes


def choose_shape(
    scenario: Scenario,
    *,
    current_shape: ExecutionShape,
    batch: BatchSpec,
    history_records: Sequence[Mapping[str, Any]],
    drift: Mapping[str, Any],
) -> dict[str, Any]:
    if not scenario.config.enable_reopt:
        return {
            "status": "RETAINED",
            "reason": "REOPT_DISABLED",
            "previous": current_shape.to_dict(),
            "selected": current_shape.to_dict(),
            "rejected_candidates": [],
            "error_code": None,
            "drift": {
                "join_id": drift["primary"]["join_id"],
                "source_table": drift["primary"]["source_table"],
                "source_key": drift["primary"]["source_key"],
                "deviation_ratio": drift["primary"]["deviation_ratio"],
                "batch_interval": drift["primary"]["batch_interval"],
                "threshold": drift["primary"]["threshold"],
                "threshold_margin": drift["primary"]["threshold_margin"],
                "sample_count": drift["primary"]["sample_count"],
            },
        }
    remaining_batches = max(0, len(scenario.plan.batches) - batch.arrival_seq - 1)
    actual = _actual_selectivities(history_records)
    base_rows = _base_rows(scenario, batch)
    current_per_batch, _current_stages = _candidate_cost_float(
        scenario, current_shape, base_rows, actual
    )
    current_total = current_per_batch * max(1, remaining_batches)
    switch = _switch_cost(scenario, current_shape, base_rows, actual)
    budget = scenario.config.switch_cost_budget
    candidates = _candidate_shapes(scenario, current_shape)
    evaluated: list[dict[str, Any]] = []
    for shape in candidates:
        if shape.content_digest == current_shape.content_digest:
            continue
        per_batch, stages = _candidate_cost_float(scenario, shape, base_rows, actual)
        total = per_batch * max(1, remaining_batches)
        saving = current_total - total
        net_saving = saving - switch["total"]
        affordable = switch["total"] <= budget
        if total >= current_total - scenario.config.float_tolerance:
            rejection = "NOT_BETTER"
        elif net_saving <= scenario.config.float_tolerance:
            rejection = "NO_NET_BENEFIT"
        elif not affordable:
            rejection = "OVER_BUDGET"
        elif remaining_batches == 0:
            rejection = "NO_REMAINING_WORK"
        else:
            rejection = None
        evaluated.append(
            {
                "shape": shape,
                "per_batch_cost": per_batch,
                "total_cost": total,
                "estimated_saving": saving,
                "net_saving": net_saving,
                "affordable": affordable,
                "rejection": rejection,
                "stages": stages,
            }
        )
    evaluated.sort(
        key=lambda item: (
            item["total_cost"],
            item["shape"].join_order,
            item["shape"].algorithms,
        )
    )
    precision_sensitive = False
    precision_recheck: dict[str, Any] | None = None
    if evaluated:
        best = evaluated[0]
        runner_up = evaluated[1] if len(evaluated) > 1 else None
        high_precision_best_shape = best["shape"]
        if runner_up is not None:
            margin = abs(best["total_cost"] - runner_up["total_cost"])
            if margin < scenario.config.float_tolerance:
                precision_sensitive = True
                best_hp = _candidate_cost_decimal(scenario, best["shape"], base_rows, actual)
                runner_hp = _candidate_cost_decimal(scenario, runner_up["shape"], base_rows, actual)
                float_winner = best["shape"].content_digest
                if runner_hp < best_hp:
                    high_precision_best_shape = runner_up["shape"]
                    high_precision_winner = runner_up["shape"].content_digest
                else:
                    high_precision_winner = best["shape"].content_digest
                involved_observations = [
                    record["observation_ledger"]
                    for record in history_records
                    if record.get("observation_ledger")
                ]
                batch_interval = sorted(
                    {
                        str(record.get("batch_id"))
                        for record in history_records
                    }
                )
                precision_recheck = {
                    "method": "decimal_precision_50",
                    "precision_sensitive": True,
                    "float_winner": float_winner,
                    "high_precision_winner": high_precision_winner,
                    "stable": float_winner == high_precision_winner,
                    "float_cost_delta": runner_up["total_cost"] - best["total_cost"],
                    "high_precision_cost_delta": float(best_hp - runner_hp),
                    "evidence": precision_evidence(
                        scenario,
                        winner=best["shape"].to_dict(),
                        runner_up=runner_up["shape"].to_dict(),
                        observations=involved_observations,
                        batch_interval=batch_interval,
                    ),
                }
        else:
            high_precision_best_shape = best["shape"]
        # Preserve the exact evaluated entry selected by the high precision path.
        if high_precision_best_shape.content_digest != evaluated[0]["shape"].content_digest:
            for index, item in enumerate(evaluated):
                if item["shape"].content_digest == high_precision_best_shape.content_digest:
                    evaluated.insert(0, evaluated.pop(index))
                    break
    selected_item = next((item for item in evaluated if item["rejection"] is None), None)
    rejected = [
        {
            "shape": item["shape"].to_dict(),
            "estimated_total_cost": item["total_cost"],
            "estimated_saving": item["estimated_saving"],
            "net_saving": item["net_saving"],
            "switch_cost": switch["total"],
            "budget": budget,
            "affordable": item["affordable"],
            "reason": item["rejection"] or "NOT_SELECTED_AFTER_PRECISION_RECHECK",
        }
        for item in evaluated
        if item is not selected_item
    ]
    if selected_item is None:
        beneficial = [item for item in evaluated if item["rejection"] != "NOT_BETTER" and item["net_saving"] > 0]
        budget_limited = any(item["rejection"] == "OVER_BUDGET" for item in beneficial)
        error_code = "E_SWITCH_BUDGET" if budget_limited else None
        reason = "SWITCH_BUDGET_EXCEEDED" if budget_limited else "NO_BENEFICIAL_SWITCH"
        return {
            "status": "RETAINED",
            "reason": reason,
            "error_code": error_code,
            "previous": current_shape.to_dict(),
            "selected": current_shape.to_dict(),
            "current_per_batch_cost": current_per_batch,
            "current_remaining_cost": current_total,
            "switch_cost": switch,
            "budget": budget,
            "remaining_batches": remaining_batches,
            "rejected_candidates": rejected,
            "precision_sensitive": precision_sensitive,
            "precision_recheck": precision_recheck,
            "drift": {
                "join_id": drift["primary"]["join_id"],
                "source_table": drift["primary"]["source_table"],
                "source_key": drift["primary"]["source_key"],
                "deviation_ratio": drift["primary"]["deviation_ratio"],
                "batch_interval": drift["primary"]["batch_interval"],
                "threshold": drift["primary"]["threshold"],
                "threshold_margin": drift["primary"]["threshold_margin"],
                "sample_count": drift["primary"]["sample_count"],
            },
        }
    chosen_shape = ExecutionShape(
        version=current_shape.version + 1,
        join_order=selected_item["shape"].join_order,
        algorithms=selected_item["shape"].algorithms,
        reason="drift_reoptimization",
    )
    return {
        "status": "SWITCHED",
        "reason": "LOWER_ESTIMATED_COST_WITHIN_BUDGET",
        "error_code": None,
        "previous": current_shape.to_dict(),
        "selected": chosen_shape.to_dict(),
        "selected_content_digest": chosen_shape.content_digest,
        "current_per_batch_cost": current_per_batch,
        "selected_per_batch_cost": selected_item["per_batch_cost"],
        "current_remaining_cost": current_total,
        "selected_remaining_cost": selected_item["total_cost"],
        "estimated_saving": selected_item["estimated_saving"],
        "net_saving": selected_item["net_saving"],
        "switch_cost": switch,
        "budget": budget,
        "remaining_batches": remaining_batches,
        "rejected_candidates": rejected,
        "precision_sensitive": precision_sensitive,
        "precision_recheck": precision_recheck,
        "drift": {
            "join_id": drift["primary"]["join_id"],
            "source_table": drift["primary"]["source_table"],
            "source_key": drift["primary"]["source_key"],
            "deviation_ratio": drift["primary"]["deviation_ratio"],
            "batch_interval": drift["primary"]["batch_interval"],
            "threshold": drift["primary"]["threshold"],
            "threshold_margin": drift["primary"]["threshold_margin"],
            "sample_count": drift["primary"]["sample_count"],
        },
    }

