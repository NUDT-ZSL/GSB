from __future__ import annotations

from typing import Any

from .cache import make_node_key
from .canonical import sha256_json
from .kernels import ACCUMULATION_ORDER, LOOP_ORDER


PLAN_ALGORITHM = "validation-plan/v1"
ACCUMULATION_ALGORITHM = "neumaier-block-fma64/v1"
AGGREGATE_ALGORITHM = "reference-verify-spectrum/v1"


def chunk_count(spec: dict[str, Any], block_size: int) -> int:
    shapes = spec["matrix_shapes"]
    if spec["op_kind"] == "gemm":
        m_dim = shapes["A"][0]
        n_dim = shapes["B"][1]
        batch = 1
    else:
        batch, m_dim, _ = shapes["A"]
        n_dim = shapes["B"][2]
    m_blocks = (m_dim + block_size - 1) // block_size
    n_blocks = (n_dim + block_size - 1) // block_size
    return int(batch * m_blocks * n_blocks)


def build_plan(
    spec: dict[str, Any],
    array_hashes: dict[str, dict[str, Any]],
    *,
    effective_block_size: int,
    degradations: list[dict[str, Any]],
    memory_estimate: dict[str, Any],
) -> dict[str, Any]:
    document = {
        "algorithm": PLAN_ALGORITHM,
        "job_id": spec["job_id"],
        "op_kind": spec["op_kind"],
        "matrix_shapes": spec["matrix_shapes"],
        "dtype": spec["dtype"],
        "layout": spec["layout"],
        "block_size": spec["block_size"],
        "effective_block_size": int(effective_block_size),
        "seed": spec["seed"],
        "revision": spec["revision"],
        "array_hashes": array_hashes,
        "loop_order": LOOP_ORDER,
        "accumulation_order": ACCUMULATION_ORDER,
        "chunk_count": chunk_count(spec, effective_block_size),
        "degradations": degradations,
        "memory_estimate": memory_estimate,
        "validation": {
            "shapes": "ok",
            "dtype": "ok",
            "layout": "ok",
            "block_size": "ok",
        },
    }
    document["plan_hash"] = sha256_json({key: value for key, value in document.items() if key != "plan_hash"})
    return document


def plan_key_parts(spec: dict[str, Any], array_hashes: dict[str, dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "algorithm": PLAN_ALGORITHM,
        "job_id": spec["job_id"],
        "op_kind": spec["op_kind"],
        "matrix_shapes": spec["matrix_shapes"],
        "dtype": spec["dtype"],
        "layout": spec["layout"],
        "block_size": spec["block_size"],
        "effective_block_size": plan["effective_block_size"],
        "array_hashes": array_hashes,
        "upstream_product_hash": None,
    }


def accumulation_key_parts(
    spec: dict[str, Any],
    array_hashes: dict[str, dict[str, Any]],
    plan: dict[str, Any],
) -> dict[str, Any]:
    return {
        "algorithm": ACCUMULATION_ALGORITHM,
        "job_id": spec["job_id"],
        "op_kind": spec["op_kind"],
        "matrix_shapes": spec["matrix_shapes"],
        "dtype": spec["dtype"],
        "layout": spec["layout"],
        "block_size": spec["block_size"],
        "effective_block_size": plan["effective_block_size"],
        "alpha": spec["alpha"],
        "beta": spec["beta"],
        "array_hashes": array_hashes,
        "upstream_product_hash": plan["plan_hash"],
    }


def aggregate_key_parts(
    spec: dict[str, Any],
    array_hashes: dict[str, dict[str, Any]],
    accumulation_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "algorithm": AGGREGATE_ALGORITHM,
        "job_id": spec["job_id"],
        "op_kind": spec["op_kind"],
        "matrix_shapes": spec["matrix_shapes"],
        "dtype": spec["dtype"],
        "layout": spec["layout"],
        "block_size": spec["block_size"],
        "alpha": spec["alpha"],
        "beta": spec["beta"],
        "tolerance": spec["tolerance"],
        "array_hashes": array_hashes,
        "upstream_product_hash": accumulation_payload["accumulation_hash"],
    }


def derive_node_keys(
    spec: dict[str, Any],
    array_hashes: dict[str, dict[str, Any]],
    plan: dict[str, Any],
    accumulation_payload: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    plan_parts = plan_key_parts(spec, array_hashes, plan)
    plan_key = make_node_key("validation_plan", plan_parts)
    nodes = {
        "validation_plan": {"key": plan_key, "key_composition": plan_parts},
    }
    if accumulation_payload is not None:
        accum_parts = accumulation_key_parts(spec, array_hashes, plan)
        accum_key = make_node_key("block_accumulate", accum_parts)
        aggregate_parts = aggregate_key_parts(spec, array_hashes, accumulation_payload)
        aggregate_key = make_node_key("aggregate_validate", aggregate_parts)
        nodes["block_accumulate"] = {"key": accum_key, "key_composition": accum_parts}
        nodes["aggregate_validate"] = {"key": aggregate_key, "key_composition": aggregate_parts}
    return nodes
