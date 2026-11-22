from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import BusinessError, fail
from .util import canonical_json_bytes, is_int, sha256_hex


DTYPES = {
    "float16",
    "float32",
    "float64",
    "int8",
    "int16",
    "int32",
    "int64",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
}

HASH_ALGORITHMS = {"sha256"}
CRASH_POINTS = {
    "none",
    "after_shard_commit",
    "after_all_shards",
    "during_manifest_write",
    "before_manifest_commit",
    "after_manifest_file",
}


@dataclass(frozen=True)
class LayerSpec:
    name: str
    shape: tuple[int, ...]
    dtype: str
    has_optimizer_state: bool
    revision: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "shape": list(self.shape),
            "dtype": self.dtype,
            "has_optimizer_state": self.has_optimizer_state,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class SavePlan:
    version: int
    logical_clock_ms: int
    max_concurrency: int
    logical_time_budget_ms: int
    incremental: bool
    logical_clock_tolerance_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "logical_clock_ms": self.logical_clock_ms,
            "max_concurrency": self.max_concurrency,
            "logical_time_budget_ms": self.logical_time_budget_ms,
            "incremental": self.incremental,
            "logical_clock_tolerance_ms": self.logical_clock_tolerance_ms,
        }


@dataclass(frozen=True)
class TrainingPlan:
    seed: int
    layers: tuple[LayerSpec, ...]
    save: SavePlan

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "layers": [layer.as_dict() for layer in self.layers],
            "save": self.save.as_dict(),
        }


@dataclass(frozen=True)
class ShardPolicy:
    num_shards: int
    max_shard_bytes: int
    hash_algorithm: str = "sha256"

    def as_dict(self) -> dict[str, Any]:
        return {
            "num_shards": self.num_shards,
            "max_shard_bytes": self.max_shard_bytes,
            "hash_algorithm": self.hash_algorithm,
        }


@dataclass(frozen=True)
class CrashPlan:
    enabled: bool = False
    point: str = "none"
    after_shards: int = 0
    exit_code: int = 91
    fail_shards: tuple[str, ...] = ()
    cancel_after_started: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "point": self.point,
            "after_shards": self.after_shards,
            "exit_code": self.exit_code,
            "fail_shards": list(self.fail_shards),
            "cancel_after_started": self.cancel_after_started,
        }


def validate_training_plan(data: dict[str, Any]) -> TrainingPlan:
    seed = data.get("seed")
    if not is_int(seed) or seed < 0:
        fail("TRAINING_PLAN_INVALID", "seed 必须是非负整数", field="seed")

    raw_layers = data.get("layers")
    if not isinstance(raw_layers, list) or not raw_layers:
        fail("TRAINING_PLAN_INVALID", "layers 必须是非空数组", field="layers")

    seen: set[str] = set()
    layers: list[LayerSpec] = []
    for index, raw in enumerate(raw_layers):
        if not isinstance(raw, dict):
            fail("TRAINING_PLAN_INVALID", "层条目必须是对象", index=index)
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            fail("LAYER_NAME_DUPLICATE", "层名必须是非空字符串", index=index)
        if name in seen:
            fail("LAYER_NAME_DUPLICATE", "层名必须唯一", name=name, index=index)
        seen.add(name)

        shape = raw.get("shape")
        if not isinstance(shape, list) or not shape:
            fail("LAYER_SHAPE_INVALID", "shape 必须是非空正整数数组", name=name)
        normalized_shape: list[int] = []
        for dim_index, dim in enumerate(shape):
            if not is_int(dim) or dim <= 0:
                fail(
                    "LAYER_SHAPE_INVALID",
                    "shape 的每一维必须是正整数",
                    name=name,
                    dimension=dim_index,
                    value=dim,
                )
            normalized_shape.append(dim)

        dtype = raw.get("dtype")
        if dtype not in DTYPES:
            fail(
                "DTYPE_INVALID",
                "dtype 不在允许枚举内",
                name=name,
                value=dtype,
                allowed=sorted(DTYPES),
            )

        has_optimizer_state = raw.get("has_optimizer_state")
        if not isinstance(has_optimizer_state, bool):
            fail(
                "LAYER_OPTIMIZER_INVALID",
                "has_optimizer_state 必须是布尔值",
                name=name,
            )

        revision = raw.get("revision", 0)
        if not is_int(revision) or revision < 0:
            fail("LAYER_REVISION_INVALID", "revision 必须是非负整数", name=name)

        layers.append(
            LayerSpec(
                name=name,
                shape=tuple(normalized_shape),
                dtype=dtype,
                has_optimizer_state=has_optimizer_state,
                revision=revision,
            )
        )

    raw_save = data.get("save")
    if not isinstance(raw_save, dict):
        fail("SAVE_PLAN_INVALID", "save 必须是对象", field="save")
    version = raw_save.get("version")
    if not is_int(version) or version <= 0:
        fail("SAVE_VERSION_INVALID", "version 必须是正整数", value=version)
    logical_clock_ms = raw_save.get("logical_clock_ms")
    if not is_int(logical_clock_ms) or logical_clock_ms < 0:
        fail(
            "SAVE_PLAN_INVALID",
            "logical_clock_ms 必须是非负整数",
            value=logical_clock_ms,
        )
    max_concurrency = raw_save.get("max_concurrency")
    if not is_int(max_concurrency) or max_concurrency <= 0:
        fail(
            "CONCURRENCY_INVALID",
            "max_concurrency 必须是正整数",
            value=max_concurrency,
        )
    budget = raw_save.get("logical_time_budget_ms")
    if not is_int(budget) or budget <= 0:
        fail("BUDGET_INVALID", "logical_time_budget_ms 必须是正整数", value=budget)
    incremental = raw_save.get("incremental")
    if not isinstance(incremental, bool):
        fail("SAVE_PLAN_INVALID", "incremental 必须是布尔值", value=incremental)
    tolerance = raw_save.get("logical_clock_tolerance_ms", 0)
    if not is_int(tolerance) or tolerance < 0:
        fail(
            "SAVE_PLAN_INVALID",
            "logical_clock_tolerance_ms 必须是非负整数",
            value=tolerance,
        )

    ordered_layers = tuple(sorted(layers, key=lambda item: item.name))
    return TrainingPlan(
        seed=seed,
        layers=ordered_layers,
        save=SavePlan(
            version=version,
            logical_clock_ms=logical_clock_ms,
            max_concurrency=max_concurrency,
            logical_time_budget_ms=budget,
            incremental=incremental,
            logical_clock_tolerance_ms=tolerance,
        ),
    )


def validate_shard_policy(data: dict[str, Any], plan: TrainingPlan) -> ShardPolicy:
    num_shards = data.get("num_shards")
    if not is_int(num_shards) or num_shards <= 0:
        fail("SHARD_COUNT_INVALID", "num_shards 必须是正整数", value=num_shards)
    if num_shards > len(plan.layers):
        fail(
            "SHARD_COUNT_INVALID",
            "分片数不能超过层总数",
            num_shards=num_shards,
            layer_count=len(plan.layers),
        )
    max_shard_bytes = data.get("max_shard_bytes")
    if not is_int(max_shard_bytes) or max_shard_bytes <= 0:
        fail(
            "SHARD_LIMIT_INVALID",
            "max_shard_bytes 必须是正整数",
            value=max_shard_bytes,
        )
    hash_algorithm = data.get("hash_algorithm", "sha256")
    if hash_algorithm not in HASH_ALGORITHMS:
        fail(
            "SHARD_HASH_INVALID",
            "不支持的分片哈希算法",
            value=hash_algorithm,
            allowed=sorted(HASH_ALGORITHMS),
        )
    return ShardPolicy(
        num_shards=num_shards,
        max_shard_bytes=max_shard_bytes,
        hash_algorithm=hash_algorithm,
    )


def validate_crash_plan(data: dict[str, Any] | None) -> CrashPlan:
    if data is None:
        return CrashPlan()
    enabled = data.get("enabled", False)
    point = data.get("point", "none")
    after_shards = data.get("after_shards", 0)
    exit_code = data.get("exit_code", 91)
    fail_shards = data.get("fail_shards", [])
    cancel_after_started = data.get("cancel_after_started")

    if not isinstance(enabled, bool):
        fail("CRASH_PLAN_INVALID", "enabled 必须是布尔值")
    if point not in CRASH_POINTS:
        fail(
            "CRASH_PLAN_INVALID",
            "未知崩溃注入点",
            value=point,
            allowed=sorted(CRASH_POINTS),
        )
    if not is_int(after_shards) or after_shards < 0:
        fail("CRASH_PLAN_INVALID", "after_shards 必须是非负整数")
    if not is_int(exit_code) or exit_code <= 0 or exit_code > 255:
        fail("CRASH_PLAN_INVALID", "exit_code 必须在 1..255")
    if not isinstance(fail_shards, list) or not all(
        isinstance(item, str) for item in fail_shards
    ):
        fail("CRASH_PLAN_INVALID", "fail_shards 必须是字符串数组")
    if cancel_after_started is not None and (
        not is_int(cancel_after_started) or cancel_after_started < 0
    ):
        fail("CRASH_PLAN_INVALID", "cancel_after_started 必须是非负整数或 null")

    if not enabled and point != "none":
        point = "none"
    return CrashPlan(
        enabled=enabled,
        point=point,
        after_shards=after_shards,
        exit_code=exit_code,
        fail_shards=tuple(sorted(fail_shards)),
        cancel_after_started=cancel_after_started,
    )


def shard_id_for(layer_name: str, num_shards: int) -> str:
    index = int(sha256_hex(layer_name.encode("utf-8")), 16) % num_shards
    return f"shard-{index:04d}"


def content_document(plan: TrainingPlan, policy: ShardPolicy) -> dict[str, Any]:
    return {
        "seed": plan.seed,
        "layers": [layer.as_dict() for layer in sorted(plan.layers, key=lambda x: x.name)],
        "shard_policy": policy.as_dict(),
    }


def content_digest(plan: TrainingPlan, policy: ShardPolicy) -> str:
    return sha256_hex(canonical_json_bytes(content_document(plan, policy)))


def layer_map(plan: TrainingPlan) -> dict[str, LayerSpec]:
    return {layer.name: layer for layer in plan.layers}


def validate_manifest_shape(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        fail("MANIFEST_INVALID", "清单根必须是对象")
    required = {"format", "version", "content_digest", "content", "shards", "layers"}
    missing = sorted(required - set(manifest))
    if missing:
        fail("MANIFEST_INVALID", "清单缺少字段", missing=missing)
    if manifest["format"] != "ckpt-manifest/v1":
        fail("MANIFEST_INVALID", "清单格式版本不受支持", value=manifest["format"])
    if not is_int(manifest["version"]) or manifest["version"] <= 0:
        fail("MANIFEST_INVALID", "清单版本号非法")
    content = manifest["content"]
    if not isinstance(content, dict):
        fail("MANIFEST_INVALID", "content 必须是对象")
    if sha256_hex(canonical_json_bytes(content)) != manifest["content_digest"]:
        fail("MANIFEST_INVALID", "清单 content_digest 与内容不一致")
    if not isinstance(manifest["shards"], list) or not manifest["shards"]:
        fail("MANIFEST_INVALID", "shards 必须是非空数组")
    if not isinstance(manifest["layers"], list) or not manifest["layers"]:
        fail("MANIFEST_INVALID", "layers 必须是非空数组")
    return manifest
