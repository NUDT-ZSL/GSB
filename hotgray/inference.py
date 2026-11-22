from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .errors import ErrorCode, fail
from .util import digest_hex, sha256_bytes, sha256_json

INFERENCE_WIDTH = 8
INFERENCE_OUTPUT_DIM = 4


def _resource_param(
    binding: Mapping[str, Any],
    reference_name: str,
    key: str,
    default: float | int,
) -> float | int:
    references = binding.get("references", {})
    resources = binding.get("resources", {})
    resource_id = references.get(reference_name)
    if resource_id is None:
        return default
    resource = resources.get(resource_id)
    if not isinstance(resource, dict):
        return default
    params = resource.get("params")
    if not isinstance(params, dict):
        return default
    value = params.get(key, default)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def normalize_model_input(payload: Any) -> np.ndarray:
    if isinstance(payload, dict):
        values = payload.get("input")
    else:
        values = payload
    if not isinstance(values, (list, tuple)) or not values:
        fail(
            ErrorCode.INVALID_INPUT,
            "推理请求 input 必须为非空数值数组",
            actual=type(values).__name__,
        )
    if len(values) > 1024:
        fail(ErrorCode.INVALID_INPUT, "推理请求 input 长度不能超过 1024", actual=len(values))
    normalized: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            fail(ErrorCode.INVALID_INPUT, "推理请求 input 只能包含数值", actual=value)
        numeric = float(value)
        if not math.isfinite(numeric):
            fail(ErrorCode.INVALID_INPUT, "推理请求 input 不能包含 NaN/Infinity", actual=value)
        normalized.append(numeric)
    vector = np.asarray(normalized, dtype=np.float32)
    if vector.size < INFERENCE_WIDTH:
        vector = np.resize(vector, INFERENCE_WIDTH)
    return vector


def _load_shard_weights(
    blob_dir: str | Path,
    binding: Mapping[str, Any],
) -> list[tuple[str, np.ndarray]]:
    chain = binding.get("shard_chain")
    if not isinstance(chain, list) or not chain:
        fail(ErrorCode.INVALID_INPUT, "版本绑定缺少分片摘要链", request_id=binding.get("request_id"))
    loaded: list[tuple[str, np.ndarray]] = []
    base = Path(blob_dir)
    for item in chain:
        digest = item.get("digest")
        if not isinstance(digest, str):
            fail(ErrorCode.SHARD_DIGEST_MISMATCH, "版本绑定分片摘要非法")
        path = base / f"{digest_hex(digest)}.bin"
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            fail(
                ErrorCode.MISSING_SHARD,
                f"请求绑定所需的分片物化文件缺失: {path}",
                version=binding.get("version"),
                layer=item.get("layer"),
                shard_index=item.get("shard_index"),
                path=str(path),
            )
        actual = sha256_bytes(data)
        if actual != digest:
            fail(
                ErrorCode.SHARD_DIGEST_MISMATCH,
                "请求绑定分片物化文件摘要不匹配",
                version=binding.get("version"),
                layer=item.get("layer"),
                shard_index=item.get("shard_index"),
                expected=digest,
                actual=actual,
            )
        if len(data) % 4:
            fail(
                ErrorCode.SHARD_DIGEST_MISMATCH,
                "分片字节长度不是 float32 的整数倍",
                version=binding.get("version"),
                layer=item.get("layer"),
                shard_index=item.get("shard_index"),
                actual=len(data),
            )
        weights = np.frombuffer(data, dtype="<f4").astype(np.float32, copy=True)
        loaded.append((str(item.get("layer")), weights))
    return loaded


def compute_inference(
    *,
    blob_dir: str | Path,
    binding: Mapping[str, Any],
    payload: Any,
) -> dict[str, Any]:
    vector = normalize_model_input(payload)
    pre_scale = float(_resource_param(binding, "preprocess", "scale", 1.0))
    pre_offset = float(_resource_param(binding, "preprocess", "offset", 0.0))
    rotation = int(_resource_param(binding, "preprocess", "rotation", 0)) % INFERENCE_WIDTH
    post_scale = float(_resource_param(binding, "postprocess", "scale", 1.0))
    post_offset = float(_resource_param(binding, "postprocess", "offset", 0.0))
    clip = float(_resource_param(binding, "postprocess", "clip", 8.0))

    state = np.roll(vector * np.float32(pre_scale) + np.float32(pre_offset), rotation)
    loaded = _load_shard_weights(blob_dir, binding)
    previous_layer: str | None = None
    layer_weights: list[np.ndarray] = []
    groups: list[tuple[str, list[np.ndarray]]] = []
    for layer, weights in loaded:
        if previous_layer is None or layer != previous_layer:
            if previous_layer is not None:
                groups.append((previous_layer, layer_weights))
            previous_layer = layer
            layer_weights = []
        layer_weights.append(weights)
    if previous_layer is not None:
        groups.append((previous_layer, layer_weights))

    for _, shards in groups:
        weights = np.concatenate(shards)
        if weights.size == 0:
            continue
        columns = state.size
        row_count = min(INFERENCE_OUTPUT_DIM, max(1, weights.size // columns))
        selected_length = row_count * columns
        matrix = weights[:selected_length].reshape(row_count, columns)
        tail = weights[selected_length:]
        bias = np.float32(0.0 if tail.size == 0 else np.tanh(np.sum(tail, dtype=np.float64)))
        state = np.tanh((matrix @ state).astype(np.float32) + bias).astype(np.float32)
        if state.size < INFERENCE_WIDTH:
            state = np.resize(state, INFERENCE_WIDTH)

    output = state[:INFERENCE_OUTPUT_DIM] * np.float32(post_scale) + np.float32(post_offset)
    output = np.clip(output, -abs(clip), abs(clip)).astype("<f4")
    output_bytes = output.tobytes()
    output_hex = output_bytes.hex()
    result_digest = sha256_bytes(output_bytes)
    result = {
        "outputs": [float(value) for value in output],
        "output_bits": output_hex,
        "result_digest": result_digest,
        "binding_digest": binding.get("binding_digest"),
        "version": binding.get("version"),
    }
    result["result_fingerprint"] = sha256_json(
        {
            "binding_digest": binding.get("binding_digest"),
            "output_bits": output_hex,
        }
    )
    return result


def binding_stable_view(binding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": binding.get("version"),
        "binding_digest": binding.get("binding_digest"),
        "shard_chain": binding.get("shard_chain", []),
        "references": binding.get("references", {}),
        "resources": {
            resource_id: {
                "resource_id": resource_id,
                "digest": resource.get("digest"),
                "revision": resource.get("revision"),
                "kind": resource.get("kind"),
            }
            for resource_id, resource in sorted(binding.get("resources", {}).items())
        },
    }


def result_bytes(result: Mapping[str, Any]) -> bytes:
    bits = result.get("output_bits")
    if not isinstance(bits, str):
        fail(ErrorCode.INVALID_INPUT, "推理结果缺少 output_bits")
    return bytes.fromhex(bits)
