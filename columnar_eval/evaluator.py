from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .model import (
    BOOL,
    BatchBudget,
    BudgetRejected,
    ColumnVector,
    DType,
    EvalBatch,
    EvaluationError,
)
from .normalize import NormalizedModel
from .typesys import (
    ARITHMETIC_OPS,
    COMPARISON_OPS,
    LOGICAL_OPS,
    PromotionRecord,
    cast_vector,
    common_type,
    plan_promotion,
)


@dataclass
class NodeResult:
    expression_id: str
    node_id: str
    op: str
    vector: ColumnVector
    row_scope: str
    row_indices: list[int] | None
    selection_context: dict[str, Any] | None

    def to_json(self) -> dict[str, Any]:
        return {
            "expression_id": self.expression_id,
            "node_id": self.node_id,
            "op": self.op,
            "row_scope": self.row_scope,
            "row_indices": self.row_indices,
            "selection_context": self.selection_context,
            "result": self.vector.to_json(),
        }


@dataclass
class BatchEvaluation:
    batch_index: int
    row_start: int
    row_count: int
    input_columns: int
    working_columns_est: int
    expression_results: dict[str, ColumnVector]
    node_results: dict[str, dict[str, NodeResult]]
    ledger: list[dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {
            "batch_index": self.batch_index,
            "row_start": self.row_start,
            "row_count": self.row_count,
            "input_columns": self.input_columns,
            "working_columns_est": self.working_columns_est,
            "expressions": [
                {
                    "expression_id": expression_id,
                    "result": vector.to_json(),
                }
                for expression_id, vector in self.expression_results.items()
            ],
            "nodes": copy.deepcopy(self.ledger),
            "node_results": [
                node.to_json()
                for expression_nodes in self.node_results.values()
                for node in expression_nodes.values()
            ],
        }


@dataclass
class EvaluationRun:
    version: str
    row_count: int
    batch_rows: int
    budget: BatchBudget
    expressions: list[dict[str, Any]]
    batches: list[BatchEvaluation]
    expression_results: dict[str, ColumnVector]
    expression_purposes: dict[str, str]

    def all_node_results(self) -> list[NodeResult]:
        return [
            node
            for batch in self.batches
            for expression_nodes in batch.node_results.values()
            for node in expression_nodes.values()
        ]

    def aggregate_node_ledger(self) -> dict[str, dict[str, Any]]:
        aggregate: dict[str, dict[str, Any]] = {}
        for batch in self.batches:
            for entry in batch.ledger:
                expression_id = entry["expression_id"]
                node_id = entry["node_id"]
                key_map = aggregate.setdefault(expression_id, {})
                target = key_map.setdefault(
                    node_id,
                    {
                        "expression_id": expression_id,
                        "node_id": node_id,
                        "op": entry["op"],
                        "output_type": entry["output_type"],
                        "evaluated_rows": 0,
                        "null_input_count": 0,
                        "null_output_count": 0,
                        "batches": [],
                        "promotion_records": [],
                    },
                )
                target["evaluated_rows"] += entry["evaluated_rows"]
                target["null_input_count"] += entry["null_input_count"]
                target["null_output_count"] += entry["null_output_count"]
                target["batches"].append(
                    {
                        "batch_index": entry["batch_index"],
                        "evaluated_rows": entry["evaluated_rows"],
                        "null_input_count": entry["null_input_count"],
                        "null_output_count": entry["null_output_count"],
                        "row_scope": entry["row_scope"],
                    }
                )
                if "conditional" in entry:
                    conditional = entry["conditional"]
                    totals = target.setdefault(
                        "conditional_selection_totals",
                        {
                            "condition_true_rows": 0,
                            "condition_false_rows": 0,
                            "condition_null_rows": 0,
                            "true_branch_selected_rows": 0,
                            "false_branch_selected_rows": 0,
                            "true_branch_result_nulls": 0,
                            "false_branch_result_nulls": 0,
                            "merged_result_nulls": 0,
                        },
                    )
                    for key in totals:
                        totals[key] += int(conditional[key])
                    target.setdefault("conditional_batches", []).append(
                        {
                            "batch_index": entry["batch_index"],
                            **{
                                key: conditional[key] for key in totals
                            },
                            "unselected_branch_nulls_isolated": conditional[
                                "unselected_branch_nulls_isolated"
                            ],
                        }
                    )
                promotion = entry.get("promotion")
                if promotion and promotion not in target["promotion_records"]:
                    target["promotion_records"].append(promotion)
        return aggregate

    def expression_summaries(self) -> list[dict[str, Any]]:
        aggregate = self.aggregate_node_ledger()
        summaries: list[dict[str, Any]] = []
        for expression in self.expressions:
            expression_id = expression["expression_id"]
            result = self.expression_results[expression_id]
            summaries.append(
                {
                    "expression_id": expression_id,
                    "purpose": self.expression_purposes.get(expression_id, ""),
                    "result_type": result.dtype.label,
                    "result_rows": len(result),
                    "result_null_count": result.null_count,
                    "batch_row_counts": [batch.row_count for batch in self.batches],
                    "batch_count": len(self.batches),
                    "node_count": len(aggregate.get(expression_id, {})),
                    "nodes": copy.deepcopy(aggregate.get(expression_id, {})),
                }
            )
        return summaries

    def to_json(self, *, include_node_results: bool = True) -> dict[str, Any]:
        batches_json: list[dict[str, Any]] = []
        for batch in self.batches:
            payload = batch.to_json()
            if not include_node_results:
                payload.pop("node_results", None)
            batches_json.append(payload)
        return {
            "version": self.version,
            "row_count": self.row_count,
            "execution_mode": "column_vector_batch",
            "batch_rows": self.batch_rows,
            "batch_budget": self.budget.to_json(),
            "batch_plan": [
                {
                    "batch_index": batch.batch_index,
                    "row_start": batch.row_start,
                    "row_count": batch.row_count,
                }
                for batch in self.batches
            ],
            "expressions": self.expression_summaries(),
            "expression_results": {
                expression_id: vector.to_json()
                for expression_id, vector in self.expression_results.items()
            },
            "batches": batches_json,
            "budget_audit": {
                "all_batches_within_row_limit": all(
                    batch.row_count <= self.budget.max_rows for batch in self.batches
                ),
                "all_batches_within_input_width": all(
                    batch.input_columns <= self.budget.max_input_columns
                    for batch in self.batches
                ),
                "all_batches_within_working_width": all(
                    batch.working_columns_est <= self.budget.max_working_columns
                    for batch in self.batches
                ),
                "all_batches_within_cell_limit": (
                    True
                    if self.budget.max_cells is None
                    else all(
                        batch.row_count * batch.working_columns_est
                        <= self.budget.max_cells
                        for batch in self.batches
                    )
                ),
                "truncated_rows": 0,
                "rejection_policy": self.budget.rejection_policy,
            },
        }


def _tri_and(left: bool | None, right: bool | None) -> bool | None:
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def _tri_or(left: bool | None, right: bool | None) -> bool | None:
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


def _tri_not(value: bool | None) -> bool | None:
    if value is None:
        return None
    return not value


def _truncating_quotient(numerator: int, denominator: int) -> int:
    if denominator == 0:
        raise ZeroDivisionError
    negative = (numerator < 0) != (denominator < 0)
    quotient = abs(numerator) // abs(denominator)
    return -quotient if negative else quotient


_COMPARE_FUNCTIONS = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "lt": lambda left, right: left < right,
    "le": lambda left, right: left <= right,
    "gt": lambda left, right: left > right,
    "ge": lambda left, right: left >= right,
}

_LOGIC_FUNCTIONS = {"and": _tri_and, "or": _tri_or}


def _count_nodes(node: dict[str, Any]) -> int:
    return 1 + sum(_count_nodes(arg) for arg in node.get("args", []))


def _concat_vectors(vectors: list[ColumnVector]) -> ColumnVector:
    if not vectors:
        raise EvaluationError("cannot concatenate zero vectors")
    dtype = vectors[0].dtype
    if any(vector.dtype != dtype for vector in vectors):
        raise EvaluationError("batch result types differ across batches")
    values: list[Any] = []
    mask: list[bool] = []
    for vector in vectors:
        values.extend(vector.values)
        mask.extend(vector.null_mask)
    return ColumnVector(dtype=dtype, values=values, null_mask=mask)


class _ExpressionBatchEvaluator:
    def __init__(
        self,
        *,
        expression: dict[str, Any],
        batch: EvalBatch,
    ) -> None:
        self.expression = expression
        self.expression_id = expression["expression_id"]
        self.batch = batch
        self.ledger: list[dict[str, Any]] = []
        self.node_results: dict[str, NodeResult] = {}
        self.full_indices = list(range(batch.row_count))

    def evaluate(self) -> ColumnVector:
        result = self._eval_node(
            self.expression["root"],
            indices=self.full_indices,
            selection_context=None,
        )
        return result

    def _record(
        self,
        *,
        node: dict[str, Any],
        result: ColumnVector,
        operand_vectors: list[ColumnVector],
        promotion: PromotionRecord | dict[str, Any] | None,
        indices: list[int],
        selection_context: dict[str, Any] | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        node_id = node["node_id"]
        if node_id in self.node_results:
            raise EvaluationError(
                f"duplicate node id in expression {self.expression_id}: {node_id}"
            )
        null_inputs_by_operand = [
            vector.null_count for vector in operand_vectors
        ]
        operand_node_ids = [
            arg["node_id"] for arg in node.get("args", [])
        ] or [node_id]
        if selection_context is None and len(indices) == self.batch.row_count:
            row_scope = "full_batch"
            ledger_indices = None
        else:
            row_scope = "selected_rows"
            ledger_indices = list(indices)
        promotion_json = (
            promotion.to_json()
            if isinstance(promotion, PromotionRecord)
            else copy.deepcopy(promotion)
        )
        entry: dict[str, Any] = {
            "expression_id": self.expression_id,
            "batch_index": self.batch.batch_index,
            "node_id": node_id,
            "op": node["op"],
            "evaluation_mode": "whole_column_vector",
            "row_scope": row_scope,
            "operand_node_ids": operand_node_ids,
            "evaluated_rows": len(result),
            "row_start": self.batch.row_start,
            "row_indices": ledger_indices,
            "selection_context": copy.deepcopy(selection_context),
            "null_input_count": sum(null_inputs_by_operand),
            "null_inputs_by_operand": null_inputs_by_operand,
            "null_output_count": result.null_count,
            "output_type": result.dtype.label,
            "promotion": promotion_json,
            "result": result.to_json(),
        }
        if extra:
            entry.update(copy.deepcopy(extra))
        self.ledger.append(entry)
        self.node_results[node_id] = NodeResult(
            expression_id=self.expression_id,
            node_id=node_id,
            op=node["op"],
            vector=result,
            row_scope=row_scope,
            row_indices=ledger_indices,
            selection_context=copy.deepcopy(selection_context),
        )

    def _eval_node(
        self,
        node: dict[str, Any],
        *,
        indices: list[int],
        selection_context: dict[str, Any] | None,
    ) -> ColumnVector:
        op = node["op"]
        if op == "column":
            field = node["field"]
            if field not in self.batch.columns:
                raise EvaluationError(f"unknown normalized column: {field}")
            source = self.batch.columns[field]
            vector = source.subset(indices)
            self._record(
                node=node,
                result=vector,
                operand_vectors=[vector],
                promotion=None,
                indices=indices,
                selection_context=selection_context,
                extra={"source_kind": "column", "column_input_nulls": vector.null_count},
            )
            return vector

        if op == "literal":
            dtype = DType.from_json(node["dtype"])
            row_count = len(indices)
            if dtype.kind == "decimal":
                value = int(node["raw"])
            elif dtype.kind in {"int64", "bool"}:
                value = node.get("value", node.get("raw"))
            elif dtype.kind == "string":
                value = str(node.get("value", ""))
            elif dtype.kind == "null":
                value = None
            else:
                raise EvaluationError(f"unsupported literal type: {dtype.label}")
            if dtype.kind == "null":
                vector = ColumnVector(
                    dtype=dtype,
                    values=[None] * row_count,
                    null_mask=[True] * row_count,
                )
            else:
                vector = ColumnVector(
                    dtype=dtype,
                    values=[value] * row_count,
                    null_mask=[False] * row_count,
                )
            self._record(
                node=node,
                result=vector,
                operand_vectors=[vector],
                promotion=None,
                indices=indices,
                selection_context=selection_context,
                extra={"source_kind": "literal"},
            )
            return vector

        if op == "conditional":
            return self._eval_conditional(
                node, indices=indices, selection_context=selection_context
            )

        args = node.get("args", [])
        operand_vectors = [
            self._eval_node(arg, indices=indices, selection_context=selection_context)
            for arg in args
        ]
        operand_types = [vector.dtype for vector in operand_vectors]
        promotion = plan_promotion(op, operand_types)
        extra: dict[str, Any] = {}

        if op in ARITHMETIC_OPS:
            cast_args = [
                cast_vector(vector, target)
                for vector, target in zip(operand_vectors, promotion.operand_targets)
            ]
            mask = [
                left or right
                for left, right in zip(cast_args[0].null_mask, cast_args[1].null_mask)
            ]
            if op == "add":
                values = [
                    None if is_null else int(left) + int(right)
                    for left, right, is_null in zip(
                        cast_args[0].values, cast_args[1].values, mask
                    )
                ]
            elif op == "sub":
                values = [
                    None if is_null else int(left) - int(right)
                    for left, right, is_null in zip(
                        cast_args[0].values, cast_args[1].values, mask
                    )
                ]
            elif op == "mul":
                values = [
                    None if is_null else int(left) * int(right)
                    for left, right, is_null in zip(
                        cast_args[0].values, cast_args[1].values, mask
                    )
                ]
            elif op == "div":
                scale = int(promotion.result_type.scale or 0)
                values = []
                for left, right, is_null in zip(
                    cast_args[0].values, cast_args[1].values, mask
                ):
                    if is_null:
                        values.append(None)
                    elif int(right) == 0:
                        raise EvaluationError(
                            f"{self.expression_id}/{node['node_id']}: division by zero"
                        )
                    else:
                        values.append(
                            _truncating_quotient(
                                int(left) * 10**scale, int(right)
                            )
                        )
            else:
                raise EvaluationError(f"unsupported arithmetic op: {op}")
            result = ColumnVector(
                dtype=promotion.result_type,
                values=values,
                null_mask=mask,
            )
        elif op in COMPARISON_OPS:
            cast_args = [
                cast_vector(vector, target)
                for vector, target in zip(operand_vectors, promotion.operand_targets)
            ]
            mask = [
                left or right
                for left, right in zip(cast_args[0].null_mask, cast_args[1].null_mask)
            ]
            compare = _COMPARE_FUNCTIONS[op]
            values = [
                None if is_null else compare(left, right)
                for left, right, is_null in zip(
                    cast_args[0].values, cast_args[1].values, mask
                )
            ]
            result = ColumnVector(dtype=BOOL, values=values, null_mask=mask)
        elif op in LOGICAL_OPS:
            cast_args = [
                cast_vector(vector, BOOL) for vector in operand_vectors
            ]
            logic = _LOGIC_FUNCTIONS[op]
            values: list[bool | None] = []
            for left, right in zip(cast_args[0].values, cast_args[1].values):
                values.append(logic(left, right))
            mask = [value is None for value in values]
            result = ColumnVector(dtype=BOOL, values=values, null_mask=mask)
            extra["truth_table"] = (
                "AND: false dominates null; OR: true dominates null; "
                "otherwise null propagates"
            )
        elif op == "not":
            cast_arg = cast_vector(operand_vectors[0], BOOL)
            values = [_tri_not(value) for value in cast_arg.values]
            mask = [value is None for value in values]
            result = ColumnVector(dtype=BOOL, values=values, null_mask=mask)
        elif op == "abs":
            cast_arg = cast_vector(
                operand_vectors[0], promotion.operand_targets[0]
            )
            values = [
                None if is_null else abs(int(value))
                for value, is_null in zip(cast_arg.values, cast_arg.null_mask)
            ]
            result = ColumnVector(
                dtype=promotion.result_type,
                values=values,
                null_mask=list(cast_arg.null_mask),
            )
        elif op == "coalesce":
            cast_args = [
                cast_vector(vector, promotion.result_type)
                for vector in operand_vectors
            ]
            result_values: list[Any] = []
            result_mask: list[bool] = []
            for row_idx in range(len(cast_args[0])):
                chosen: Any = None
                found = False
                for vector in cast_args:
                    if not vector.null_mask[row_idx]:
                        chosen = vector.values[row_idx]
                        found = True
                        break
                result_values.append(chosen if found else None)
                result_mask.append(not found)
            result = ColumnVector(
                dtype=promotion.result_type,
                values=result_values,
                null_mask=result_mask,
            )
        elif op == "lower":
            cast_arg = cast_vector(operand_vectors[0], promotion.operand_targets[0])
            values = [
                None if is_null else value.lower()
                for value, is_null in zip(cast_arg.values, cast_arg.null_mask)
            ]
            result = ColumnVector(
                dtype=promotion.result_type,
                values=values,
                null_mask=list(cast_arg.null_mask),
            )
        elif op == "length":
            cast_arg = cast_vector(operand_vectors[0], promotion.operand_targets[0])
            values = [
                None if is_null else len(value)
                for value, is_null in zip(cast_arg.values, cast_arg.null_mask)
            ]
            result = ColumnVector(
                dtype=promotion.result_type,
                values=values,
                null_mask=list(cast_arg.null_mask),
            )
        elif op == "is_null":
            source = operand_vectors[0]
            result = ColumnVector(
                dtype=BOOL,
                values=list(source.null_mask),
                null_mask=[False] * len(source),
            )
            extra["rule"] = "is_null returns true where input mask is 1"
        else:
            raise EvaluationError(f"unsupported operator: {op}")

        self._record(
            node=node,
            result=result,
            operand_vectors=operand_vectors,
            promotion=promotion,
            indices=indices,
            selection_context=selection_context,
            extra=extra,
        )
        return result

    def _eval_conditional(
        self,
        node: dict[str, Any],
        *,
        indices: list[int],
        selection_context: dict[str, Any] | None,
    ) -> ColumnVector:
        args = node.get("args", [])
        if len(args) != 3:
            raise EvaluationError("conditional requires condition, true branch, false branch")
        condition = self._eval_node(
            args[0], indices=indices, selection_context=selection_context
        )
        if condition.dtype.kind not in {"bool", "null"}:
            raise EvaluationError(
                f"conditional condition must be bool, got {condition.dtype.label}"
            )
        true_indices: list[int] = []
        false_indices: list[int] = []
        null_indices: list[int] = []
        for local_idx, (value, is_null) in enumerate(
            zip(condition.values, condition.null_mask)
        ):
            if is_null:
                null_indices.append(local_idx)
            elif value is True:
                true_indices.append(local_idx)
            elif value is False:
                false_indices.append(local_idx)
            else:
                raise EvaluationError("conditional condition contains non-boolean value")

        true_context = {
            "branch": "true",
            "parent_node_id": node["node_id"],
            "selected_rows": len(true_indices),
        }
        false_context = {
            "branch": "false",
            "parent_node_id": node["node_id"],
            "selected_rows": len(false_indices),
        }
        true_vector = self._eval_node(
            args[1],
            indices=[indices[idx] for idx in true_indices],
            selection_context=true_context,
        )
        false_vector = self._eval_node(
            args[2],
            indices=[indices[idx] for idx in false_indices],
            selection_context=false_context,
        )
        result_type = common_type(
            [true_vector.dtype, false_vector.dtype], context="conditional branches"
        )
        true_aligned = cast_vector(true_vector, result_type)
        false_aligned = cast_vector(false_vector, result_type)
        if len(true_aligned) != len(true_indices) or len(false_aligned) != len(false_indices):
            raise EvaluationError("conditional branch result size mismatch")

        values: list[Any] = [None] * len(indices)
        mask = [False] * len(indices)
        for local_idx in null_indices:
            mask[local_idx] = True
        for local_idx, vector_idx in zip(true_indices, range(len(true_indices))):
            values[local_idx] = true_aligned.values[vector_idx]
            mask[local_idx] = true_aligned.null_mask[vector_idx]
        for local_idx, vector_idx in zip(false_indices, range(len(false_indices))):
            values[local_idx] = false_aligned.values[vector_idx]
            mask[local_idx] = false_aligned.null_mask[vector_idx]
        for local_idx, is_null in enumerate(mask):
            if is_null:
                values[local_idx] = None
            elif values[local_idx] is None:
                raise EvaluationError("conditional merge produced an unflagged null")

        promotion = {
            "operand_types": [condition.dtype.label, true_vector.dtype.label, false_vector.dtype.label],
            "operand_targets": [BOOL.label, result_type.label, result_type.label],
            "result_type": result_type.label,
            "basis": (
                "condition evaluated as a selection vector; true and false branches "
                "are evaluated independently on selected row indices, then unified "
                "to a common branch result type and merged"
            ),
            "conversions": [
                {"from": condition.dtype.label, "to": BOOL.label},
                {"from": true_vector.dtype.label, "to": result_type.label},
                {"from": false_vector.dtype.label, "to": result_type.label},
            ],
        }
        result = ColumnVector(dtype=result_type, values=values, null_mask=mask)
        self._record(
            node=node,
            result=result,
            operand_vectors=[condition, true_vector, false_vector],
            promotion=promotion,
            indices=indices,
            selection_context=selection_context,
            extra={
                "conditional": {
                    "condition_true_rows": len(true_indices),
                    "condition_false_rows": len(false_indices),
                    "condition_null_rows": len(null_indices),
                    "true_branch_selected_rows": len(true_indices),
                    "false_branch_selected_rows": len(false_indices),
                    "true_branch_result_nulls": true_aligned.null_count,
                    "false_branch_result_nulls": false_aligned.null_count,
                    "merged_result_nulls": result.null_count,
                    "unselected_branch_nulls_isolated": True,
                    "true_branch_row_indices": [
                        indices[idx] for idx in true_indices
                    ],
                    "false_branch_row_indices": [
                        indices[idx] for idx in false_indices
                    ],
                    "condition_null_row_indices": [
                        indices[idx] for idx in null_indices
                    ],
                }
            },
        )
        return result


def evaluate_batch(
    model: NormalizedModel,
    batch: EvalBatch,
    *,
    budget: BatchBudget,
) -> BatchEvaluation:
    """Evaluate one already-materialized batch; reject it if any budget is exceeded."""
    batch.validate()
    budget.check_input(
        batch_index=batch.batch_index,
        row_count=batch.row_count,
        input_columns=len(batch.columns),
    )
    batch_expression_results: dict[str, ColumnVector] = {}
    batch_node_results: dict[str, dict[str, NodeResult]] = {}
    batch_ledger: list[dict[str, Any]] = []
    batch_working_columns = 0
    for expression in model.expressions:
        node_count = _count_nodes(expression["root"])
        working_columns = len(batch.columns) + node_count
        batch_working_columns = max(batch_working_columns, working_columns)
        budget.check_working(
            batch_index=batch.batch_index,
            row_count=batch.row_count,
            working_columns=working_columns,
        )
        evaluator = _ExpressionBatchEvaluator(
            expression=expression,
            batch=batch,
        )
        result = evaluator.evaluate()
        batch_expression_results[expression["expression_id"]] = result
        batch_node_results[expression["expression_id"]] = evaluator.node_results
        batch_ledger.extend(evaluator.ledger)
    return BatchEvaluation(
        batch_index=batch.batch_index,
        row_start=batch.row_start,
        row_count=batch.row_count,
        input_columns=len(batch.columns),
        working_columns_est=batch_working_columns,
        expression_results=batch_expression_results,
        node_results=batch_node_results,
        ledger=batch_ledger,
    )


def evaluate_model(
    model: NormalizedModel,
    *,
    batch_rows: int = 32,
    budget: BatchBudget | None = None,
) -> EvaluationRun:
    if batch_rows <= 0:
        raise ValueError("batch_rows must be positive")
    if budget is None:
        budget = BatchBudget(
            max_rows=64,
            max_input_columns=max(12, model.column_width()),
            max_working_columns=max(64, model.column_width() + 32),
        )
    if batch_rows > budget.max_rows:
        raise BudgetRejected(
            stage="batch_plan",
            metric="requested_batch_rows",
            observed=batch_rows,
            limit=budget.max_rows,
            policy=budget.rejection_policy,
        )
    if model.column_width() > budget.max_input_columns:
        raise BudgetRejected(
            stage="batch_plan",
            metric="input_columns",
            observed=model.column_width(),
            limit=budget.max_input_columns,
            policy=budget.rejection_policy,
        )

    batches: list[BatchEvaluation] = []
    expression_batches: dict[str, list[ColumnVector]] = {
        expression["expression_id"]: [] for expression in model.expressions
    }
    expression_purposes = {
        expression["expression_id"]: expression.get("purpose", "")
        for expression in model.expressions
    }
    batch_index = 0
    for row_start in range(0, model.row_count, batch_rows):
        row_count = min(batch_rows, model.row_count - row_start)
        batch = model.batch(
            batch_index=batch_index,
            row_start=row_start,
            row_count=row_count,
        )
        batch_evaluation = evaluate_batch(model, batch, budget=budget)
        for expression_id, result in batch_evaluation.expression_results.items():
            expression_batches[expression_id].append(result)
        batches.append(batch_evaluation)
        batch_index += 1

    full_results = {
        expression_id: _concat_vectors(vectors)
        for expression_id, vectors in expression_batches.items()
    }
    return EvaluationRun(
        version=model.version,
        row_count=model.row_count,
        batch_rows=batch_rows,
        budget=budget,
        expressions=model.expressions,
        batches=batches,
        expression_results=full_results,
        expression_purposes=expression_purposes,
    )
