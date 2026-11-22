from __future__ import annotations

from typing import Any

import numpy as np

from .derivation import (
    CheckpointArtifacts,
    build_checkpoint_artifacts,
    parse_shard,
)
from .errors import fail
from .plans import (
    DTYPES,
    SavePlan,
    ShardPolicy,
    TrainingPlan,
    validate_manifest_shape,
    validate_shard_policy,
    validate_training_plan,
)
from .store import ObjectStore
from .util import canonical_json_bytes, sha256_hex


def rebuild_plan_from_manifest(manifest: dict[str, Any]) -> tuple[TrainingPlan, ShardPolicy]:
    content = manifest["content"]
    if not isinstance(content, dict):
        fail("MANIFEST_INVALID", "清单 content 必须是对象")
    raw_plan = {
        "seed": content.get("seed"),
        "layers": content.get("layers"),
        "save": {
            "version": manifest.get("version"),
            "logical_clock_ms": 0,
            "max_concurrency": 1,
            "logical_time_budget_ms": 1,
            "incremental": False,
            "logical_clock_tolerance_ms": 0,
        },
    }
    plan = validate_training_plan(raw_plan)
    policy = validate_shard_policy(content.get("shard_policy", {}), plan)
    expected = sha256_hex(canonical_json_bytes(content))
    if expected != manifest.get("content_digest"):
        fail(
            "MANIFEST_INVALID",
            "清单 content_digest 不匹配",
            expected_digest=expected,
            actual_digest=manifest.get("content_digest"),
        )
    return plan, policy


def validate_manifest_ordering(
    manifest: dict[str, Any], policy: ShardPolicy
) -> list[dict[str, Any]]:
    shards = manifest["shards"]
    if not all(isinstance(item, dict) for item in shards):
        fail("MANIFEST_INVALID", "清单分片条目必须是对象")
    ids = [item.get("shard_id") for item in shards]
    expected_ids = [f"shard-{index:04d}" for index in range(policy.num_shards)]
    if ids != expected_ids:
        fail(
            "SHARD_ORDER_MISMATCH",
            "清单分片顺序、数量或 ID 不符合稳定拼接顺序",
            actual=ids,
            expected=expected_ids,
        )
    for shard in shards:
        names = shard.get("layers")
        if not isinstance(names, list) or names != sorted(names):
            fail(
                "SHARD_ORDER_MISMATCH",
                "清单中分片内层顺序必须稳定排序",
                shard_id=shard.get("shard_id"),
                layers=names,
            )
        expected_path = f"objects/{shard.get('digest')}.shard"
        if shard.get("path") != expected_path:
            fail(
                "MANIFEST_INVALID",
                "分片路径不是内容寻址路径",
                shard_id=shard.get("shard_id"),
                path=shard.get("path"),
                expected_path=expected_path,
            )

    layers = manifest["layers"]
    if not all(isinstance(item, dict) for item in layers):
        fail("MANIFEST_INVALID", "清单层条目必须是对象")
    names = [item.get("name") for item in layers]
    if names != sorted(names) or len(set(names)) != len(names):
        fail(
            "MANIFEST_INVALID",
            "清单层顺序必须按层名稳定排序且不重复",
            names=names,
        )
    for layer in layers:
        expected_shard = _layer_shard(layer["name"], policy.num_shards)
        if layer.get("shard_id") != expected_shard:
            fail(
                "SHARD_ORDER_MISMATCH",
                "清单层分片归属与参数名哈希不一致",
                layer=layer.get("name"),
                expected_shard=expected_shard,
                actual_shard=layer.get("shard_id"),
            )
        dtype = layer.get("dtype")
        if dtype not in DTYPES:
            fail("MANIFEST_INVALID", "清单层 dtype 非法", layer=layer.get("name"))
        shape = layer.get("shape")
        if (
            not isinstance(shape, list)
            or not shape
            or not all(isinstance(dim, int) and not isinstance(dim, bool) and dim > 0 for dim in shape)
        ):
            fail("MANIFEST_INVALID", "清单层形状非法", layer=layer.get("name"))
    return shards


def _layer_shard(name: str, num_shards: int) -> str:
    index = int(sha256_hex(name.encode("utf-8")), 16) % num_shards
    return f"shard-{index:04d}"


def reconstruct_manifest(
    manifest: dict[str, Any], store: ObjectStore
) -> tuple[dict[str, Any], dict[str, dict[str, np.ndarray]]]:
    validate_manifest_shape(manifest)
    plan, policy = rebuild_plan_from_manifest(manifest)
    manifest_shards = validate_manifest_ordering(manifest, policy)
    expected: CheckpointArtifacts = build_checkpoint_artifacts(plan, policy)

    expected_shards = expected.shard_map()
    expected_layers = expected.layer_map()
    manifest_layers = {
        layer["name"]: layer for layer in manifest["layers"]
    }
    if set(manifest_layers) != set(expected_layers):
        fail(
            "LAYER_MISSING",
            "清单层集合与内容重建层集合不一致",
            missing=sorted(set(expected_layers) - set(manifest_layers)),
            extra=sorted(set(manifest_layers) - set(expected_layers)),
        )
    for name, manifest_layer in manifest_layers.items():
        expected_layer = expected_layers[name]
        if manifest_layer.get("param_digest") != expected_layer.param_digest:
            fail(
                "PARAMETER_DIGEST_MISMATCH",
                "清单参数摘要与逐层独立重建不一致",
                layer=name,
                actual_digest=manifest_layer.get("param_digest"),
                expected_digest=expected_layer.param_digest,
            )
        if manifest_layer.get("optimizer_digest") != expected_layer.optimizer_digest:
            fail(
                "OPTIMIZER_DIGEST_MISMATCH",
                "清单优化器摘要与逐层独立重建不一致",
                layer=name,
                actual_digest=manifest_layer.get("optimizer_digest"),
                expected_digest=expected_layer.optimizer_digest,
            )
        if manifest_layer.get("layer_digest") != expected_layer.layer_digest:
            fail(
                "CONSISTENCY_MISMATCH",
                "清单逐层摘要与独立重建不一致",
                layer=name,
            )
        if manifest_layer.get("shape") != list(expected_layer.spec.shape):
            fail(
                "LAYER_SHAPE_MISMATCH",
                "清单层形状与独立重建不一致",
                layer=name,
                actual_shape=manifest_layer.get("shape"),
                expected_shape=list(expected_layer.spec.shape),
            )
        if manifest_layer.get("dtype") != expected_layer.spec.dtype:
            fail(
                "CONSISTENCY_MISMATCH",
                "清单层 dtype 与独立重建不一致",
                layer=name,
            )
        if manifest_layer.get("shard_id") != expected_layer.shard_id:
            fail(
                "SHARD_ORDER_MISMATCH",
                "清单层分片归属与独立重建不一致",
                layer=name,
            )

    reconstructed: dict[str, dict[str, np.ndarray]] = {}
    layer_chain: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    for manifest_shard in manifest_shards:
        shard_id = manifest_shard["shard_id"]
        expected_shard = expected_shards[shard_id]
        if manifest_shard.get("size") != expected_shard.size:
            fail(
                "CONSISTENCY_MISMATCH",
                "清单分片大小与独立重建不一致",
                shard_id=shard_id,
                actual=manifest_shard.get("size"),
                expected=expected_shard.size,
            )
        if manifest_shard.get("logical_cost_ms") != expected_shard.logical_cost_ms:
            fail(
                "CONSISTENCY_MISMATCH",
                "清单分片逻辑成本与独立重建不一致",
                shard_id=shard_id,
            )
        manifest_digest = manifest_shard.get("digest")
        if not isinstance(manifest_digest, str):
            fail("MANIFEST_INVALID", "分片摘要字段非法", shard_id=shard_id)
        if manifest_digest != expected_shard.digest:
            fail(
                "CONSISTENCY_MISMATCH",
                "清单分片摘要与逐层独立重建结果不一致",
                shard_id=shard_id,
                manifest_digest=manifest_digest,
                rebuilt_digest=expected_shard.digest,
            )
        data = store.read_object(manifest_digest)
        actual_digest = sha256_hex(data)
        if actual_digest != manifest_digest:
            fail(
                "SHARD_DIGEST_MISMATCH",
                "分片摘要不匹配",
                shard_id=shard_id,
                expected_digest=manifest_digest,
                actual_digest=actual_digest,
            )
        header, params, optimizers = parse_shard(
            data, expected_shard_id=shard_id
        )
        if header.get("shard_index") != expected_shard.shard_index:
            fail(
                "SHARD_ORDER_MISMATCH",
                "分片索引与稳定拼接顺序不一致",
                shard_id=shard_id,
                actual=header.get("shard_index"),
                expected=expected_shard.shard_index,
            )
        actual_names = [entry["name"] for entry in header.get("entries", [])]
        expected_names = [layer.spec.name for layer in expected_shard.layers]
        if actual_names != expected_names:
            fail(
                "SHARD_ORDER_MISMATCH",
                "分片内层拼接顺序与独立重建不一致",
                shard_id=shard_id,
                actual=actual_names,
                expected=expected_names,
            )
        if set(manifest_shard.get("layers", [])) != set(expected_names):
            fail(
                "SHARD_ORDER_MISMATCH",
                "清单分片层集合与对象内容不一致",
                shard_id=shard_id,
            )

        for entry, expected_layer in zip(header["entries"], expected_shard.layers):
            name = expected_layer.spec.name
            if entry.get("shape") != list(expected_layer.spec.shape):
                fail(
                    "LAYER_SHAPE_MISMATCH",
                    "分片头层形状与独立重建不一致",
                    layer=name,
                    actual_shape=entry.get("shape"),
                    expected_shape=list(expected_layer.spec.shape),
                )
            if entry.get("dtype") != expected_layer.spec.dtype:
                fail(
                    "CONSISTENCY_MISMATCH",
                    "分片头层 dtype 与独立重建不一致",
                    layer=name,
                )
            param_bytes = params[name]
            param_digest = sha256_hex(param_bytes)
            if param_digest != expected_layer.param_digest:
                fail(
                    "PARAMETER_DIGEST_MISMATCH",
                    "参数摘要不匹配",
                    layer=name,
                    shard_id=shard_id,
                    actual_digest=param_digest,
                    expected_digest=expected_layer.param_digest,
                )
            if param_bytes != expected_layer.param_bytes:
                fail(
                    "CONSISTENCY_MISMATCH",
                    "拼接参数与逐层独立重建字节不一致",
                    layer=name,
                    shard_id=shard_id,
                )
            optimizer_bytes = optimizers.get(name, b"")
            optimizer_digest = (
                sha256_hex(optimizer_bytes) if expected_layer.optimizer_digest else None
            )
            if optimizer_digest != expected_layer.optimizer_digest:
                fail(
                    "OPTIMIZER_DIGEST_MISMATCH",
                    "优化器状态摘要不匹配",
                    layer=name,
                    shard_id=shard_id,
                    actual_digest=optimizer_digest,
                    expected_digest=expected_layer.optimizer_digest,
                )
            if optimizer_bytes != expected_layer.optimizer_bytes:
                fail(
                    "CONSISTENCY_MISMATCH",
                    "拼接优化器状态与逐层独立重建字节不一致",
                    layer=name,
                    shard_id=shard_id,
                )
            identity = {
                "name": name,
                "shape": list(expected_layer.spec.shape),
                "dtype": expected_layer.spec.dtype,
                "has_optimizer_state": expected_layer.spec.has_optimizer_state,
                "revision": expected_layer.spec.revision,
                "param_digest": param_digest,
                "optimizer_digest": optimizer_digest,
            }
            layer_digest = sha256_hex(canonical_json_bytes(identity))
            if layer_digest != expected_layer.layer_digest:
                fail(
                    "CONSISTENCY_MISMATCH",
                    "逐层摘要链与参数摘要不一致",
                    layer=name,
                    actual_digest=layer_digest,
                    expected_digest=expected_layer.layer_digest,
                )

            dtype = np.dtype(expected_layer.spec.dtype).newbyteorder("<")
            array = np.frombuffer(param_bytes, dtype=dtype).reshape(
                expected_layer.spec.shape
            )
            optimizer_arrays: dict[str, np.ndarray] = {}
            if optimizer_bytes:
                half = len(optimizer_bytes) // 2
                optimizer_arrays["exp_avg"] = np.frombuffer(
                    optimizer_bytes[:half], dtype=np.dtype("<f4")
                ).reshape(expected_layer.spec.shape)
                optimizer_arrays["exp_avg_sq"] = np.frombuffer(
                    optimizer_bytes[half:], dtype=np.dtype("<f4")
                ).reshape(expected_layer.spec.shape)
            reconstructed[name] = {
                "parameter": array,
                **optimizer_arrays,
            }
            layer_chain.append(
                {
                    "name": name,
                    "shard_id": shard_id,
                    "shape": list(expected_layer.spec.shape),
                    "dtype": expected_layer.spec.dtype,
                    "param_digest": param_digest,
                    "optimizer_digest": optimizer_digest,
                    "layer_digest": layer_digest,
                }
            )
        checks.append(
            {
                "shard_id": shard_id,
                "digest": manifest_digest,
                "status": "verified",
                "layers": actual_names,
            }
        )

    if set(reconstructed) != set(expected_layers):
        fail(
            "LAYER_MISSING",
            "分片拼接后缺少层",
            missing=sorted(set(expected_layers) - set(reconstructed)),
        )
    layer_chain.sort(key=lambda item: item["name"])
    chain_digest = sha256_hex(
        canonical_json_bytes(
            {
                "version": manifest["version"],
                "content_digest": manifest["content_digest"],
                "layers": layer_chain,
            }
        )
    )
    result = {
        "version": manifest["version"],
        "content_digest": manifest["content_digest"],
        "shard_count": len(manifest_shards),
        "layer_count": len(layer_chain),
        "layers": layer_chain,
        "chain_digest": chain_digest,
        "checks": checks,
        "reconstruction": {
            "primary": "manifest_order_shard_concat",
            "control": "per_layer_independent_regeneration",
            "agreement": True,
        },
    }
    return result, reconstructed
