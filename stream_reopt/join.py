"""Batch-at-a-time join execution for a materialized execution shape."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import digest_value
from .data import TableBatch, input_digest, result_digest
from .errors import BusinessError, ErrorCode
from .models import BatchSpec, ExecutionShape, JoinPredicate, Scenario


@dataclass(frozen=True)
class StageObservation:
    step: int
    predicate_ids: tuple[str, ...]
    source_table: str
    source_key: str
    added_table: str
    algorithm: str
    left_rows: int
    right_rows: int
    output_rows: int
    output_digest: str

    @property
    def input_rows(self) -> int:
        return self.left_rows + self.right_rows

    @property
    def pair_count(self) -> int:
        return self.left_rows * self.right_rows

    @property
    def selectivity_numerator(self) -> int:
        return self.output_rows

    @property
    def selectivity_denominator(self) -> int:
        return max(1, self.pair_count)

    @property
    def join_id(self) -> str:
        return "+".join(self.predicate_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "predicate_ids": list(self.predicate_ids),
            "join_id": self.join_id,
            "source_table": self.source_table,
            "source_key": self.source_key,
            "added_table": self.added_table,
            "algorithm": self.algorithm,
            "left_rows": self.left_rows,
            "right_rows": self.right_rows,
            "input_rows": self.input_rows,
            "pair_count": self.pair_count,
            "output_rows": self.output_rows,
            "selectivity_numerator": self.selectivity_numerator,
            "selectivity_denominator": self.selectivity_denominator,
            "output_digest": self.output_digest,
        }


@dataclass(frozen=True)
class ExecutionResult:
    batch: BatchSpec
    batch_id: str
    shape: ExecutionShape
    input_digest: str
    output_rows: int
    output_digest: str
    columns: Mapping[str, np.ndarray]
    stages: tuple[StageObservation, ...]
    execution_digest: str

    @property
    def stage_output_rows(self) -> int:
        return sum(stage.output_rows for stage in self.stages)

    def summary(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "shape_version": self.shape.version,
            "shape_digest": self.shape.content_digest,
            "input_digest": self.input_digest,
            "output_rows": self.output_rows,
            "output_digest": self.output_digest,
            "stages": [stage.to_dict() for stage in self.stages],
            "execution_digest": self.execution_digest,
        }

    def stage_dicts(self) -> list[dict[str, Any]]:
        return [stage.to_dict() for stage in self.stages]


def _predicate_constraints(
    predicates: Sequence[JoinPredicate],
    next_table: str,
) -> list[tuple[str, str, str, JoinPredicate]]:
    constraints: list[tuple[str, str, str, JoinPredicate]] = []
    for predicate in predicates:
        if predicate.right_table == next_table:
            left_name = f"{predicate.left_table}.{predicate.left_column}"
            right_name = predicate.right_column
        else:
            left_name = f"{predicate.right_table}.{predicate.right_column}"
            right_name = predicate.left_column
        constraints.append((left_name, right_name, predicate.operator, predicate))
    return constraints


def _compare(left: Any, right: Any, operator: str) -> bool:
    if operator in {"=", "=="}:
        return left == right
    if operator == "!=":
        return left != right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    raise AssertionError(f"validated predicate operator escaped validation: {operator}")


def _mask_for_pairs(
    constraints: Sequence[tuple[str, str, str, JoinPredicate]],
    pipeline: Mapping[str, np.ndarray],
    right_batch: TableBatch,
    left_indices: np.ndarray,
    right_indices: np.ndarray,
) -> np.ndarray:
    mask = np.ones(len(left_indices), dtype=bool)
    for left_name, right_name, operator, _ in constraints:
        left_values = pipeline[left_name][left_indices]
        right_values = right_batch.columns[right_name][right_indices]
        if operator in {"=", "=="}:
            mask &= left_values == right_values
        elif operator == "!=":
            mask &= left_values != right_values
        elif operator == "<":
            mask &= left_values < right_values
        elif operator == "<=":
            mask &= left_values <= right_values
        elif operator == ">":
            mask &= left_values > right_values
        elif operator == ">=":
            mask &= left_values >= right_values
    return mask


def _nested_loop_join(
    constraints: Sequence[tuple[str, str, str, JoinPredicate]],
    pipeline: Mapping[str, np.ndarray],
    right_batch: TableBatch,
) -> tuple[np.ndarray, np.ndarray]:
    left_rows = len(next(iter(pipeline.values()))) if pipeline else 0
    right_rows = right_batch.rows
    if left_rows == 0 or right_rows == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    mask = np.ones((left_rows, right_rows), dtype=bool)
    for left_name, right_name, operator, _ in constraints:
        left_values = pipeline[left_name]
        right_values = right_batch.columns[right_name]
        if operator in {"=", "=="}:
            mask &= np.equal.outer(left_values, right_values)
        elif operator == "!=":
            mask &= np.not_equal.outer(left_values, right_values)
        elif operator == "<":
            mask &= np.less.outer(left_values, right_values)
        elif operator == "<=":
            mask &= np.less_equal.outer(left_values, right_values)
        elif operator == ">":
            mask &= np.greater.outer(left_values, right_values)
        elif operator == ">=":
            mask &= np.greater_equal.outer(left_values, right_values)
    left_indices, right_indices = np.nonzero(mask)
    return left_indices.astype(np.int64, copy=False), right_indices.astype(np.int64, copy=False)


def _hash_join(
    constraints: Sequence[tuple[str, str, str, JoinPredicate]],
    pipeline: Mapping[str, np.ndarray],
    right_batch: TableBatch,
) -> tuple[np.ndarray, np.ndarray]:
    left_rows = len(next(iter(pipeline.values()))) if pipeline else 0
    right_rows = right_batch.rows
    if left_rows == 0 or right_rows == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    equality = [item for item in constraints if item[2] in {"=", "=="}]
    if not equality:
        return _nested_loop_join(constraints, pipeline, right_batch)
    left_key_arrays = [pipeline[item[0]] for item in equality]
    right_key_arrays = [right_batch.columns[item[1]] for item in equality]
    left_keys = list(zip(*(array.tolist() for array in left_key_arrays)))
    right_keys = list(zip(*(array.tolist() for array in right_key_arrays)))
    buckets: dict[tuple[Any, ...], list[int]] = {}
    for index, key in enumerate(left_keys):
        buckets.setdefault(key, []).append(index)
    left_result: list[int] = []
    right_result: list[int] = []
    for right_index, key in enumerate(right_keys):
        candidates = buckets.get(key)
        if not candidates:
            continue
        candidate_array = np.asarray(candidates, dtype=np.int64)
        right_array = np.full(len(candidates), right_index, dtype=np.int64)
        reserved = [
            item
            for item in constraints
            if item[2] not in {"=", "=="}
        ]
        if reserved:
            mask = _mask_for_pairs(reserved, pipeline, right_batch, candidate_array, right_array)
            candidate_array = candidate_array[mask]
            right_array = right_array[mask]
        left_result.extend(candidate_array.tolist())
        right_result.extend(right_array.tolist())
    return (
        np.asarray(left_result, dtype=np.int64),
        np.asarray(right_result, dtype=np.int64),
    )


def _sort_merge_join(
    constraints: Sequence[tuple[str, str, str, JoinPredicate]],
    pipeline: Mapping[str, np.ndarray],
    right_batch: TableBatch,
) -> tuple[np.ndarray, np.ndarray]:
    equality = [item for item in constraints if item[2] in {"=", "=="}]
    if not equality:
        return _nested_loop_join(constraints, pipeline, right_batch)
    left_rows = len(next(iter(pipeline.values()))) if pipeline else 0
    right_rows = right_batch.rows
    if left_rows == 0 or right_rows == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    left_keys = list(
        zip(*(pipeline[item[0]].tolist() for item in equality))
    )
    right_keys = list(
        zip(*(right_batch.columns[item[1]].tolist() for item in equality))
    )
    left_order = sorted(range(left_rows), key=left_keys.__getitem__)
    right_order = sorted(range(right_rows), key=right_keys.__getitem__)
    residual = [item for item in constraints if item[2] not in {"=", "=="}]
    left_result: list[int] = []
    right_result: list[int] = []
    i = 0
    j = 0
    while i < left_rows and j < right_rows:
        left_key = left_keys[left_order[i]]
        right_key = right_keys[right_order[j]]
        if left_key < right_key:
            i += 1
            continue
        if left_key > right_key:
            j += 1
            continue
        left_end = i + 1
        while left_end < left_rows and left_keys[left_order[left_end]] == left_key:
            left_end += 1
        right_end = j + 1
        while right_end < right_rows and right_keys[right_order[right_end]] == right_key:
            right_end += 1
        left_group = np.asarray(left_order[i:left_end], dtype=np.int64)
        right_group = np.asarray(right_order[j:right_end], dtype=np.int64)
        left_pairs = np.repeat(left_group, len(right_group))
        right_pairs = np.tile(right_group, len(left_group))
        if residual:
            mask = _mask_for_pairs(residual, pipeline, right_batch, left_pairs, right_pairs)
            left_pairs = left_pairs[mask]
            right_pairs = right_pairs[mask]
        left_result.extend(left_pairs.tolist())
        right_result.extend(right_pairs.tolist())
        i = left_end
        j = right_end
    return (
        np.asarray(left_result, dtype=np.int64),
        np.asarray(right_result, dtype=np.int64),
    )


def _join_pair(
    algorithm: str,
    constraints: Sequence[tuple[str, str, str, JoinPredicate]],
    pipeline: Mapping[str, np.ndarray],
    right_batch: TableBatch,
) -> tuple[np.ndarray, np.ndarray]:
    if algorithm == "hash":
        return _hash_join(constraints, pipeline, right_batch)
    if algorithm == "sort_merge":
        return _sort_merge_join(constraints, pipeline, right_batch)
    if algorithm == "nested_loop":
        return _nested_loop_join(constraints, pipeline, right_batch)
    raise BusinessError(
        ErrorCode.BAD_PLAN,
        f"unsupported execution algorithm {algorithm!r}",
        details={"algorithm": algorithm},
    )


def _project_join(
    step: int,
    pipeline: Mapping[str, np.ndarray],
    right_batch: TableBatch,
    left_indices: np.ndarray,
    right_indices: np.ndarray,
) -> dict[str, np.ndarray]:
    output: dict[str, np.ndarray] = {}
    for name, values in pipeline.items():
        output[name] = values[left_indices]
    for name, values in right_batch.columns.items():
        output[f"{right_batch.name}.{name}"] = values[right_indices]
    return output


def _stage_identity(
    predicates: Sequence[JoinPredicate],
    next_table: str,
) -> tuple[tuple[str, ...], str, str]:
    if not predicates:
        raise BusinessError(
            ErrorCode.BAD_PLAN,
            f"no join predicate connects {next_table}",
            details={"table": next_table},
        )
    ordered = sorted(predicates, key=lambda predicate: predicate.id)
    first = ordered[0]
    return tuple(predicate.id for predicate in ordered), first.source_table, first.source_key


def execute_batch(
    scenario: Scenario,
    batch: BatchSpec,
    table_batches: Mapping[str, TableBatch],
    shape: ExecutionShape,
    stage_callback: Callable[[int], None] | None = None,
) -> ExecutionResult:
    if len(shape.join_order) != len(scenario.tables):
        raise BusinessError(
            ErrorCode.BAD_PLAN,
            "execution shape does not cover all tables",
            details={"shape": shape.to_dict()},
        )
    if not scenario.plan.valid_order(shape.join_order):
        raise BusinessError(
            ErrorCode.BAD_PLAN,
            "execution shape join order is disconnected",
            details={"shape": shape.to_dict()},
        )
    if len(shape.algorithms) != len(shape.join_order) - 1:
        raise BusinessError(
            ErrorCode.BAD_PLAN,
            "execution shape algorithm count is invalid",
            details={"shape": shape.to_dict()},
        )
    first_table = shape.join_order[0]
    first_batch = table_batches[first_table]
    pipeline: dict[str, np.ndarray] = {
        f"{first_table}.{name}": values
        for name, values in first_batch.columns.items()
    }
    stages: list[StageObservation] = []
    joined = [first_table]
    for step, next_table in enumerate(shape.join_order[1:]):
        predicates = scenario.plan.predicates_for_step(joined, next_table)
        if not predicates:
            raise BusinessError(
                ErrorCode.BAD_PLAN,
                f"join order becomes disconnected before {next_table}",
                details={"join_order": list(shape.join_order), "table": next_table},
            )
        predicate_ids, source_table, source_key = _stage_identity(predicates, next_table)
        constraints = _predicate_constraints(predicates, next_table)
        right_batch = table_batches[next_table]
        left_rows = len(next(iter(pipeline.values()))) if pipeline else 0
        left_indices, right_indices = _join_pair(
            shape.algorithms[step], constraints, pipeline, right_batch
        )
        projected = _project_join(step + 1, pipeline, right_batch, left_indices, right_indices)
        output_rows = len(left_indices)
        stage_digest = result_digest(projected, output_rows)
        stages.append(
            StageObservation(
                step=step,
                predicate_ids=predicate_ids,
                source_table=source_table,
                source_key=source_key,
                added_table=next_table,
                algorithm=shape.algorithms[step],
                left_rows=left_rows,
                right_rows=right_batch.rows,
                output_rows=output_rows,
                output_digest=stage_digest,
            )
        )
        pipeline = projected
        joined.append(next_table)
        if stage_callback is not None:
            stage_callback(step)
    output_rows = len(next(iter(pipeline.values()))) if pipeline else 0
    output_digest = result_digest(pipeline, output_rows)
    batch_input_digest = input_digest(batch, table_batches)
    execution_digest = digest_value(
        {
            "batch_id": batch.batch_id,
            "input_digest": batch_input_digest,
            "shape_version": shape.version,
            "shape_digest": shape.content_digest,
            "stages": [stage.to_dict() for stage in stages],
            "output_rows": output_rows,
            "output_digest": output_digest,
        }
    )
    return ExecutionResult(
        batch=batch,
        batch_id=batch.batch_id,
        shape=shape,
        input_digest=batch_input_digest,
        output_rows=output_rows,
        output_digest=output_digest,
        columns=pipeline,
        stages=tuple(stages),
        execution_digest=execution_digest,
    )

