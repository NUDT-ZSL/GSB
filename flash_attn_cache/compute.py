from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .errors import BusinessError, ErrorCode, Issue
from .graph import NodeSpec
from .models import SoftmaxConfig


def quantize_stats(values: np.ndarray, config: SoftmaxConfig) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if config.rounding_precision >= 52:
        return np.array(values, dtype=np.float64, copy=True, order="C")
    mantissa, exponent = np.frexp(values)
    resolution = float(1 << config.rounding_precision)
    scaled = mantissa * resolution
    if config.rounding_mode == "nearest":
        rounded = np.rint(scaled)
    else:
        rounded = np.trunc(scaled)
    return np.ascontiguousarray(np.ldexp(rounded / resolution, exponent))


def protect_norm(
    values: np.ndarray,
    config: SoftmaxConfig,
    *,
    node_id: str,
    block_index: int | tuple[int, ...] | None,
    case_id: str,
) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if not np.isfinite(values).all():
        raise BusinessError(
            Issue(
                ErrorCode.SOFTMAX_NAN,
                "softmax normalization factor is not finite",
                case_id=case_id,
                node_id=node_id,
                block_index=block_index,
                category="nonfinite_norm",
            )
        )
    negative = values < 0
    if negative.any():
        raise BusinessError(
            Issue(
                ErrorCode.SOFTMAX_NAN,
                "softmax normalization factor is negative",
                case_id=case_id,
                node_id=node_id,
                block_index=block_index,
                category="negative_norm",
            )
        )
    zero = values == 0
    if zero.any() and config.subnormal_floor <= 0:
        raise BusinessError(
            Issue(
                ErrorCode.NORM_ZERO,
                "softmax normalization factor is zero",
                case_id=case_id,
                node_id=node_id,
                block_index=block_index,
                category="zero_norm",
            )
        )
    protected = np.array(values, dtype=np.float64, copy=True, order="C")
    if config.subnormal_floor > 0:
        protected = np.maximum(protected, config.subnormal_floor)
    return np.ascontiguousarray(protected)


def merge_online_state(
    previous: tuple[np.ndarray, np.ndarray, np.ndarray] | None,
    chunk_max: np.ndarray,
    chunk_sum: np.ndarray,
    chunk_acc: np.ndarray,
    config: SoftmaxConfig,
    *,
    node_id: str,
    block_index: int | tuple[int, ...] | None,
    case_id: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    chunk_max = np.asarray(chunk_max, dtype=np.float64)
    chunk_sum = np.asarray(chunk_sum, dtype=np.float64)
    chunk_acc = np.asarray(chunk_acc, dtype=np.float64)
    if previous is None:
        merged_max = quantize_stats(chunk_max, config)
        merged_sum = protect_norm(
            quantize_stats(chunk_sum, config),
            config,
            node_id=node_id,
            block_index=block_index,
            case_id=case_id,
        )
        merged_acc = np.ascontiguousarray(chunk_acc)
    else:
        prev_max, prev_sum, prev_acc = previous
        merged_max = quantize_stats(
            np.maximum(prev_max, chunk_max),
            config,
        )
        alpha = np.exp(prev_max - merged_max)
        beta = np.exp(chunk_max - merged_max)
        raw_sum = prev_sum * alpha + chunk_sum * beta
        merged_acc = prev_acc * alpha[..., None] + chunk_acc * beta[..., None]
        merged_sum = protect_norm(
            quantize_stats(raw_sum, config),
            config,
            node_id=node_id,
            block_index=block_index,
            case_id=case_id,
        )
    if not np.isfinite(merged_max).all() or not np.isfinite(merged_sum).all():
        raise BusinessError(
            Issue(
                ErrorCode.SOFTMAX_NAN,
                "merged online-softmax statistics are not finite",
                case_id=case_id,
                node_id=node_id,
                block_index=block_index,
                category="nonfinite_merge",
            )
        )
    if not np.isfinite(merged_acc).all():
        raise BusinessError(
            Issue(
                ErrorCode.SOFTMAX_NAN,
                "merged attention accumulator is not finite",
                case_id=case_id,
                node_id=node_id,
                block_index=block_index,
                category="nonfinite_acc",
            )
        )
    return (
        np.ascontiguousarray(merged_max),
        np.ascontiguousarray(merged_sum),
        np.ascontiguousarray(merged_acc),
    )


def _block_index(node: NodeSpec) -> int | tuple[int, ...] | None:
    if node.query_block is not None and node.kv_block is not None:
        return (node.query_block, node.kv_block)
    return node.block_index


def _score_arrays(
    node: NodeSpec,
    role_arrays: Mapping[str, Mapping[str, np.ndarray]],
    softmax: SoftmaxConfig,
    case_id: str,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    q = np.asarray(role_arrays["qproj"]["q"], dtype=np.float64)
    k = np.asarray(role_arrays["kvload"]["k"], dtype=np.float64)
    scale = float(node.common["scale"])
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        scores = np.matmul(q, np.swapaxes(k, -1, -2)) * scale
    if scores.shape != node.output_shapes["scores"]:
        raise BusinessError(
            Issue(
                ErrorCode.INCONSISTENT,
                f"score shape {scores.shape} does not match graph shape "
                f"{node.output_shapes['scores']}",
                case_id=case_id,
                node_id=node.node_id,
                block_index=_block_index(node),
                category="score_shape",
            )
        )
    query_start = int(node.params["query_start"])
    query_len = int(node.params["query_len"])
    kv_start = int(node.params["kv_start"])
    kv_len = int(node.params["kv_len"])
    causal = bool(node.params["causal"])
    valid = np.ones((query_len, kv_len), dtype=bool)
    if causal:
        query_positions = np.arange(query_start, query_start + query_len)
        kv_positions = np.arange(kv_start, kv_start + kv_len)
        valid = kv_positions[None, :] <= query_positions[:, None]
    nan_or_posinf = np.isnan(scores) | np.isposinf(scores)
    if nan_or_posinf.any():
        raise BusinessError(
            Issue(
                ErrorCode.SOFTMAX_NAN,
                "score block contains NaN or positive infinity",
                case_id=case_id,
                node_id=node.node_id,
                block_index=_block_index(node),
                category="nonfinite_score",
            )
        )
    valid_mask = np.broadcast_to(valid[None, :, :], scores.shape)
    if np.isneginf(scores[valid_mask]).any():
        raise BusinessError(
            Issue(
                ErrorCode.SOFTMAX_NAN,
                "score block contains negative infinity inside the causal mask",
                case_id=case_id,
                node_id=node.node_id,
                block_index=_block_index(node),
                category="nonfinite_score",
            )
        )
    if not causal:
        scores = np.ascontiguousarray(scores)
    else:
        scores = np.ascontiguousarray(
            np.where(valid[None, :, :], scores, -np.inf)
        )
    metadata = {
        "query_start": query_start,
        "query_len": query_len,
        "kv_start": kv_start,
        "kv_len": kv_len,
        "causal": causal,
        "valid_count": int(valid.sum()),
    }
    return {"scores": scores}, metadata


def _softmax_arrays(
    node: NodeSpec,
    role_arrays: Mapping[str, Mapping[str, np.ndarray]],
    softmax: SoftmaxConfig,
    case_id: str,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    scores = np.asarray(role_arrays["score"]["scores"], dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        row_max = np.max(scores, axis=-1)
        if not np.isfinite(row_max).all():
            raise BusinessError(
                Issue(
                    ErrorCode.SOFTMAX_NAN,
                    "chunk softmax maximum is not finite",
                    case_id=case_id,
                    node_id=node.node_id,
                    block_index=_block_index(node),
                    category="nonfinite_chunk_max",
                )
            )
        rounded_max = quantize_stats(row_max, softmax)
        weights = np.exp(scores - rounded_max[..., None])
        row_sum = np.sum(weights, axis=-1)
        rounded_sum = protect_norm(
            quantize_stats(row_sum, softmax),
            softmax,
            node_id=node.node_id,
            block_index=_block_index(node),
            case_id=case_id,
        )
    if not np.isfinite(weights).all():
        raise BusinessError(
            Issue(
                ErrorCode.SOFTMAX_NAN,
                "chunk softmax weights are not finite",
                case_id=case_id,
                node_id=node.node_id,
                block_index=_block_index(node),
                category="nonfinite_weights",
            )
        )
    arrays = {
        "weights": np.ascontiguousarray(weights),
        "max": np.ascontiguousarray(rounded_max),
        "sum": np.ascontiguousarray(rounded_sum),
    }
    metadata = {
        "subnormal_floor": softmax.subnormal_floor,
        "rounding_mode": softmax.rounding_mode,
        "rounding_precision": softmax.rounding_precision,
    }
    return arrays, metadata


def _accum_arrays(
    node: NodeSpec,
    role_arrays: Mapping[str, Mapping[str, np.ndarray]],
    softmax: SoftmaxConfig,
    case_id: str,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    weights = np.asarray(
        role_arrays["softmax"]["weights"], dtype=np.float64
    )
    chunk_max = np.asarray(role_arrays["softmax"]["max"], dtype=np.float64)
    chunk_sum = np.asarray(role_arrays["softmax"]["sum"], dtype=np.float64)
    v = np.asarray(role_arrays["kvload"]["v"], dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        chunk_acc = np.matmul(weights, v)
    previous = None
    if "prior_accum" in role_arrays:
        prior = role_arrays["prior_accum"]
        previous = (
            np.asarray(prior["max"], dtype=np.float64),
            np.asarray(prior["sum"], dtype=np.float64),
            np.asarray(prior["acc"], dtype=np.float64),
        )
    merged_max, merged_sum, merged_acc = merge_online_state(
        previous,
        chunk_max,
        chunk_sum,
        chunk_acc,
        softmax,
        node_id=node.node_id,
        block_index=_block_index(node),
        case_id=case_id,
    )
    metadata = {
        "subnormal_floor": softmax.subnormal_floor,
        "rounding_mode": softmax.rounding_mode,
        "rounding_precision": softmax.rounding_precision,
    }
    return {
        "max": merged_max,
        "sum": merged_sum,
        "acc": merged_acc,
    }, metadata


def _finalize_arrays(
    node: NodeSpec,
    role_arrays: Mapping[str, Mapping[str, np.ndarray]],
    softmax: SoftmaxConfig,
    case_id: str,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    accum = role_arrays["final_accum"]
    denominator = np.asarray(accum["sum"], dtype=np.float64)
    denominator = protect_norm(
        denominator,
        softmax,
        node_id=node.node_id,
        block_index=_block_index(node),
        case_id=case_id,
    )
    numerator = np.asarray(accum["acc"], dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        out = numerator / denominator[..., None]
    if not np.isfinite(out).all():
        raise BusinessError(
            Issue(
                ErrorCode.SOFTMAX_NAN,
                "final attention output is not finite",
                case_id=case_id,
                node_id=node.node_id,
                block_index=_block_index(node),
                category="nonfinite_output",
            )
        )
    return {
        "out": np.ascontiguousarray(out),
        "max": np.ascontiguousarray(accum["max"]),
        "sum": np.ascontiguousarray(denominator),
    }, {
        "subnormal_floor": softmax.subnormal_floor,
        "rounding_mode": softmax.rounding_mode,
        "rounding_precision": softmax.rounding_precision,
    }


def execute_node(
    node: NodeSpec,
    upstream_arrays: Mapping[str, Mapping[str, np.ndarray]],
    softmax: SoftmaxConfig,
    case_id: str,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    role_arrays = {
        role: upstream_arrays[upstream_id]
        for upstream_id, role in zip(node.upstream, node.upstream_roles)
    }
    if node.kind == "split_q":
        return {"q": np.ascontiguousarray(node.local_arrays["q"])}, dict(node.params)
    if node.kind == "split_kv":
        return {
            "k": np.ascontiguousarray(node.local_arrays["k"]),
            "v": np.ascontiguousarray(node.local_arrays["v"]),
        }, dict(node.params)
    if node.kind == "qproj":
        return {
            "q": np.ascontiguousarray(role_arrays["split_q"]["q"])
        }, dict(node.params)
    if node.kind == "kvload":
        return {
            "k": np.ascontiguousarray(role_arrays["split_kv"]["k"]),
            "v": np.ascontiguousarray(role_arrays["split_kv"]["v"]),
        }, {
            **dict(node.params),
            **dict(role_arrays["split_kv"].get("__metadata__", {})),
        }
    if node.kind == "score":
        return _score_arrays(node, role_arrays, softmax, case_id)
    if node.kind == "softmax":
        return _softmax_arrays(node, role_arrays, softmax, case_id)
    if node.kind == "accum":
        return _accum_arrays(node, role_arrays, softmax, case_id)
    if node.kind == "finalize":
        return _finalize_arrays(node, role_arrays, softmax, case_id)
    raise BusinessError(
        Issue(
            ErrorCode.NODE_FAILURE,
            f"unknown node kind '{node.kind}'",
            case_id=case_id,
            node_id=node.node_id,
            block_index=_block_index(node),
            category="unknown_node_kind",
        )
    )
