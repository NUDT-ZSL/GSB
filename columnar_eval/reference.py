from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .model import (
    BOOL,
    INT64,
    NULL,
    BatchBudget,
    BudgetRejected,
    ColumnVector,
    DType,
    EvalBatch,
    EvaluationError,
)
from .normalize import NormalizedModel


@dataclass
class ReferenceNodeResult:
    expression_id: str
    node_id: str
    op: str
    vector: ColumnVector
    row_scope: str
    row_indices: list[int] | None
    selection_context: dict[str, Any] | None


@dataclass
class ReferenceBatch:
    batch_index: int
    row_start: int
    row_count: int
    expression_results: dict[str, ColumnVector]
    node_results: dict[str, dict[str, ReferenceNodeResult]]


@dataclass
class ReferenceRun:
    version: str
    row_count: int
    batch_rows: int
    batches: list[ReferenceBatch]
    expression_results: dict[str, ColumnVector]

    def to_json(self, *, include_node_results: bool = True) -> dict[str, Any]:
        return {
            "version": self.version,
            "row_count": self.row_count,
            "batch_rows": self.batch_rows,
            "batches": [
                {
                    "batch_index": batch.batch_index,
                    "row_start": batch.row_start,
                    "row_count": batch.row_count,
                    "expression_results": {
                        expression_id: vector.to_json()
                        for expression_id, vector in batch.expression_results.items()
                    },
                    "node_results": (
                        [
                            {
                                "expression_id": node.expression_id,
                                "node_id": node.node_id,
                                "op": node.op,
                                "row_scope": node.row_scope,
                                "row_indices": node.row_indices,
                                "selection_context": node.selection_context,
                                "result": node.vector.to_json(),
                            }
                            for expression_nodes in batch.node_results.values()
                            for node in expression_nodes.values()
                        ]
                        if include_node_results
                        else []
                    ),
                }
                for batch in self.batches
            ],
            "expression_results": {
                expression_id: vector.to_json()
                for expression_id, vector in self.expression_results.items()
            },
            "mode": "independent_three_valued_reference",
        }


def _concat(vectors: list[ColumnVector]) -> ColumnVector:
    if not vectors:
        raise EvaluationError("reference has no batch outputs to concatenate")
    dtype = vectors[0].dtype
    if any(vector.dtype != dtype for vector in vectors):
        raise EvaluationError("reference batch output types differ")
    return ColumnVector(
        dtype=dtype,
        values=[value for vector in vectors for value in vector.values],
        null_mask=[flag for vector in vectors for flag in vector.null_mask],
    )


def _tree_node_count(node: dict[str, Any]) -> int:
    return 1 + sum(_tree_node_count(arg) for arg in node.get("args", []))


def _scale(dtype: DType) -> int:
    return int(dtype.scale or 0) if dtype.kind == "decimal" else 0


def _numeric_kind(types: list[DType], *, arithmetic: bool) -> DType:
    present = [dtype for dtype in types if dtype.kind != "null"]
    if not present:
        return NULL
    if any(dtype.kind not in {"int64", "decimal", "bool"} for dtype in present):
        raise EvaluationError(
            f"reference numeric operation got {[dtype.label for dtype in present]}"
        )
    if any(dtype.kind == "decimal" for dtype in present):
        return DType("decimal", max(_scale(dtype) for dtype in present))
    if not arithmetic and all(dtype.kind == "bool" for dtype in present):
        return BOOL
    return INT64


def _common_type(types: list[DType], context: str) -> DType:
    present = [dtype for dtype in types if dtype.kind != "null"]
    if not present:
        return NULL
    kinds = {dtype.kind for dtype in present}
    if len(kinds) == 1:
        if kinds == {"decimal"}:
            return DType("decimal", max(_scale(dtype) for dtype in present))
        if kinds == {"string"}:
            return DType("string")
        if kinds == {"bool"}:
            return BOOL
        if kinds == {"int64"}:
            return INT64
    if kinds <= {"int64", "decimal", "bool"}:
        return _numeric_kind(present, arithmetic=False)
    raise EvaluationError(f"{context}: incompatible reference types {list(kinds)}")


def _cast(vector: ColumnVector, target: DType) -> ColumnVector:
    if vector.dtype == target:
        return ColumnVector(
            dtype=target,
            values=list(vector.values),
            null_mask=list(vector.null_mask),
        )
    source = vector.dtype
    if target.kind == "decimal":
        target_scale = int(target.scale or 0)
        output: list[Any] = []
        for value, is_null in zip(vector.values, vector.null_mask):
            if is_null:
                output.append(None)
            elif source.kind in {"int64", "bool"}:
                output.append(int(value) * 10**target_scale)
            elif source.kind == "decimal":
                source_scale = int(source.scale or 0)
                if target_scale >= source_scale:
                    output.append(int(value) * 10 ** (target_scale - source_scale))
                else:
                    divisor = 10 ** (source_scale - target_scale)
                    if int(value) % divisor:
                        raise EvaluationError("reference exact decimal cast failed")
                    output.append(int(value) // divisor)
            elif source.kind == "null":
                output.append(None)
            else:
                raise EvaluationError(f"reference cannot cast {source.label} to decimal")
        mask = list(vector.null_mask)
    elif target.kind == "int64":
        if source.kind in {"bool", "null"}:
            output = [
                None if is_null else int(value)
                for value, is_null in zip(vector.values, vector.null_mask)
            ]
            mask = list(vector.null_mask)
        elif source.kind == "int64":
            output = list(vector.values)
            mask = list(vector.null_mask)
        else:
            raise EvaluationError(f"reference cannot cast {source.label} to int64")
    elif target.kind == "string":
        if source.kind not in {"string", "null"}:
            raise EvaluationError(f"reference cannot cast {source.label} to string")
        output = list(vector.values)
        mask = list(vector.null_mask)
    elif target.kind == "bool":
        if source.kind not in {"bool", "null"}:
            raise EvaluationError(f"reference cannot cast {source.label} to bool")
        output = list(vector.values)
        mask = list(vector.null_mask)
    elif target.kind == "null":
        output = [None] * len(vector)
        mask = [True] * len(vector)
    else:
        raise EvaluationError(f"reference unsupported target {target.label}")
    return ColumnVector(dtype=target, values=output, null_mask=mask)


def _three_valued_and(left: bool | None, right: bool | None) -> bool | None:
    if left is False:
        return False
    if right is False:
        return False
    if left is None or right is None:
        return None
    return True


def _three_valued_or(left: bool | None, right: bool | None) -> bool | None:
    if left is True:
        return True
    if right is True:
        return True
    if left is None or right is None:
        return None
    return False


def _infer_regular_node(
    op: str, operand_types: list[DType]
) -> tuple[DType, list[DType]]:
    if op in {"add", "sub"}:
        result = _numeric_kind(operand_types, arithmetic=True)
        targets = [
            INT64 if dtype.kind == "null" and result.kind != "decimal" else result
            for dtype in operand_types
        ]
        return result, targets
    if op == "mul":
        present = [dtype for dtype in operand_types if dtype.kind != "null"]
        if any(dtype.kind == "decimal" for dtype in present):
            result = DType("decimal", sum(_scale(dtype) for dtype in present))
        else:
            result = INT64
        targets = [INT64 if dtype.kind in {"null", "bool"} else dtype for dtype in operand_types]
        return result, targets
    if op == "div":
        common = _numeric_kind(operand_types, arithmetic=True)
        result = common if common.kind == "decimal" else DType("decimal", 3)
        return result, [
            INT64 if dtype.kind == "null" else result for dtype in operand_types
        ]
    if op in {"eq", "ne", "lt", "le", "gt", "ge"}:
        present = [dtype for dtype in operand_types if dtype.kind != "null"]
        if not present:
            target = NULL
        elif all(dtype.kind in {"int64", "decimal", "bool"} for dtype in present):
            target = _numeric_kind(operand_types, arithmetic=False)
        elif all(dtype.kind == "string" for dtype in present):
            target = DType("string")
        elif all(dtype.kind == "bool" for dtype in present):
            target = BOOL
        else:
            raise EvaluationError("reference comparison types are incompatible")
        return BOOL, [target for _ in operand_types]
    if op in {"and", "or"}:
        for dtype in operand_types:
            if dtype.kind not in {"bool", "null"}:
                raise EvaluationError("reference logical operand is not bool")
        return BOOL, [BOOL for _ in operand_types]
    if op == "not":
        if len(operand_types) != 1 or operand_types[0].kind not in {"bool", "null"}:
            raise EvaluationError("reference NOT operand is not bool")
        return BOOL, [BOOL]
    if op == "abs":
        if len(operand_types) != 1 or operand_types[0].kind not in {
            "int64",
            "decimal",
            "bool",
        }:
            raise EvaluationError("reference abs operand is not numeric")
        target = INT64 if operand_types[0].kind == "bool" else operand_types[0]
        return target, [target]
    if op == "coalesce":
        result = _common_type(operand_types, context="reference coalesce")
        return result, [result for _ in operand_types]
    if op == "lower":
        if len(operand_types) != 1 or operand_types[0].kind not in {"string", "null"}:
            raise EvaluationError("reference lower operand is not string")
        return DType("string"), [DType("string")]
    if op == "length":
        if len(operand_types) != 1 or operand_types[0].kind not in {"string", "null"}:
            raise EvaluationError("reference length operand is not string")
        return INT64, [DType("string")]
    if op == "is_null":
        if len(operand_types) != 1:
            raise EvaluationError("reference is_null requires one operand")
        return BOOL, operand_types
    raise EvaluationError(f"reference does not implement operator {op}")


def _quotient_toward_zero(numerator: int, denominator: int) -> int:
    if denominator == 0:
        raise EvaluationError("reference division by zero")
    negate = (numerator < 0) != (denominator < 0)
    magnitude = abs(numerator) // abs(denominator)
    return -magnitude if negate else magnitude


class _ReferenceBatchEvaluator:
    def __init__(self, expression: dict[str, Any], batch: EvalBatch) -> None:
        self.expression = expression
        self.expression_id = expression["expression_id"]
        self.batch = batch
        self.node_results: dict[str, ReferenceNodeResult] = {}
        self.full_indices = list(range(batch.row_count))

    def evaluate(self) -> ColumnVector:
        return self._walk(
            self.expression["root"],
            indices=self.full_indices,
            selection_context=None,
        )

    def _store(
        self,
        *,
        node: dict[str, Any],
        vector: ColumnVector,
        indices: list[int],
        selection_context: dict[str, Any] | None,
    ) -> None:
        node_id = node["node_id"]
        if selection_context is None and len(indices) == self.batch.row_count:
            scope = "full_batch"
            stored_indices = None
        else:
            scope = "selected_rows"
            stored_indices = list(indices)
        self.node_results[node_id] = ReferenceNodeResult(
            expression_id=self.expression_id,
            node_id=node_id,
            op=node["op"],
            vector=vector,
            row_scope=scope,
            row_indices=stored_indices,
            selection_context=copy.deepcopy(selection_context),
        )

    def _walk(
        self,
        node: dict[str, Any],
        *,
        indices: list[int],
        selection_context: dict[str, Any] | None,
    ) -> ColumnVector:
        op = node["op"]
        if op == "column":
            field = node["field"]
            vector = self.batch.columns[field].subset(indices)
            self._store(
                node=node,
                vector=vector,
                indices=indices,
                selection_context=selection_context,
            )
            return vector
        if op == "literal":
            dtype = DType.from_json(node["dtype"])
            if dtype.kind == "decimal":
                value: Any = int(node["raw"])
            elif dtype.kind in {"int64", "bool"}:
                value = node.get("value", node.get("raw"))
            elif dtype.kind == "string":
                value = str(node.get("value", ""))
            else:
                value = None
            if dtype.kind == "null":
                vector = ColumnVector(
                    dtype=dtype,
                    values=[None] * len(indices),
                    null_mask=[True] * len(indices),
                )
            else:
                vector = ColumnVector(
                    dtype=dtype,
                    values=[value] * len(indices),
                    null_mask=[False] * len(indices),
                )
            self._store(
                node=node,
                vector=vector,
                indices=indices,
                selection_context=selection_context,
            )
            return vector
        if op == "conditional":
            return self._conditional(
                node, indices=indices, selection_context=selection_context
            )

        args = [
            self._walk(arg, indices=indices, selection_context=selection_context)
            for arg in node.get("args", [])
        ]
        result_type, targets = _infer_regular_node(op, [arg.dtype for arg in args])

        if op in {"add", "sub", "mul", "div"}:
            cast_args = [_cast(vector, target) for vector, target in zip(args, targets)]
            output: list[Any] = []
            null_position: list[bool] = []
            for left, right, left_null, right_null in zip(
                cast_args[0].values,
                cast_args[1].values,
                cast_args[0].null_mask,
                cast_args[1].null_mask,
            ):
                is_null = left_null or right_null
                null_position.append(is_null)
                if is_null:
                    output.append(None)
                elif op == "add":
                    output.append(int(left) + int(right))
                elif op == "sub":
                    output.append(int(left) - int(right))
                elif op == "mul":
                    output.append(int(left) * int(right))
                else:
                    output.append(
                        _quotient_toward_zero(
                            int(left) * 10 ** int(result_type.scale or 0),
                            int(right),
                        )
                    )
            vector = ColumnVector(
                dtype=result_type,
                values=output,
                null_mask=null_position,
            )
        elif op in {"eq", "ne", "lt", "le", "gt", "ge"}:
            cast_args = [_cast(vector, target) for vector, target in zip(args, targets)]
            output = []
            mask = []
            for left, right, left_null, right_null in zip(
                cast_args[0].values,
                cast_args[1].values,
                cast_args[0].null_mask,
                cast_args[1].null_mask,
            ):
                is_null = left_null or right_null
                mask.append(is_null)
                if is_null:
                    output.append(None)
                elif op == "eq":
                    output.append(left == right)
                elif op == "ne":
                    output.append(left != right)
                elif op == "lt":
                    output.append(left < right)
                elif op == "le":
                    output.append(left <= right)
                elif op == "gt":
                    output.append(left > right)
                else:
                    output.append(left >= right)
            vector = ColumnVector(dtype=BOOL, values=output, null_mask=mask)
        elif op in {"and", "or"}:
            left = _cast(args[0], BOOL)
            right = _cast(args[1], BOOL)
            output = [
                (
                    _three_valued_and(lvalue, rvalue)
                    if op == "and"
                    else _three_valued_or(lvalue, rvalue)
                )
                for lvalue, rvalue in zip(left.values, right.values)
            ]
            vector = ColumnVector(
                dtype=BOOL,
                values=output,
                null_mask=[value is None for value in output],
            )
        elif op == "not":
            source = _cast(args[0], BOOL)
            output = [None if value is None else not value for value in source.values]
            vector = ColumnVector(
                dtype=BOOL,
                values=output,
                null_mask=[value is None for value in output],
            )
        elif op == "abs":
            source = _cast(args[0], targets[0])
            output = [
                None if is_null else abs(int(value))
                for value, is_null in zip(source.values, source.null_mask)
            ]
            vector = ColumnVector(
                dtype=result_type,
                values=output,
                null_mask=list(source.null_mask),
            )
        elif op == "coalesce":
            cast_args = [_cast(vector, result_type) for vector in args]
            output = []
            mask = []
            for row_idx in range(len(indices)):
                value: Any = None
                for arg in cast_args:
                    if not arg.null_mask[row_idx]:
                        value = arg.values[row_idx]
                        break
                output.append(value)
                mask.append(value is None)
            vector = ColumnVector(dtype=result_type, values=output, null_mask=mask)
        elif op == "lower":
            source = _cast(args[0], DType("string"))
            output = [
                None if is_null else str(value).lower()
                for value, is_null in zip(source.values, source.null_mask)
            ]
            vector = ColumnVector(
                dtype=DType("string"),
                values=output,
                null_mask=list(source.null_mask),
            )
        elif op == "length":
            source = _cast(args[0], DType("string"))
            output = [
                None if is_null else len(str(value))
                for value, is_null in zip(source.values, source.null_mask)
            ]
            vector = ColumnVector(
                dtype=INT64,
                values=output,
                null_mask=list(source.null_mask),
            )
        elif op == "is_null":
            source = args[0]
            vector = ColumnVector(
                dtype=BOOL,
                values=list(source.null_mask),
                null_mask=[False] * len(source),
            )
        else:
            raise EvaluationError(f"reference cannot evaluate operator {op}")

        self._store(
            node=node,
            vector=vector,
            indices=indices,
            selection_context=selection_context,
        )
        return vector

    def _conditional(
        self,
        node: dict[str, Any],
        *,
        indices: list[int],
        selection_context: dict[str, Any] | None,
    ) -> ColumnVector:
        args = node.get("args", [])
        if len(args) != 3:
            raise EvaluationError("reference conditional needs three arguments")
        condition = self._walk(
            args[0], indices=indices, selection_context=selection_context
        )
        if condition.dtype.kind not in {"bool", "null"}:
            raise EvaluationError("reference condition is not bool")
        true_rows = [
            idx
            for idx, (value, is_null) in enumerate(
                zip(condition.values, condition.null_mask)
            )
            if not is_null and value is True
        ]
        false_rows = [
            idx
            for idx, (value, is_null) in enumerate(
                zip(condition.values, condition.null_mask)
            )
            if not is_null and value is False
        ]
        null_rows = [
            idx
            for idx, (value, is_null) in enumerate(
                zip(condition.values, condition.null_mask)
            )
            if is_null
        ]
        true_vector = self._walk(
            args[1],
            indices=[indices[idx] for idx in true_rows],
            selection_context={
                "branch": "true",
                "parent_node_id": node["node_id"],
                "selected_rows": len(true_rows),
            },
        )
        false_vector = self._walk(
            args[2],
            indices=[indices[idx] for idx in false_rows],
            selection_context={
                "branch": "false",
                "parent_node_id": node["node_id"],
                "selected_rows": len(false_rows),
            },
        )
        result_type = _common_type(
            [true_vector.dtype, false_vector.dtype], context="reference conditional"
        )
        true_aligned = _cast(true_vector, result_type)
        false_aligned = _cast(false_vector, result_type)
        values: list[Any] = [None] * len(indices)
        mask = [False] * len(indices)
        for idx in null_rows:
            mask[idx] = True
        for local_idx in true_rows:
            values[local_idx] = true_aligned.values[true_rows.index(local_idx)]
            mask[local_idx] = true_aligned.null_mask[true_rows.index(local_idx)]
        for local_idx in false_rows:
            values[local_idx] = false_aligned.values[false_rows.index(local_idx)]
            mask[local_idx] = false_aligned.null_mask[false_rows.index(local_idx)]
        for idx, is_null in enumerate(mask):
            if is_null:
                values[idx] = None
            elif values[idx] is None:
                raise EvaluationError("reference conditional merge lost a value")
        vector = ColumnVector(dtype=result_type, values=values, null_mask=mask)
        self._store(
            node=node,
            vector=vector,
            indices=indices,
            selection_context=selection_context,
        )
        return vector


def reference_evaluate_batch(
    model: NormalizedModel,
    batch: EvalBatch,
    *,
    budget: BatchBudget,
) -> ReferenceBatch:
    batch.validate()
    budget.check_input(
        batch_index=batch.batch_index,
        row_count=batch.row_count,
        input_columns=len(batch.columns),
    )
    expression_results: dict[str, ColumnVector] = {}
    node_results: dict[str, dict[str, ReferenceNodeResult]] = {}
    for expression in model.expressions:
        working_columns = len(batch.columns) + _tree_node_count(expression["root"])
        budget.check_working(
            batch_index=batch.batch_index,
            row_count=batch.row_count,
            working_columns=working_columns,
        )
        evaluator = _ReferenceBatchEvaluator(expression, batch)
        result = evaluator.evaluate()
        expression_results[expression["expression_id"]] = result
        node_results[expression["expression_id"]] = evaluator.node_results
    return ReferenceBatch(
        batch_index=batch.batch_index,
        row_start=batch.row_start,
        row_count=batch.row_count,
        expression_results=expression_results,
        node_results=node_results,
    )


def reference_evaluate_model(
    model: NormalizedModel,
    *,
    batch_rows: int = 32,
    budget: BatchBudget | None = None,
) -> ReferenceRun:
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
            stage="reference_batch_plan",
            metric="requested_batch_rows",
            observed=batch_rows,
            limit=budget.max_rows,
            policy=budget.rejection_policy,
        )
    if model.column_width() > budget.max_input_columns:
        raise BudgetRejected(
            stage="reference_batch_plan",
            metric="input_columns",
            observed=model.column_width(),
            limit=budget.max_input_columns,
            policy=budget.rejection_policy,
        )

    batches: list[ReferenceBatch] = []
    all_batch_expression_results: dict[str, list[ColumnVector]] = {
        expression["expression_id"]: [] for expression in model.expressions
    }
    batch_index = 0
    for row_start in range(0, model.row_count, batch_rows):
        row_count = min(batch_rows, model.row_count - row_start)
        batch = model.batch(
            batch_index=batch_index,
            row_start=row_start,
            row_count=row_count,
        )
        budget.check_input(
            batch_index=batch_index,
            row_count=batch.row_count,
            input_columns=len(batch.columns),
        )
        batch_evaluation = reference_evaluate_batch(model, batch, budget=budget)
        for expression_id, result in batch_evaluation.expression_results.items():
            all_batch_expression_results[expression_id].append(result)
        batches.append(batch_evaluation)
        batch_index += 1
    return ReferenceRun(
        version=model.version,
        row_count=model.row_count,
        batch_rows=batch_rows,
        batches=batches,
        expression_results={
            expression_id: _concat(vectors)
            for expression_id, vectors in all_batch_expression_results.items()
        },
    )
