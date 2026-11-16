"""Fixed-point cost parameters and deterministic plan enumeration."""

from __future__ import annotations

import hashlib
import itertools
import math
from dataclasses import dataclass, replace
from typing import Any, Iterable

from .catalog import Catalog, ColumnDef, TableDef
from .errors import QueryError, SchemaError
from .query import ColumnRef, JoinEdge, Literal, Predicate, Query


FIXED_SCALE = 1000
OPERATORS = ("hash", "nested_loop")
DECISION_RULE = (
    "Sort candidates by (quantized_cost ascending, plan_signature ascending). "
    "Costs are fixed-point micro-units and quantized downward onto cost_quantum; "
    "differences inside one quantum are ties. Plan signature is the explicit "
    "tie-breaker, never dictionary iteration order."
)


@dataclass(frozen=True)
class CostParams:
    scan_row_cost: float = 1.0
    filter_row_cost: float = 0.25
    hash_build_row_cost: float = 1.8
    hash_probe_row_cost: float = 0.8
    nested_loop_pair_cost: float = 0.35
    output_row_cost: float = 0.2
    skew_factor: float = 1.35
    cost_quantum: float = 0.01
    join_order_bias: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        numeric_names = (
            "scan_row_cost",
            "filter_row_cost",
            "hash_build_row_cost",
            "hash_probe_row_cost",
            "nested_loop_pair_cost",
            "output_row_cost",
            "cost_quantum",
        )
        for name in numeric_names:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise SchemaError(f"{name} must be a finite number", f"cost_params.{name}")
            if float(value) < 0:
                raise SchemaError(f"{name} must be non-negative", f"cost_params.{name}")
        if float(self.cost_quantum) <= 0:
            raise SchemaError("cost_quantum must be greater than zero", "cost_params.cost_quantum")
        if isinstance(self.skew_factor, bool) or not isinstance(self.skew_factor, (int, float)):
            raise SchemaError("skew_factor must be a number", "cost_params.skew_factor")
        if not math.isfinite(float(self.skew_factor)) or float(self.skew_factor) < 1:
            raise SchemaError("skew_factor must be finite and at least 1", "cost_params.skew_factor")
        seen: set[str] = set()
        for table, value in self.join_order_bias:
            key = str(table).lower()
            if key in seen:
                raise SchemaError(f"duplicate join_order_bias entry for {key!r}", "cost_params.join_order_bias")
            seen.add(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise SchemaError(
                    f"join_order_bias for {key!r} must be finite",
                    f"cost_params.join_order_bias.{key}",
                )

    @property
    def bias_map(self) -> dict[str, float]:
        return {key: float(value) for key, value in self.join_order_bias}

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_row_cost": float(self.scan_row_cost),
            "filter_row_cost": float(self.filter_row_cost),
            "hash_build_row_cost": float(self.hash_build_row_cost),
            "hash_probe_row_cost": float(self.hash_probe_row_cost),
            "nested_loop_pair_cost": float(self.nested_loop_pair_cost),
            "output_row_cost": float(self.output_row_cost),
            "skew_factor": float(self.skew_factor),
            "cost_quantum": float(self.cost_quantum),
            "join_order_bias": {key: float(value) for key, value in sorted(self.join_order_bias)},
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "CostParams":
        if not isinstance(value, dict):
            raise SchemaError("cost parameters must be an object", "cost_params")
        allowed = {
            "scan_row_cost",
            "filter_row_cost",
            "hash_build_row_cost",
            "hash_probe_row_cost",
            "nested_loop_pair_cost",
            "output_row_cost",
            "skew_factor",
            "cost_quantum",
            "join_order_bias",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise SchemaError(f"unknown cost parameter {unknown[0]!r}", f"cost_params.{unknown[0]}")
        bias_raw = value.get("join_order_bias", {})
        if not isinstance(bias_raw, dict):
            raise SchemaError("join_order_bias must be an object", "cost_params.join_order_bias")
        bias: list[tuple[str, float]] = []
        for table, raw in bias_raw.items():
            if not isinstance(table, str) or not table.strip():
                raise SchemaError("join_order_bias keys must be table names", "cost_params.join_order_bias")
            bias.append((table.lower(), float(raw)))
        kwargs = {name: float(value[name]) for name in allowed - {"join_order_bias"} if name in value}
        return cls(**kwargs, join_order_bias=tuple(sorted(bias)))

    def with_updates(self, patch: dict[str, Any]) -> "CostParams":
        current = self.to_dict()
        for key, value in patch.items():
            if key not in current:
                raise SchemaError(f"unknown cost parameter {key!r}", f"cost_params.{key}")
            current[key] = value
        return CostParams.from_mapping(current)

    def to_micro(self, value: float) -> int:
        return int(round(float(value) * FIXED_SCALE))

    @property
    def quantum_micro(self) -> int:
        return max(1, self.to_micro(self.cost_quantum))


@dataclass(frozen=True)
class ScanEstimate:
    table: str
    input_rows: int
    output_rows: int
    filter_count: int
    cost_micro: int

    def to_dict(self, params: CostParams) -> dict[str, Any]:
        return {
            "table": self.table,
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "filter_count": self.filter_count,
            "cost": round(self.cost_micro / FIXED_SCALE, 6),
            "cost_micro": self.cost_micro,
        }


@dataclass(frozen=True)
class PlanStep:
    index: int
    right_table: str
    operator: str
    join_kind: str
    predicates: tuple[Predicate, ...]
    input_left_rows: int
    input_right_rows: int
    output_rows: int
    raw_join_cost_micro: int
    bias_cost_micro: int
    output_cost_micro: int
    cumulative_cost_micro: int
    equality_keys: int

    def to_dict(self, params: CostParams) -> dict[str, Any]:
        return {
            "index": self.index,
            "right_table": self.right_table,
            "operator": self.operator,
            "join_kind": self.join_kind,
            "join_predicates": [predicate.render() for predicate in self.predicates],
            "input_left_rows_est": self.input_left_rows,
            "input_right_rows_est": self.input_right_rows,
            "output_rows_est": self.output_rows,
            "equality_keys": self.equality_keys,
            "raw_join_cost": round(self.raw_join_cost_micro / FIXED_SCALE, 6),
            "join_order_bias_cost": round(self.bias_cost_micro / FIXED_SCALE, 6),
            "output_cost": round(self.output_cost_micro / FIXED_SCALE, 6),
            "cumulative_cost": round(self.cumulative_cost_micro / FIXED_SCALE, 6),
            "cumulative_cost_micro": self.cumulative_cost_micro,
        }


@dataclass(frozen=True)
class Plan:
    query_id: str
    relation_order: tuple[str, ...]
    operators: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    scans: tuple[ScanEstimate, ...]
    post_filter_selectivity: float
    post_filter_output_rows: int
    post_filter_cost_micro: int
    total_cost_micro: int
    quantized_cost_micro: int
    plan_signature: str
    plan_id: str

    def to_dict(self, params: CostParams, *, selected: bool = False) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_signature": self.plan_signature,
            "selected": selected,
            "relation_order": list(self.relation_order),
            "operators": list(self.operators),
            "scans": [scan.to_dict(params) for scan in self.scans],
            "steps": [step.to_dict(params) for step in self.steps],
            "post_filter_selectivity": round(self.post_filter_selectivity, 9),
            "post_filter_cost": round(self.post_filter_cost_micro / FIXED_SCALE, 6),
            "final_rows_est": self.post_filter_output_rows,
            "total_cost": round(self.total_cost_micro / FIXED_SCALE, 6),
            "total_cost_micro": self.total_cost_micro,
            "quantized_cost": round(self.quantized_cost_micro / FIXED_SCALE, 6),
            "quantized_cost_micro": self.quantized_cost_micro,
        }


@dataclass(frozen=True)
class PlanSet:
    query: Query
    params: CostParams
    sample_scale: float
    candidates: tuple[Plan, ...]
    selected: Plan | None
    decision_rule: str = DECISION_RULE

    def fallback_plan(self) -> Plan:
        for plan in self.candidates:
            if all(operator == "nested_loop" for operator in plan.operators) and plan.relation_order == self.query.tables:
                return plan
        raise QueryError("fallback all-nested-loop plan is unavailable", "plans.fallback")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query.query_id,
            "canonical_sql": self.query.canonical_sql,
            "sample_scale": self.sample_scale,
            "cost_params": self.params.to_dict(),
            "decision_rule": self.decision_rule,
            "selected_plan_id": self.selected.plan_id if self.selected else None,
            "candidates": [
                plan.to_dict(self.params, selected=self.selected is not None and plan.plan_id == self.selected.plan_id)
                for plan in self.candidates
            ],
        }


def _round_rows(value: float) -> int:
    if value <= 0:
        return 0
    return max(1, int(round(value)))


def _ndv(catalog: Catalog, ref: ColumnRef) -> int:
    table = catalog.table(ref.table)
    if table is None:
        return 1
    column = table.column(ref.column)
    if column is None:
        return 1
    return max(1, column.ndv if column.ndv is not None else 1)


def _predicate_selectivity(predicate: Predicate, catalog: Catalog) -> float:
    left_ndv = _ndv(catalog, predicate.left)
    if isinstance(predicate.right, ColumnRef):
        right_ndv = _ndv(catalog, predicate.right)
        if predicate.op == "=":
            return min(1.0, 1.0 / max(left_ndv, right_ndv))
        if predicate.op in {"<>", "!="}:
            return 0.9
        return 0.3
    if predicate.op == "=":
        return max(1.0 / max(left_ndv, 1), 1e-9)
    if predicate.op in {"<>", "!="}:
        return 0.9
    return 0.3


def _filters_selectivity(predicates: Iterable[Predicate], catalog: Catalog) -> float:
    result = 1.0
    for predicate in predicates:
        result *= max(1e-9, min(1.0, _predicate_selectivity(predicate, catalog)))
    return result


def _is_equality_pair(predicate: Predicate) -> bool:
    return isinstance(predicate.right, ColumnRef) and predicate.op == "="


def _skew_multiplier(
    predicates: Iterable[Predicate],
    new_table: str,
    current_tables: set[str],
    catalog: Catalog,
    params: CostParams,
) -> float:
    touched = set()
    for predicate in predicates:
        if _is_equality_pair(predicate):
            touched.add(predicate.left.table)
            if isinstance(predicate.right, ColumnRef):
                touched.add(predicate.right.table)
    for table_key in touched | {new_table}:
        table = catalog.table(table_key)
        if table is None or not table.skew or table.skew_column is None:
            continue
        for predicate in predicates:
            refs = [predicate.left]
            if isinstance(predicate.right, ColumnRef):
                refs.append(predicate.right)
            if any(ref.table == table.key and ref.column == table.skew_column for ref in refs):
                return float(params.skew_factor)
    return 1.0


def _applicable_inner_predicates(
    query: Query,
    context: set[str],
    applied: set[int],
) -> list[tuple[int, Predicate]]:
    result = []
    for index, predicate in enumerate(query.join_predicates):
        if index in applied:
            continue
        if predicate.tables <= context:
            result.append((index, predicate))
    return result


def _hash_equality_count(predicates: Iterable[Predicate], current_tables: set[str], new_table: str) -> int:
    count = 0
    for predicate in predicates:
        if not _is_equality_pair(predicate):
            continue
        left_side = predicate.left.table
        right = predicate.right
        assert isinstance(right, ColumnRef)
        right_side = right.table
        if {left_side, right_side} <= current_tables | {new_table} and (
            left_side == new_table or right_side == new_table
        ) and (left_side in current_tables or right_side in current_tables):
            count += 1
    return count


def _estimate_order(
    query: Query,
    catalog: Catalog,
    params: CostParams,
    sample_scale: float,
    order: tuple[str, ...],
    operators: tuple[str, ...],
) -> Plan:
    table_map = catalog.table_by_key
    scans: list[ScanEstimate] = []
    scan_cost_by_table: dict[str, int] = {}
    scan_rows_by_table: dict[str, int] = {}
    scan_outputs: dict[str, int] = {}
    total_cost = 0
    for table_key in order:
        table = table_map[table_key]
        raw_rows = int(round(table.row_count * sample_scale))
        if table.row_count and raw_rows == 0:
            raw_rows = 1
        local_predicates = query.filters_for(table_key)
        selectivity = _filters_selectivity(local_predicates, catalog)
        output_rows = _round_rows(raw_rows * selectivity) if raw_rows else 0
        filter_comparisons = raw_rows * len(local_predicates)
        cost_micro = params.to_micro(
            raw_rows * float(params.scan_row_cost)
            + filter_comparisons * float(params.filter_row_cost)
        )
        scan = ScanEstimate(table_key, raw_rows, output_rows, len(local_predicates), cost_micro)
        scans.append(scan)
        scan_cost_by_table[table_key] = cost_micro
        scan_rows_by_table[table_key] = raw_rows
        scan_outputs[table_key] = output_rows
        total_cost += cost_micro

    current_tables: set[str] = {order[0]}
    current_rows = float(scan_outputs[order[0]])
    steps: list[PlanStep] = []
    applied: set[int] = set()

    if query.has_outer:
        edge_by_right = {edge.right_table: edge for edge in query.edges}
    else:
        edge_by_right = {}

    for index, right_table in enumerate(order[1:]):
        context = current_tables | {right_table}
        join_kind = "inner"
        if query.has_outer:
            edge = edge_by_right.get(right_table)
            if edge is None:
                raise QueryError("missing outer-join edge for plan", "plans.outer")
            join_kind = edge.kind
            predicates = list(edge.predicates)
            for predicate_index, predicate in enumerate(query.join_predicates):
                if predicate in edge.predicates:
                    applied.add(predicate_index)
        else:
            selected = _applicable_inner_predicates(query, context, applied)
            for predicate_index, _ in selected:
                applied.add(predicate_index)
            predicates = [predicate for _, predicate in selected]

        right_rows = float(scan_outputs[right_table])
        selectivity = _filters_selectivity(predicates, catalog)
        if any(isinstance(predicate.right, ColumnRef) for predicate in predicates):
            inner_rows = current_rows * right_rows * selectivity
        else:
            # ON predicates containing only a literal do not suppress a cross
            # product's cardinality in this deliberately simple model.
            inner_rows = current_rows * right_rows * selectivity
        skew = _skew_multiplier(predicates, right_table, current_tables, catalog, params)
        inner_rows *= skew
        if query.has_outer:
            output_rows_f = max(current_rows, inner_rows)
        else:
            output_rows_f = inner_rows
        output_rows = _round_rows(output_rows_f)

        equality_keys = _hash_equality_count(predicates, current_tables, right_table)
        operator = operators[index]
        if operator == "hash":
            # The executor builds the hash table on the newly joined (right)
            # side and probes it with the accumulated left side.
            raw_cost = (
                right_rows * float(params.hash_build_row_cost)
                + current_rows * float(params.hash_probe_row_cost)
                + output_rows_f * float(params.output_row_cost)
            )
        else:
            raw_cost = (
                current_rows * right_rows * float(params.nested_loop_pair_cost)
                + output_rows_f * float(params.output_row_cost)
            )
        raw_cost_micro = params.to_micro(raw_cost)
        bias_value = params.bias_map.get(right_table, 0.0)
        # A stable per-position bias lets users force plans without making the
        # result depend on set/dict traversal order.
        bias_micro = params.to_micro(bias_value * (index + 1))
        output_cost_micro = params.to_micro(output_rows_f * float(params.output_row_cost))
        cumulative = max(0, total_cost + raw_cost_micro + bias_micro)
        total_cost = cumulative
        step = PlanStep(
            index=index,
            right_table=right_table,
            operator=operator,
            join_kind=join_kind,
            predicates=tuple(sorted(predicates, key=_predicate_sort_key)),
            input_left_rows=_round_rows(current_rows),
            input_right_rows=_round_rows(right_rows),
            output_rows=output_rows,
            raw_join_cost_micro=raw_cost_micro,
            bias_cost_micro=bias_micro,
            output_cost_micro=output_cost_micro,
            cumulative_cost_micro=cumulative,
            equality_keys=equality_keys,
        )
        steps.append(step)
        current_tables = context
        current_rows = float(output_rows)

    post_selectivity = _filters_selectivity(query.post_filters, catalog)
    final_rows = current_rows * post_selectivity
    post_filter_output_rows = _round_rows(final_rows) if final_rows > 0 else 0
    post_cost_micro = params.to_micro(
        current_rows * len(query.post_filters) * float(params.filter_row_cost)
        + max(0.0, current_rows - final_rows) * float(params.output_row_cost)
    )
    total_cost = max(0, total_cost + post_cost_micro)
    quantum = params.quantum_micro
    quantized = (total_cost // quantum) * quantum

    operators_tuple = tuple(operators)
    signature = (
        "order="
        + ">".join(order)
        + "|ops="
        + ",".join(operators_tuple)
    )
    plan_id = "p_" + hashlib.sha256((query.query_id + "|" + signature).encode("utf-8")).hexdigest()[:24]
    return Plan(
        query_id=query.query_id,
        relation_order=order,
        operators=operators_tuple,
        steps=tuple(steps),
        scans=tuple(scans),
        post_filter_selectivity=post_selectivity,
        post_filter_output_rows=post_filter_output_rows,
        post_filter_cost_micro=post_cost_micro,
        total_cost_micro=total_cost,
        quantized_cost_micro=quantized,
        plan_signature=signature,
        plan_id=plan_id,
    )


def _predicate_sort_key(predicate: Predicate) -> tuple[str, str, str]:
    if isinstance(predicate.right, ColumnRef):
        right = predicate.right.render()
    else:
        right = render_literal_for_sort(predicate.right)
    return predicate.left.render(), predicate.op, right


def render_literal_for_sort(literal: Literal) -> str:
    from .query import render_literal

    return render_literal(literal)


def enumerate_plans(
    query: Query,
    catalog: Catalog,
    params: CostParams,
    sample_scale: float = 1.0,
) -> PlanSet:
    if not query.table_defs:
        raise QueryError("query has no tables", "plans")
    if query.has_outer:
        orders = [query.tables]
    else:
        orders = [tuple(order) for order in itertools.permutations(query.tables)]

    candidates: list[Plan] = []
    for order in orders:
        # Determine which operator combinations are legal for this order.
        legal_ops: list[tuple[str, ...]] = []
        for operators in itertools.product(OPERATORS, repeat=max(0, len(order) - 1)):
            current = {order[0]}
            applied: set[int] = set()
            legal = True
            for index, right_table in enumerate(order[1:]):
                context = current | {right_table}
                if query.has_outer:
                    edge = next((item for item in query.edges if item.right_table == right_table), None)
                    predicates = list(edge.predicates) if edge else []
                else:
                    selected = _applicable_inner_predicates(query, context, applied)
                    for predicate_index, _ in selected:
                        applied.add(predicate_index)
                    predicates = [predicate for _, predicate in selected]
                if operators[index] == "hash" and _hash_equality_count(predicates, current, right_table) == 0:
                    legal = False
                    break
                current = context
            if legal:
                legal_ops.append(tuple(operators))
        for operators in legal_ops:
            candidates.append(_estimate_order(query, catalog, params, sample_scale, order, operators))

    if not candidates:
        raise QueryError("no legal candidate plan", "plans")
    candidates.sort(key=lambda plan: (plan.quantized_cost_micro, plan.plan_signature, plan.total_cost_micro))
    unique: list[Plan] = []
    seen: set[str] = set()
    for plan in candidates:
        if plan.plan_id not in seen:
            seen.add(plan.plan_id)
            unique.append(plan)
    selected = unique[0] if unique else None
    return PlanSet(
        query=query,
        params=params,
        sample_scale=sample_scale,
        candidates=tuple(unique),
        selected=selected,
    )


def fixed_tie_demo(plans: Iterable[Plan], params: CostParams) -> list[str]:
    """Helper used by tests/reporting to make tie handling inspectable."""

    ranked = sorted(plans, key=lambda plan: (plan.quantized_cost_micro, plan.plan_signature))
    return [plan.plan_id for plan in ranked if ranked and plan.quantized_cost_micro == ranked[0].quantized_cost_micro]
