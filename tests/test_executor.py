"""业务要求 3：逐批执行、实测观测与真值逐行一致、偏差定位到连接步。"""
import dataclasses

import pytest

from reopt import datagen, executor
from reopt.errors import BusinessError
from reopt.schema import parse_plan, parse_tables

TABLES = {
    "seed": 7,
    "tables": [
        {"name": "A", "rows": 1000, "columns": [
            {"name": "k_ab", "type": "int64", "ndv": 10, "width_bytes": 8},
            {"name": "pa", "type": "int64", "ndv": 100, "width_bytes": 8}]},
        {"name": "B", "rows": 800, "columns": [
            {"name": "k_ab", "type": "int64", "ndv": 10, "width_bytes": 8},
            {"name": "k_bc", "type": "int64", "ndv": 8, "width_bytes": 8}]},
        {"name": "C", "rows": 600, "columns": [
            {"name": "k_bc", "type": "int64", "ndv": 8, "width_bytes": 8},
            {"name": "pc", "type": "int64", "ndv": 50, "width_bytes": 8}]},
    ],
}
PLAN = {
    "algorithms_enum": ["nested_loop", "hash", "sort_merge"],
    "steps": [
        {"left": "A", "right": "B", "left_key": "k_ab", "right_key": "k_ab",
         "algorithm": "hash", "est_selectivity": 0.01, "est_cost": 10},
        {"left": "@prev", "right": "C", "left_key": "k_bc", "right_key": "k_bc",
         "algorithm": "hash", "est_selectivity": 0.01, "est_cost": 10},
    ],
}


def _base(upto_seq, rows_per_batch=30):
    _, tables = parse_tables(TABLES)
    plan = parse_plan(PLAN, tables)
    base = {}
    for name, table in tables.items():
        batches = [datagen.generate_batch(table, 7, seq, rows_per_batch)
                   for seq in range(1, upto_seq + 1)]
        base[name] = executor.build_base_relation(table, batches)
    return tables, plan, base


def test_measured_matches_truth_per_batch():
    for upto in (1, 2, 3):
        tables, plan, base = _base(upto)
        intermediates, observations = executor.run_pipeline(plan, base)
        executor.verify_against_truth(plan, base, intermediates)
        truth = executor.truth_join(plan, base)
        assert sorted(intermediates[-1].rows) == sorted(truth.rows)
        assert len(intermediates[-1].rows) > 0
        # 观测只来自已提交批次的完整结果
        assert observations[0].left_rows == 30 * upto
        assert observations[0].right_rows == 30 * upto


def test_algorithms_yield_same_result():
    tables, plan, base = _base(2)
    digests = set()
    for algo in ("nested_loop", "hash", "sort_merge"):
        steps = tuple(dataclasses.replace(s, algorithm=algo) for s in plan.steps)
        algo_plan = dataclasses.replace(plan, steps=steps)
        intermediates, _ = executor.run_pipeline(algo_plan, base)
        digests.add(executor.result_digest(intermediates[-1]))
    assert len(digests) == 1


def test_deviation_located_to_step():
    tables, plan, base = _base(2)
    intermediates, _ = executor.run_pipeline(plan, base)
    assert intermediates[1].rows
    # 篡改第 1 步（下标 1）的中间结果
    tampered = list(intermediates)
    tampered[1] = executor.Relation(tampered[1].schema, tampered[1].rows[:-1])
    assert executor.locate_deviation(plan, base, tampered) == 1
    with pytest.raises(BusinessError) as excinfo:
        executor.verify_against_truth(plan, base, tampered)
    assert "第 1 个连接步" in excinfo.value.message
