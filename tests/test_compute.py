import math

from pipeline_tool.operators import canonical_json, digest_records, execute_op, sort_records


def test_filter_is_deterministic(input_records):
    op = {"type": "filter", "field": "value", "op": ">=", "threshold": 10}
    out1, trace1 = execute_op("n", op, input_records)
    out2, trace2 = execute_op("n", op, list(reversed(input_records)))
    assert canonical_json(out1) == canonical_json(out2)
    assert trace1 == trace2
    assert {r["id"] for r in out1} == {"r02", "r03", "r05", "r06", "r07", "r09", "r10", "r12"}


def test_int_aggregation_exact(input_records):
    op = {"type": "aggregate_int", "field": "qty"}
    out, trace = execute_op("sum_qty", op, input_records)
    assert out[0]["value"] == sum(r["qty"] for r in input_records)
    assert out[0]["id"] == "sum_qty:int_sum"
    assert trace["sum_qty:int_sum"]["node"] == "sum_qty"
    assert len(trace["sum_qty:int_sum"]["inputs"]) == len(input_records)


def test_float_aggregation_bit_stable(input_records):
    op = {"type": "aggregate_float", "field": "score"}
    results = {execute_op("s", op, input_records)[0][0]["value"] for _ in range(50)}
    assert len(results) == 1
    expected = math.fsum(float(r["score"]) for r in input_records)
    assert results.pop() == expected


def test_float_sum_matches_fsum_not_naive_order():
    records = [{"id": f"r{i}", "v": v} for i, v in enumerate([1e16, 1.0, -1e16, 1.0])]
    out, _ = execute_op("s", {"type": "aggregate_float", "field": "v"}, records)
    assert out[0]["value"] == math.fsum([1e16, 1.0, -1e16, 1.0]) == 2.0


def test_digest_stable():
    records = [{"id": "a", "x": 1}, {"id": "b", "x": 2.5}]
    assert digest_records(records) == digest_records(sort_records(list(reversed(records))))
