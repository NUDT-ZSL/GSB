from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from .deterministic import block_policy_for
from .errors import BusinessError, E_MANIFEST_INVALID, E_RESOURCE_LIMIT
from .quantization import QuantizedLayer
from .serialization import array_checksum


REDUCTION_ORDER = (
    "output_block_ascending -> input_block_ascending -> k_ascending -> partial_sum_ascending"
)
NORMALIZATION_SCOPE = (
    "tanh applied once on the complete reduced tensor; no block-local normalization"
)


def resolve_block_policy(
    entry: dict[str, Any],
    *,
    batch_size: int,
    max_block_elements: int,
) -> tuple[dict[str, int | str], dict[str, Any] | None]:
    policy = block_policy_for(entry, batch_size=batch_size)
    try:
        row_block = int(policy["row_block"])
        col_block = int(policy["col_block"])
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            E_MANIFEST_INVALID,
            "block_policy 行列分块参数非法",
            stage="inference",
            layer_id=str(entry["layer_id"]),
            layer_index=int(entry["layer_index"]),
            details={"block_policy": policy},
        ) from exc
    if row_block < 1 or col_block < 1:
        raise BusinessError(
            E_MANIFEST_INVALID,
            "block_policy 分块大小必须为正整数",
            stage="inference",
            layer_id=str(entry["layer_id"]),
            layer_index=int(entry["layer_index"]),
            details={"block_policy": policy},
        )
    applied = dict(policy)
    degradation: dict[str, Any] | None = None
    if row_block * col_block > max_block_elements:
        while row_block * col_block > max_block_elements:
            if col_block > 1:
                col_block = max(1, col_block // 2)
            elif row_block > 1:
                row_block = max(1, row_block // 2)
            else:
                raise BusinessError(
                    E_RESOURCE_LIMIT,
                    "max_block_elements 过小，无法继续降级分块",
                    stage="inference",
                    layer_id=str(entry["layer_id"]),
                    layer_index=int(entry["layer_index"]),
                    details={"max_block_elements": max_block_elements},
                )
        applied["row_block"] = row_block
        applied["col_block"] = col_block
        degradation = {
            "category": "block_size",
            "stage": "inference",
            "layer_id": str(entry["layer_id"]),
            "layer_index": int(entry["layer_index"]),
            "original": {
                "row_block": int(policy["row_block"]),
                "col_block": int(policy["col_block"]),
            },
            "applied": {"row_block": row_block, "col_block": col_block},
            "reason": "max_block_elements",
        }
    return applied, degradation


def blocked_matmul(
    x: np.ndarray,
    weight: np.ndarray,
    policy: dict[str, int | str],
) -> np.ndarray:
    """Deterministic blocked GEMM with explicit ascending reduction order.

    ``row_block`` bounds output-feature rows and ``col_block`` bounds the
    reduction dimension. Batch rows are never normalized independently.
    """
    x = np.ascontiguousarray(x, dtype=np.float64)
    weight = np.ascontiguousarray(weight, dtype=np.float64)
    output_dim, input_dim = weight.shape
    if x.shape[-1] != input_dim:
        raise BusinessError(
            E_MANIFEST_INVALID,
            "推理输入维度与权重不一致",
            stage="inference",
            details={"input_dim": int(x.shape[-1]), "weight_input_dim": input_dim},
        )
    row_block = int(policy.get("row_block", 1))
    col_block = int(policy.get("col_block", 1))
    result = np.empty((x.shape[0], output_dim), dtype=np.float64)
    for out_start in range(0, output_dim, row_block):
        out_end = min(out_start + row_block, output_dim)
        accumulator = np.zeros((x.shape[0], out_end - out_start), dtype=np.float64)
        for in_start in range(0, input_dim, col_block):
            in_end = min(in_start + col_block, input_dim)
            # Explicit ascending-k accumulation makes the reduction order part of
            # the executable contract rather than an implicit BLAS detail.
            for k in range(in_start, in_end):
                accumulator = accumulator + (
                    x[:, k, None] * weight[out_start:out_end, k][None, :]
                )
        result[:, out_start:out_end] = accumulator
    return np.ascontiguousarray(result)


def activated(value: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(np.tanh(value))


def inference_layer_checksum(activation: np.ndarray) -> str:
    return array_checksum(activation)


def run_inference(
    entries: list[dict[str, Any]],
    records: dict[str, QuantizedLayer],
    initial: np.ndarray,
    *,
    max_block_elements: int,
    start_index: int = 0,
    prior_activations: dict[str, np.ndarray] | None = None,
) -> dict[str, Any]:
    """Run or continue the deterministic blocked inference chain."""
    activations: dict[str, np.ndarray] = dict(prior_activations or {})
    policies: dict[str, dict[str, int | str]] = {}
    degradations: list[dict[str, Any]] = []
    if start_index > 0:
        if not activations:
            raise BusinessError(
                E_MANIFEST_INVALID,
                "续算推理缺少上一已提交激活",
                stage="inference",
                details={"start_index": start_index},
            )
        previous = next((e for e in entries if int(e["layer_index"]) == start_index - 1), None)
        if previous is None:
            raise BusinessError(
                E_MANIFEST_INVALID,
                "续算推理起点无对应上游层",
                stage="inference",
                details={"start_index": start_index},
            )
        x = activations[str(previous["layer_id"])]
    else:
        x = np.ascontiguousarray(initial, dtype=np.float64)
    for entry in entries:
        layer_index = int(entry["layer_index"])
        if layer_index < start_index:
            continue
        layer_id = str(entry["layer_id"])
        if layer_id not in records:
            raise BusinessError(
                E_MANIFEST_INVALID,
                "推理所需量化快照权重缺失",
                stage="inference",
                layer_id=layer_id,
                layer_index=layer_index,
            )
        policy, degradation = resolve_block_policy(
            entry,
            batch_size=int(x.shape[0]),
            max_block_elements=max_block_elements,
        )
        if degradation:
            degradations.append(degradation)
        linear = blocked_matmul(x, records[layer_id].dequantized, policy)
        x = activated(linear)
        activations[layer_id] = x
        policies[layer_id] = policy
    final_layer = entries[-1]
    final_activation = activations[str(final_layer["layer_id"])]
    prediction = np.ascontiguousarray(np.mean(final_activation, axis=0))
    order = [str(entry["layer_id"]) for entry in entries]
    return {
        "activations": activations,
        "policies": policies,
        "degradations": degradations,
        "final_prediction": prediction,
        "layer_order": order,
        "reduction_order": REDUCTION_ORDER,
        "normalization_scope": NORMALIZATION_SCOPE,
    }


def run_full_path(
    entries: Iterable[dict[str, Any]],
    weights: dict[str, np.ndarray],
    initial: np.ndarray,
) -> dict[str, np.ndarray]:
    """Float64 reference: full GEMM, no blocks, same tanh activation."""
    x = np.ascontiguousarray(initial, dtype=np.float64)
    activations: dict[str, np.ndarray] = {}
    for entry in entries:
        layer_id = str(entry["layer_id"])
        x = activated(np.ascontiguousarray(x @ weights[layer_id].T))
        activations[layer_id] = x
    return activations


def run_block_isolated(
    entries: Iterable[dict[str, Any]],
    weights: dict[str, np.ndarray],
    reference_activations: dict[str, np.ndarray],
    initial: np.ndarray,
    *,
    max_block_elements: int,
) -> dict[str, np.ndarray]:
    """Measure block accumulation one layer at a time from reference inputs."""
    result: dict[str, np.ndarray] = {}
    previous = np.ascontiguousarray(initial, dtype=np.float64)
    for entry in entries:
        layer_id = str(entry["layer_id"])
        policy, _ = resolve_block_policy(
            entry,
            batch_size=int(previous.shape[0]),
            max_block_elements=max_block_elements,
        )
        result[layer_id] = activated(blocked_matmul(previous, weights[layer_id], policy))
        previous = reference_activations[layer_id]
    return result
