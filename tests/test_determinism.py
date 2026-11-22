from __future__ import annotations

from copy import deepcopy

import numpy as np

from stream_reopt.data import generate_batch, input_digest
from stream_reopt.join import execute_batch
from stream_reopt.models import ExecutionShape, load_scenario, parse_scenario


def test_batch_generation_and_execution_are_bit_reproducible(sample_dict: dict) -> None:
    scenario = parse_scenario(sample_dict)
    batch = scenario.plan.batches[3]
    first = generate_batch(scenario, batch)
    second = generate_batch(scenario, batch)
    assert input_digest(batch, first) == input_digest(batch, second)
    for table_name in first:
        for column_name in first[table_name].columns:
            assert np.array_equal(
                first[table_name].columns[column_name],
                second[table_name].columns[column_name],
            )
    result_a = execute_batch(scenario, batch, first, scenario.initial_shape)
    result_b = execute_batch(scenario, batch, second, scenario.initial_shape)
    assert result_a.execution_digest == result_b.execution_digest
    assert result_a.output_digest == result_b.output_digest


def test_seed_changes_logical_batch(sample_dict: dict) -> None:
    data = deepcopy(sample_dict)
    scenario_a = parse_scenario(data)
    data["plan"]["seed"] += 1
    scenario_b = parse_scenario(data)
    batch = scenario_a.plan.batches[3]
    assert input_digest(batch, generate_batch(scenario_a, batch)) != input_digest(
        batch, generate_batch(scenario_b, batch)
    )


def test_different_join_orders_produce_same_result_multiset() -> None:
    scenario = load_scenario("samples/adaptive_join_sample.json")
    batch = scenario.plan.batches[5]
    tables = generate_batch(scenario, batch)
    initial = execute_batch(scenario, batch, tables, scenario.initial_shape)
    alternate = ExecutionShape(
        version=2,
        join_order=("customers", "orders", "lineitem"),
        algorithms=("hash", "hash"),
    )
    switched = execute_batch(scenario, batch, tables, alternate)
    assert initial.output_rows == switched.output_rows
    assert initial.output_digest == switched.output_digest
    assert initial.execution_digest != switched.execution_digest


def test_algorithms_agree_on_result() -> None:
    scenario = load_scenario("samples/adaptive_join_sample.json")
    batch = scenario.plan.batches[2]
    tables = generate_batch(scenario, batch)
    results = []
    for algorithm in ("hash", "sort_merge", "nested_loop"):
        shape = ExecutionShape(
            version=1,
            join_order=scenario.plan.initial_order,
            algorithms=(algorithm, algorithm),
        )
        results.append(execute_batch(scenario, batch, tables, shape))
    assert len({result.output_digest for result in results}) == 1
    assert len({result.output_rows for result in results}) == 1

