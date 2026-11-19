"""Deterministic operator execution: filter, integer aggregation, float aggregation.

Determinism rules:
- input records are always processed in a fixed order (sorted by record id);
- integer aggregation uses exact arbitrary-precision ints;
- float aggregation uses math.fsum over a fixed order, so results are
  bit-identical across runs and platforms.
"""

from __future__ import annotations

import hashlib
import json
import math
import time

from .errors import E_BAD_PIPELINE, PipelineError

_CMP_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest_records(records: list[dict]) -> str:
    return hashlib.sha256(canonical_json(records)).hexdigest()


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sort_records(records: list[dict]) -> list[dict]:
    return sorted(records, key=lambda r: str(r.get("id", "")))


def execute_op(node_name: str, op: dict, input_records: list[dict]) -> tuple[list[dict], dict]:
    """Run one operator. Returns (output_records, trace_entry).

    trace_entry maps each output record id to the node that produced it and
    the ids of the input records it was derived from.
    """
    op_type = op.get("type")
    inputs = sort_records(input_records)

    if op_type == "filter":
        field = op.get("field")
        cmp_name = op.get("op")
        threshold = op.get("threshold")
        if field is None or cmp_name not in _CMP_OPS:
            raise PipelineError(E_BAD_PIPELINE, "filter op needs field and a valid comparison", node=node_name)
        cmp_fn = _CMP_OPS[cmp_name]
        out = [r for r in inputs if field in r and cmp_fn(r[field], threshold)]
        trace = {str(r["id"]): {"node": node_name, "inputs": [str(r["id"])]} for r in out}
        return out, trace

    if op_type == "aggregate_int":
        field = op.get("field")
        if field is None:
            raise PipelineError(E_BAD_PIPELINE, "aggregate_int op needs a field", node=node_name)
        total = 0
        for record in inputs:  # fixed order, exact int arithmetic
            total += int(record.get(field, 0))
        out_id = f"{node_name}:int_sum"
        return [{"id": out_id, "value": total}], {out_id: {"node": node_name, "inputs": [str(r["id"]) for r in inputs]}}

    if op_type == "aggregate_float":
        field = op.get("field")
        if field is None:
            raise PipelineError(E_BAD_PIPELINE, "aggregate_float op needs a field", node=node_name)
        values = [float(record.get(field, 0.0)) for record in inputs]  # fixed order
        total = math.fsum(values)  # correctly rounded, order-independent, bit-stable
        out_id = f"{node_name}:float_sum"
        return [{"id": out_id, "value": total}], {out_id: {"node": node_name, "inputs": [str(r["id"]) for r in inputs]}}

    if op_type == "sleep":
        # Test-only operator: deterministic output, used to simulate a slow node
        # so a crash can be forced mid-stage during recovery tests.
        time.sleep(int(op.get("sleep_ms", 0)) / 1000.0)
        return list(inputs), {str(r["id"]): {"node": node_name, "inputs": [str(r["id"])]} for r in inputs}

    raise PipelineError(E_BAD_PIPELINE, "unknown op type", node=node_name, op_type=op_type)
