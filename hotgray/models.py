from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import BizError, ErrorCode, fail
from .util import (
    float_ratio,
    int_field,
    load_json,
    require_digest,
    resolve_path,
    sha256_json,
)

RESOURCE_KINDS = {
    "preprocess",
    "postprocess",
    "feature_table",
    "upstream_model",
}

MODEL_REFERENCE_KINDS = {
    "preprocess": "preprocess",
    "postprocess": "postprocess",
    "feature_table": "feature_table",
    "upstream_model": "upstream_model",
}

WARMUP_STATES = {
    "pending",
    "loading",
    "prewarmed",
    "gray",
    "rolling_back",
    "rolled_back",
    "rollback_not_achieved",
}


@dataclass(frozen=True)
class ShardSpec:
    index: int
    byte_start: int
    byte_end: int
    digest: str
    path: str | None = None

    @property
    def size(self) -> int:
        return self.byte_end - self.byte_start

    def as_dict(self) -> dict[str, Any]:
        return {
            "shard_index": self.index,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "digest": self.digest,
            "path": self.path,
        }


@dataclass(frozen=True)
class LayerSpec:
    name: str
    param_count: int
    shards: tuple[ShardSpec, ...]

    @property
    def expected_bytes(self) -> int:
        return self.param_count * 4

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "param_count": self.param_count,
            "shards": [shard.as_dict() for shard in self.shards],
        }


@dataclass(frozen=True)
class ResourceSpec:
    resource_id: str
    kind: str
    revision: str
    digest: str
    depends_on: tuple[str, ...]
    params: Mapping[str, Any]
    raw: Mapping[str, Any]

    def snapshot(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "revision": self.revision,
            "digest": self.digest,
            "depends_on": list(self.depends_on),
            "params": dict(self.params),
        }


@dataclass(frozen=True)
class ModelSpec:
    version: int
    layers: tuple[LayerSpec, ...]
    preprocess: str
    postprocess: str
    feature_table: str
    upstream_model: str
    raw: Mapping[str, Any]

    def layer(self, name: str) -> LayerSpec:
        for layer in self.layers:
            if layer.name == name:
                return layer
        fail(
            ErrorCode.MISSING_LAYER_REFERENCE,
            f"版本 {self.version} 中不存在层 {name}",
            version=self.version,
            layer=name,
        )

    def shard(self, layer: str, shard_index: int) -> ShardSpec:
        layer_spec = self.layer(layer)
        if shard_index < 0 or shard_index >= len(layer_spec.shards):
            fail(
                ErrorCode.MISSING_SHARD,
                f"版本 {self.version} 的层 {layer} 不存在分片 {shard_index}",
                version=self.version,
                layer=layer,
                shard_index=shard_index,
            )
        return layer_spec.shards[shard_index]

    def shard_chain(self) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        for layer in self.layers:
            for shard in layer.shards:
                chain.append(
                    {
                        "layer": layer.name,
                        "shard_index": shard.index,
                        "byte_start": shard.byte_start,
                        "byte_end": shard.byte_end,
                        "digest": shard.digest,
                    }
                )
        return chain

    def references(self) -> dict[str, str]:
        return {
            "preprocess": self.preprocess,
            "postprocess": self.postprocess,
            "feature_table": self.feature_table,
            "upstream_model": self.upstream_model,
        }


@dataclass(frozen=True)
class Manifest:
    samples_root: str
    resources: Mapping[str, ResourceSpec]
    models: Mapping[int, ModelSpec]
    raw: Mapping[str, Any]
    digest: str

    def model(self, version: int) -> ModelSpec:
        try:
            return self.models[version]
        except KeyError:
            fail(
                ErrorCode.UNRESOLVED_REFERENCE,
                f"模型版本 {version} 不存在",
                version=version,
            )

    def resource_closure(
        self,
        resource_id: str,
        *,
        include_self: bool = True,
        seen: set[str] | None = None,
    ) -> list[str]:
        if seen is None:
            seen = set()
        if resource_id in seen:
            return []
        seen.add(resource_id)
        result = [resource_id] if include_self else []
        for dependency in self.resources[resource_id].depends_on:
            result.extend(
                self.resource_closure(dependency, include_self=True, seen=seen)
            )
        return result

    def version_resource_snapshots(self, version: int) -> dict[str, Any]:
        model = self.model(version)
        snapshot: dict[str, Any] = {}
        for root in model.references().values():
            for resource_id in self.resource_closure(root):
                if resource_id not in snapshot:
                    snapshot[resource_id] = self.resources[resource_id].snapshot()
        return snapshot

    def version_binding(self, version: int) -> dict[str, Any]:
        model = self.model(version)
        resources = self.version_resource_snapshots(version)
        binding = {
            "version": version,
            "shard_chain": model.shard_chain(),
            "resources": resources,
            "references": model.references(),
        }
        binding["binding_digest"] = sha256_json(binding)
        return binding


@dataclass(frozen=True)
class ReplicaSpec:
    replica_id: str
    loaded_version: int
    loaded_shards: Mapping[tuple[str, int], str]
    warmup_state: str
    gray_ratio: float
    raw: Mapping[str, Any]

    def shard_chain(self) -> list[dict[str, Any]]:
        return [
            {
                "layer": layer,
                "shard_index": shard_index,
                "digest": digest,
            }
            for (layer, shard_index), digest in sorted(self.loaded_shards.items())
        ]


@dataclass(frozen=True)
class GrayStage:
    level: int
    ratio: float
    at_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "ratio": self.ratio, "at_ms": self.at_ms}


@dataclass(frozen=True)
class UpdatePlan:
    round_id: str
    baseline_version: int
    target_version: int
    patch_id: str
    patch_path: str
    replica_ids: tuple[str, ...]
    start_at_ms: int
    warmup_duration_ms: int
    warmup_timeout_ms: int
    rollback_window_ms: int
    rollback_step_ms: int
    clock_tolerance_ms: int
    stages: tuple[GrayStage, ...]
    raw: Mapping[str, Any]

    def stage(self, level: int) -> GrayStage:
        for stage in self.stages:
            if stage.level == level:
                return stage
        fail(
            ErrorCode.INVALID_GRAY_RATIO,
            f"灰度档位 {level} 不存在",
            actual=level,
        )

    @property
    def max_level(self) -> int:
        return len(self.stages)


@dataclass(frozen=True)
class PatchEntry:
    layer: str
    shard_index: int
    byte_start: int
    byte_end: int
    digest: str
    blob_path: str

    @property
    def key(self) -> tuple[str, int]:
        return (self.layer, self.shard_index)


@dataclass(frozen=True)
class PatchSpec:
    patch_id: str
    baseline_version: int
    target_version: int
    entries: tuple[PatchEntry, ...]
    raw: Mapping[str, Any]

    @property
    def by_key(self) -> dict[tuple[str, int], PatchEntry]:
        return {entry.key: entry for entry in self.entries}


@dataclass(frozen=True)
class ClockAction:
    at_ms: int
    command: str
    args: Mapping[str, Any] = field(default_factory=dict)
    scenario: str = "default"


@dataclass
class Samples:
    root: Path
    manifest: Manifest
    replicas: tuple[ReplicaSpec, ...]
    plan: UpdatePlan
    patch: PatchSpec
    clock_scenarios: Mapping[str, tuple[ClockAction, ...]]

    def replica(self, replica_id: str) -> ReplicaSpec:
        for replica in self.replicas:
            if replica.replica_id == replica_id:
                return replica
        fail(
            ErrorCode.NOT_FOUND,
            f"副本 {replica_id} 不存在",
            replica_id=replica_id,
        )

    def clock_actions(self, scenario: str = "default") -> tuple[ClockAction, ...]:
        try:
            return self.clock_scenarios[scenario]
        except KeyError:
            fail(
                ErrorCode.NOT_FOUND,
                f"逻辑时钟场景 {scenario!r} 不存在",
                actual=scenario,
                available=sorted(self.clock_scenarios),
            )


def _as_list(value: Any, name: str, **details: Any) -> list[Any]:
    if not isinstance(value, list):
        fail(ErrorCode.INVALID_INPUT, f"{name} 必须为数组", actual=type(value).__name__, **details)
    return value


def _as_dict(value: Any, name: str, **details: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(ErrorCode.INVALID_INPUT, f"{name} 必须为对象", actual=type(value).__name__, **details)
    return value


def _as_str(value: Any, name: str, *, nonempty: bool = True, **details: Any) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        fail(ErrorCode.INVALID_INPUT, f"{name} 必须为非空字符串", actual=value, **details)
    return value


def _parse_resource(raw_value: Any, index: int) -> ResourceSpec:
    raw = _as_dict(raw_value, f"resources[{index}]")
    resource_id = _as_str(raw.get("id"), f"resources[{index}].id")
    kind = _as_str(raw.get("kind"), f"resources[{index}].kind", resource_id=resource_id)
    if kind not in RESOURCE_KINDS:
        fail(
            ErrorCode.INVALID_INPUT,
            f"资源 {resource_id} 的 kind 非法: {kind}",
            resource_id=resource_id,
            actual=kind,
        )
    revision = _as_str(raw.get("revision"), f"资源 {resource_id}.revision", resource_id=resource_id)
    digest = require_digest(raw.get("digest"), field=f"资源 {resource_id}.digest", resource_id=resource_id)
    depends_raw = raw.get("depends_on", [])
    depends = _as_list(depends_raw, f"资源 {resource_id}.depends_on", resource_id=resource_id)
    dependencies: list[str] = []
    for dependency in depends:
        dependency_id = _as_str(dependency, f"资源 {resource_id}.depends_on[]", resource_id=resource_id)
        if dependency_id in dependencies:
            fail(
                ErrorCode.INVALID_INPUT,
                f"资源 {resource_id} 重复声明依赖 {dependency_id}",
                resource_id=resource_id,
                actual=dependency_id,
            )
        dependencies.append(dependency_id)
    params = raw.get("params", {})
    _as_dict(params, f"资源 {resource_id}.params", resource_id=resource_id)
    return ResourceSpec(
        resource_id=resource_id,
        kind=kind,
        revision=revision,
        digest=digest,
        depends_on=tuple(dependencies),
        params=dict(params),
        raw=dict(raw),
    )


def _parse_shard(
    raw_value: Any,
    *,
    version: int,
    layer: str,
    ordinal: int,
) -> ShardSpec:
    raw = _as_dict(raw_value, f"版本 {version} 层 {layer} shards[{ordinal}]")
    index = int_field(
        raw.get("shard_index", ordinal),
        "shard_index",
        minimum=0,
        version=version,
        layer=layer,
    )
    start = int_field(
        raw.get("byte_start"),
        "byte_start",
        minimum=0,
        version=version,
        layer=layer,
        shard_index=index,
    )
    end = int_field(
        raw.get("byte_end"),
        "byte_end",
        minimum=0,
        version=version,
        layer=layer,
        shard_index=index,
    )
    if end <= start:
        fail(
            ErrorCode.INVALID_INPUT,
            f"版本 {version} 层 {layer} 分片 {index} 的字节范围非法",
            version=version,
            layer=layer,
            shard_index=index,
            actual=[start, end],
        )
    digest = require_digest(
        raw.get("digest"),
        field=f"版本 {version} 层 {layer} 分片 {index}.digest",
        version=version,
        layer=layer,
        shard_index=index,
    )
    path = raw.get("path")
    if path is not None:
        path = _as_str(
            path,
            f"版本 {version} 层 {layer} 分片 {index}.path",
            version=version,
            layer=layer,
            shard_index=index,
        )
    return ShardSpec(index=index, byte_start=start, byte_end=end, digest=digest, path=path)


def _parse_layer(raw_value: Any, *, version: int, ordinal: int) -> LayerSpec:
    raw = _as_dict(raw_value, f"版本 {version} layers[{ordinal}]")
    name = _as_str(raw.get("name"), f"版本 {version} layers[{ordinal}].name", version=version)
    try:
        param_count = int_field(
            raw.get("param_count"),
            "param_count",
            minimum=1,
            version=version,
            layer=name,
        )
    except BizError as exc:
        raise BizError(
            ErrorCode.INVALID_PARAM_COUNT,
            f"版本 {version} 层 {name} 的 param_count 必须为正整数",
            {"version": version, "layer": name, "actual": raw.get("param_count")},
        ) from exc
    shards_raw = _as_list(raw.get("shards"), f"版本 {version} 层 {name}.shards", version=version, layer=name)
    if not shards_raw:
        fail(
            ErrorCode.MISSING_SHARD,
            f"版本 {version} 层 {name} 没有任何分片",
            version=version,
            layer=name,
        )
    shards = [
        _parse_shard(item, version=version, layer=name, ordinal=index)
        for index, item in enumerate(shards_raw)
    ]
    indices = [shard.index for shard in shards]
    if len(set(indices)) != len(indices):
        fail(
            ErrorCode.INVALID_INPUT,
            f"版本 {version} 层 {name} 存在重复分片序号",
            version=version,
            layer=name,
        )
    if set(indices) != set(range(len(shards))):
        missing = sorted(set(range(len(shards))) - set(indices))
        fail(
            ErrorCode.MISSING_SHARD,
            f"版本 {version} 层 {name} 分片序号不连续",
            version=version,
            layer=name,
            shard_index=missing[0] if missing else None,
        )
    ordered = sorted(shards, key=lambda shard: shard.index)
    cursor = 0
    for shard in ordered:
        if shard.byte_start != cursor:
            fail(
                ErrorCode.MISSING_SHARD,
                f"版本 {version} 层 {name} 分片 {shard.index} 未连续覆盖字节区间",
                version=version,
                layer=name,
                shard_index=shard.index,
                expected=cursor,
                actual=shard.byte_start,
            )
        cursor = shard.byte_end
    expected_bytes = param_count * 4
    if cursor != expected_bytes:
        fail(
            ErrorCode.MISSING_SHARD,
            f"版本 {version} 层 {name} 分片未完整覆盖参数字节数",
            version=version,
            layer=name,
            expected=expected_bytes,
            actual=cursor,
        )
    return LayerSpec(name=name, param_count=param_count, shards=tuple(ordered))


def _parse_model(raw_value: Any, index: int) -> ModelSpec:
    raw = _as_dict(raw_value, f"models[{index}]")
    version = int_field(raw.get("version"), "models.version", minimum=1)
    layers_raw = _as_list(raw.get("layers"), f"版本 {version}.layers", version=version)
    if not layers_raw:
        fail(ErrorCode.MISSING_LAYER_REFERENCE, "模型版本必须至少包含一层", version=version)
    layers = [_parse_layer(item, version=version, ordinal=position) for position, item in enumerate(layers_raw)]
    names = [layer.name for layer in layers]
    if len(set(names)) != len(names):
        duplicate = next(name for name in names if names.count(name) > 1)
        fail(
            ErrorCode.DUPLICATE_LAYER,
            f"版本 {version} 存在重复层名 {duplicate}",
            version=version,
            layer=duplicate,
        )
    references: dict[str, str] = {}
    for field_name in MODEL_REFERENCE_KINDS:
        references[field_name] = _as_str(
            raw.get(field_name),
            f"版本 {version}.{field_name}",
            version=version,
        )
    return ModelSpec(
        version=version,
        layers=tuple(layers),
        preprocess=references["preprocess"],
        postprocess=references["postprocess"],
        feature_table=references["feature_table"],
        upstream_model=references["upstream_model"],
        raw=dict(raw),
    )


def _detect_resource_cycle(resources: Mapping[str, ResourceSpec]) -> None:
    colors: dict[str, int] = {resource_id: 0 for resource_id in resources}
    stack: list[str] = []

    def visit(resource_id: str) -> None:
        colors[resource_id] = 1
        stack.append(resource_id)
        for dependency in resources[resource_id].depends_on:
            if colors[dependency] == 1:
                start = stack.index(dependency)
                cycle = stack[start:] + [dependency]
                fail(
                    ErrorCode.CONFIG_REFERENCE_CYCLE,
                    "配置引用成环: " + " -> ".join(cycle),
                    resource_id=dependency,
                    path=cycle,
                )
            if colors[dependency] == 0:
                visit(dependency)
        stack.pop()
        colors[resource_id] = 2

    for resource_id in sorted(resources):
        if colors[resource_id] == 0:
            visit(resource_id)


def parse_manifest(raw_value: Mapping[str, Any], samples_root: str | Path) -> Manifest:
    raw = _as_dict(raw_value, "models.json")
    resources_raw = _as_list(raw.get("resources"), "models.json.resources")
    resources: dict[str, ResourceSpec] = {}
    for index, item in enumerate(resources_raw):
        resource = _parse_resource(item, index)
        if resource.resource_id in resources:
            fail(
                ErrorCode.INVALID_INPUT,
                f"重复的资源 ID: {resource.resource_id}",
                resource_id=resource.resource_id,
            )
        resources[resource.resource_id] = resource
    for resource in resources.values():
        for dependency in resource.depends_on:
            if dependency not in resources:
                fail(
                    ErrorCode.UNRESOLVED_REFERENCE,
                    f"资源 {resource.resource_id} 依赖不存在的资源 {dependency}",
                    resource_id=resource.resource_id,
                    actual=dependency,
                )
    _detect_resource_cycle(resources)

    models_raw = _as_list(raw.get("models"), "models.json.models")
    models: dict[int, ModelSpec] = {}
    previous_version: int | None = None
    for index, item in enumerate(models_raw):
        model = _parse_model(item, index)
        if model.version in models:
            fail(
                ErrorCode.VERSION_NOT_MONOTONIC,
                f"重复模型版本号 {model.version}",
                version=model.version,
            )
        if previous_version is not None and model.version <= previous_version:
            fail(
                ErrorCode.VERSION_NOT_MONOTONIC,
                f"模型版本号必须按清单顺序严格递增: {previous_version} -> {model.version}",
                version=model.version,
                expected=previous_version + 1,
                actual=model.version,
            )
        previous_version = model.version
        for field_name, expected_kind in MODEL_REFERENCE_KINDS.items():
            resource_id = getattr(model, field_name)
            resource = resources.get(resource_id)
            if resource is None:
                fail(
                    ErrorCode.UNRESOLVED_REFERENCE,
                    f"版本 {model.version} 的 {field_name} 引用 {resource_id} 无法解析",
                    version=model.version,
                    resource_id=resource_id,
                )
            if resource.kind != expected_kind:
                fail(
                    ErrorCode.UNRESOLVED_REFERENCE,
                    f"版本 {model.version} 的 {field_name} 引用 {resource_id} 类型为 {resource.kind}",
                    version=model.version,
                    resource_id=resource_id,
                    expected=expected_kind,
                    actual=resource.kind,
                )
        models[model.version] = model
    if not models:
        fail(ErrorCode.INVALID_INPUT, "模型版本清单不能为空")
    return Manifest(
        samples_root=str(Path(samples_root).resolve()),
        resources=resources,
        models=models,
        raw=dict(raw),
        digest=sha256_json(raw),
    )


def load_manifest(path: str | Path, samples_root: str | Path | None = None) -> Manifest:
    manifest_path = Path(path)
    root = Path(samples_root) if samples_root is not None else manifest_path.parent
    return parse_manifest(load_json(manifest_path), root)


def _parse_loaded_shards(
    raw_value: Any,
    replica_id: str,
    model: ModelSpec,
) -> dict[tuple[str, int], str]:
    if isinstance(raw_value, dict):
        pairs: list[tuple[str, int, Any]] = []
        for layer, shard_values in raw_value.items():
            if isinstance(shard_values, list):
                for shard_index, digest in enumerate(shard_values):
                    pairs.append((layer, shard_index, digest))
            elif isinstance(shard_values, dict):
                for key, digest in shard_values.items():
                    pairs.append((layer, int(key), digest))
            else:
                fail(
                    ErrorCode.REPLICA_INVALID,
                    f"副本 {replica_id} 的加载分片集合格式非法",
                    replica_id=replica_id,
                    layer=layer,
                )
    elif isinstance(raw_value, list):
        pairs = []
        for item in raw_value:
            item_dict = _as_dict(item, f"副本 {replica_id}.loaded_shards[]", replica_id=replica_id)
            pairs.append(
                (
                    _as_str(item_dict.get("layer"), "layer", replica_id=replica_id),
                    int_field(item_dict.get("shard_index"), "shard_index", minimum=0, replica_id=replica_id),
                    item_dict.get("digest"),
                )
            )
    else:
        fail(
            ErrorCode.REPLICA_INVALID,
            f"副本 {replica_id} 缺少加载分片集合",
            replica_id=replica_id,
        )

    actual: dict[tuple[str, int], str] = {}
    for layer_name, shard_index, digest in pairs:
        key = (layer_name, shard_index)
        if key in actual:
            fail(
                ErrorCode.REPLICA_INVALID,
                f"副本 {replica_id} 重复声明层 {layer_name} 分片 {shard_index}",
                replica_id=replica_id,
                layer=layer_name,
                shard_index=shard_index,
            )
        actual[key] = digest

    expected: dict[tuple[str, int], str] = {}
    for layer in model.layers:
        for shard in layer.shards:
            expected[(layer.name, shard.index)] = shard.digest

    for key, digest in expected.items():
        if key not in actual:
            fail(
                ErrorCode.MISSING_SHARD,
                f"副本 {replica_id} 未加载版本 {model.version} 的分片 {key[0]}#{key[1]}",
                replica_id=replica_id,
                layer=key[0],
                shard_index=key[1],
                version=model.version,
            )
        if actual[key] != digest:
            fail(
                ErrorCode.SHARD_DIGEST_MISMATCH,
                f"副本 {replica_id} 加载的分片摘要不一致",
                replica_id=replica_id,
                layer=key[0],
                shard_index=key[1],
                version=model.version,
                expected=digest,
                actual=actual[key],
            )
    extras = sorted(set(actual) - set(expected))
    if extras:
        fail(
            ErrorCode.REPLICA_INVALID,
            f"副本 {replica_id} 加载了版本 {model.version} 不存在的分片",
            replica_id=replica_id,
            layer=extras[0][0],
            shard_index=extras[0][1],
        )
    return actual


def parse_replicas(raw_value: Any, manifest: Manifest) -> tuple[ReplicaSpec, ...]:
    raw_list = _as_list(raw_value, "replicas.json")
    if not raw_list:
        fail(ErrorCode.REPLICA_INVALID, "副本清单不能为空")
    replicas: list[ReplicaSpec] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(raw_list):
        raw = _as_dict(raw_item, f"replicas[{index}]")
        replica_id = _as_str(raw.get("replica_id"), f"replicas[{index}].replica_id")
        if replica_id in seen:
            fail(
                ErrorCode.REPLICA_INVALID,
                f"重复副本 ID: {replica_id}",
                replica_id=replica_id,
            )
        seen.add(replica_id)
        loaded_version = int_field(
            raw.get("loaded_version"),
            "loaded_version",
            minimum=1,
            replica_id=replica_id,
        )
        model = manifest.models.get(loaded_version)
        if model is None:
            fail(
                ErrorCode.UNRESOLVED_REFERENCE,
                f"副本 {replica_id} 引用了不存在的模型版本 {loaded_version}",
                replica_id=replica_id,
                version=loaded_version,
            )
        loaded_shards = _parse_loaded_shards(
            raw.get("loaded_shards"),
            replica_id,
            model,
        )
        warmup_state = _as_str(raw.get("warmup_state"), "warmup_state", replica_id=replica_id)
        if warmup_state not in WARMUP_STATES:
            fail(
                ErrorCode.REPLICA_INVALID,
                f"副本 {replica_id} 的预热状态非法: {warmup_state}",
                replica_id=replica_id,
                actual=warmup_state,
            )
        gray_ratio = float_ratio(raw.get("gray_ratio", 0.0), "gray_ratio", replica_id=replica_id)
        replicas.append(
            ReplicaSpec(
                replica_id=replica_id,
                loaded_version=loaded_version,
                loaded_shards=loaded_shards,
                warmup_state=warmup_state,
                gray_ratio=gray_ratio,
                raw=dict(raw),
            )
        )
    return tuple(sorted(replicas, key=lambda replica: replica.replica_id))


def load_replicas(path: str | Path, manifest: Manifest) -> tuple[ReplicaSpec, ...]:
    return parse_replicas(load_json(path), manifest)


def parse_plan(raw_value: Any, manifest: Manifest, replicas: Iterable[ReplicaSpec]) -> UpdatePlan:
    raw = _as_dict(raw_value, "plan.json")
    round_id = _as_str(raw.get("round_id"), "plan.round_id")
    baseline_version = int_field(raw.get("baseline_version"), "plan.baseline_version", minimum=1)
    target_version = int_field(raw.get("target_version"), "plan.target_version", minimum=1)
    if baseline_version not in manifest.models:
        fail(
            ErrorCode.UNRESOLVED_REFERENCE,
            f"基线模型版本 {baseline_version} 不存在",
            baseline_version=baseline_version,
        )
    if target_version not in manifest.models:
        fail(
            ErrorCode.UNRESOLVED_REFERENCE,
            f"目标模型版本 {target_version} 不存在",
            target_version=target_version,
        )
    if target_version <= baseline_version:
        fail(
            ErrorCode.VERSION_NOT_MONOTONIC,
            "目标版本必须高于基线版本",
            baseline_version=baseline_version,
            target_version=target_version,
        )
    patch_id = _as_str(raw.get("patch_id"), "plan.patch_id")
    patch_path = _as_str(raw.get("patch_path"), "plan.patch_path")
    replica_ids_raw = _as_list(raw.get("replica_ids"), "plan.replica_ids")
    replica_list = tuple(replicas)
    known_replica_ids = {replica.replica_id for replica in replica_list}
    plan_replica_ids: list[str] = []
    for value in replica_ids_raw:
        replica_id = _as_str(value, "plan.replica_ids[]")
        if replica_id not in known_replica_ids:
            fail(
                ErrorCode.UNRESOLVED_REFERENCE,
                f"灰度计划引用未知副本 {replica_id}",
                replica_id=replica_id,
            )
        if replica_id not in plan_replica_ids:
            plan_replica_ids.append(replica_id)
    if not plan_replica_ids:
        fail(ErrorCode.REPLICA_INVALID, "灰度计划必须至少包含一个副本")
    replicas_by_id = {replica.replica_id: replica for replica in replica_list}
    for replica_id in plan_replica_ids:
        replica = replicas_by_id[replica_id]
        if replica.loaded_version != baseline_version:
            fail(
                ErrorCode.REPLICA_INVALID,
                f"灰度副本 {replica_id} 加载版本与计划基线版本不一致",
                replica_id=replica_id,
                expected=baseline_version,
                actual=replica.loaded_version,
            )

    start_at_ms = int_field(raw.get("start_at_ms"), "plan.start_at_ms", minimum=0)
    warmup_duration_ms = int_field(raw.get("warmup_duration_ms"), "warmup_duration_ms", minimum=0)
    warmup_timeout_ms = int_field(raw.get("warmup_timeout_ms"), "warmup_timeout_ms", minimum=1)
    if warmup_timeout_ms < warmup_duration_ms:
        fail(
            ErrorCode.INVALID_INPUT,
            "warmup_timeout_ms 必须不小于 warmup_duration_ms",
            actual=warmup_timeout_ms,
            expected=warmup_duration_ms,
        )
    rollback_window_ms = int_field(raw.get("rollback_window_ms"), "rollback_window_ms", minimum=1)
    rollback_step_ms = int_field(raw.get("rollback_step_ms"), "rollback_step_ms", minimum=1)
    clock_tolerance_ms = int_field(raw.get("clock_tolerance_ms"), "clock_tolerance_ms", minimum=0)

    stages_raw = _as_list(raw.get("stages"), "plan.stages")
    if not stages_raw:
        fail(ErrorCode.INVALID_GRAY_RATIO, "灰度档位序列不能为空")
    stages: list[GrayStage] = []
    previous_ratio = 0.0
    previous_at: int | None = None
    for index, stage_raw in enumerate(stages_raw):
        stage_dict = _as_dict(stage_raw, f"plan.stages[{index}]")
        level = int_field(stage_dict.get("level"), "stage.level", minimum=1)
        if level != index + 1:
            fail(
                ErrorCode.INVALID_GRAY_RATIO,
                "灰度档位 level 必须从 1 开始连续递增",
                actual=level,
                expected=index + 1,
            )
        ratio = float_ratio(stage_dict.get("ratio"), "stage.ratio")
        if ratio <= previous_ratio:
            fail(
                ErrorCode.INVALID_GRAY_RATIO,
                "灰度档位比例必须严格递增",
                actual=ratio,
                expected=f"> {previous_ratio}",
            )
        at_ms = int_field(stage_dict.get("at_ms"), "stage.at_ms", minimum=0)
        if previous_at is not None and at_ms < previous_at:
            fail(
                ErrorCode.INVALID_INPUT,
                "灰度档位时间必须非递减",
                actual=at_ms,
                expected=f">= {previous_at}",
            )
        if at_ms < start_at_ms:
            fail(
                ErrorCode.INVALID_INPUT,
                "灰度档位时间不能早于更新开始时间",
                actual=at_ms,
                expected=start_at_ms,
            )
        previous_ratio = ratio
        previous_at = at_ms
        stages.append(GrayStage(level=level, ratio=ratio, at_ms=at_ms))
    if abs(stages[-1].ratio - 1.0) > 1e-12:
        fail(
            ErrorCode.INVALID_GRAY_RATIO,
            "灰度档位序列必须以 1.0 收尾",
            actual=stages[-1].ratio,
        )
    return UpdatePlan(
        round_id=round_id,
        baseline_version=baseline_version,
        target_version=target_version,
        patch_id=patch_id,
        patch_path=patch_path,
        replica_ids=tuple(sorted(plan_replica_ids)),
        start_at_ms=start_at_ms,
        warmup_duration_ms=warmup_duration_ms,
        warmup_timeout_ms=warmup_timeout_ms,
        rollback_window_ms=rollback_window_ms,
        rollback_step_ms=rollback_step_ms,
        clock_tolerance_ms=clock_tolerance_ms,
        stages=tuple(stages),
        raw=dict(raw),
    )


def load_plan(
    path: str | Path,
    manifest: Manifest,
    replicas: Iterable[ReplicaSpec],
) -> UpdatePlan:
    return parse_plan(load_json(path), manifest, replicas)


def parse_patch(
    raw_value: Any,
    manifest: Manifest,
    *,
    samples_root: str | Path,
    verify_blob_paths: bool = True,
) -> PatchSpec:
    raw = _as_dict(raw_value, "patch")
    patch_id = _as_str(raw.get("patch_id"), "patch.patch_id")
    baseline_version = int_field(raw.get("baseline_version"), "patch.baseline_version", minimum=1)
    target_version = int_field(raw.get("target_version"), "patch.target_version", minimum=1)
    if baseline_version not in manifest.models:
        fail(
            ErrorCode.UNRESOLVED_REFERENCE,
            f"补丁基线版本 {baseline_version} 不存在",
            baseline_version=baseline_version,
        )
    target_model = manifest.models.get(target_version)
    if target_model is None:
        fail(
            ErrorCode.UNRESOLVED_REFERENCE,
            f"补丁目标版本 {target_version} 不存在",
            target_version=target_version,
        )
    entries_raw = _as_list(raw.get("entries"), "patch.entries")
    entries: list[PatchEntry] = []
    seen: set[tuple[str, int]] = set()
    for index, entry_raw in enumerate(entries_raw):
        entry = _as_dict(entry_raw, f"patch.entries[{index}]")
        layer = _as_str(entry.get("layer"), "entry.layer", version=target_version)
        target_layer = next((item for item in target_model.layers if item.name == layer), None)
        if target_layer is None:
            fail(
                ErrorCode.MISSING_LAYER_REFERENCE,
                f"补丁引用了目标版本中不存在的层 {layer}",
                version=target_version,
                layer=layer,
            )
        shard_index = int_field(
            entry.get("shard_index"),
            "entry.shard_index",
            minimum=0,
            version=target_version,
            layer=layer,
        )
        if shard_index >= len(target_layer.shards):
            fail(
                ErrorCode.MISSING_SHARD,
                f"补丁引用了层 {layer} 中不存在的分片 {shard_index}",
                version=target_version,
                layer=layer,
                shard_index=shard_index,
            )
        key = (layer, shard_index)
        if key in seen:
            fail(
                ErrorCode.PATCH_INVALID,
                f"补丁重复声明分片 {layer}#{shard_index}",
                version=target_version,
                layer=layer,
                shard_index=shard_index,
            )
        seen.add(key)
        target_shard = target_layer.shards[shard_index]
        byte_start = int_field(
            entry.get("byte_start"),
            "entry.byte_start",
            minimum=0,
            version=target_version,
            layer=layer,
            shard_index=shard_index,
        )
        byte_end = int_field(
            entry.get("byte_end"),
            "entry.byte_end",
            minimum=0,
            version=target_version,
            layer=layer,
            shard_index=shard_index,
        )
        if (byte_start, byte_end) != (target_shard.byte_start, target_shard.byte_end):
            fail(
                ErrorCode.PATCH_INVALID,
                f"补丁分片 {layer}#{shard_index} 的字节范围与目标版本不一致",
                version=target_version,
                layer=layer,
                shard_index=shard_index,
                expected=[target_shard.byte_start, target_shard.byte_end],
                actual=[byte_start, byte_end],
            )
        digest = require_digest(
            entry.get("digest"),
            field="entry.digest",
            version=target_version,
            layer=layer,
            shard_index=shard_index,
        )
        if digest != target_shard.digest:
            fail(
                ErrorCode.SHARD_DIGEST_MISMATCH,
                f"补丁分片 {layer}#{shard_index} 摘要与目标版本不一致",
                version=target_version,
                layer=layer,
                shard_index=shard_index,
                expected=target_shard.digest,
                actual=digest,
            )
        blob_path_value = entry.get("blob_path")
        if blob_path_value is None:
            blob_path_value = target_shard.path
        if blob_path_value is None:
            fail(
                ErrorCode.MISSING_SHARD,
                f"补丁分片 {layer}#{shard_index} 缺少可物化源路径",
                version=target_version,
                layer=layer,
                shard_index=shard_index,
            )
        blob_path = _as_str(
            blob_path_value,
            "entry.blob_path",
            version=target_version,
            layer=layer,
            shard_index=shard_index,
        )
        if verify_blob_paths and not resolve_path(samples_root, blob_path).is_file():
            fail(
                ErrorCode.MISSING_SHARD,
                f"补丁分片文件不存在: {blob_path}",
                version=target_version,
                layer=layer,
                shard_index=shard_index,
                path=blob_path,
            )
        entries.append(
            PatchEntry(
                layer=layer,
                shard_index=shard_index,
                byte_start=byte_start,
                byte_end=byte_end,
                digest=digest,
                blob_path=blob_path,
            )
        )
    return PatchSpec(
        patch_id=patch_id,
        baseline_version=baseline_version,
        target_version=target_version,
        entries=tuple(sorted(entries, key=lambda item: (item.layer, item.shard_index))),
        raw=dict(raw),
    )


def load_patch(
    path: str | Path,
    manifest: Manifest,
    *,
    samples_root: str | Path,
) -> PatchSpec:
    return parse_patch(load_json(path), manifest, samples_root=samples_root)


def parse_clock_steps(raw_value: Any) -> dict[str, tuple[ClockAction, ...]]:
    scenarios: dict[str, tuple[ClockAction, ...]] = {}
    if isinstance(raw_value, list):
        scenario_items = {"default": raw_value}
    elif isinstance(raw_value, dict):
        scenario_items = raw_value
    else:
        fail(ErrorCode.INVALID_CLOCK, "逻辑时钟步进表必须为数组或场景对象")
    for scenario_name, steps_value in scenario_items.items():
        scenario = _as_str(scenario_name, "clock scenario")
        steps = _as_list(steps_value, f"clock.{scenario}")
        parsed: list[ClockAction] = []
        previous_at: int | None = None
        for index, raw_step in enumerate(steps):
            step = _as_dict(raw_step, f"clock.{scenario}[{index}]")
            at_ms = int_field(step.get("at_ms"), "at_ms", minimum=0, scenario=scenario)
            if previous_at is not None and at_ms < previous_at:
                fail(
                    ErrorCode.INVALID_CLOCK,
                    "逻辑时钟步进不能回退",
                    scenario=scenario,
                    expected=f">= {previous_at}",
                    actual=at_ms,
                )
            command = _as_str(step.get("command"), "command", scenario=scenario)
            args = step.get("args", {})
            _as_dict(args, "args", scenario=scenario)
            parsed.append(ClockAction(at_ms=at_ms, command=command, args=dict(args), scenario=scenario))
            previous_at = at_ms
        scenarios[scenario] = tuple(parsed)
    return scenarios


def load_clock_steps(path: str | Path) -> dict[str, tuple[ClockAction, ...]]:
    return parse_clock_steps(load_json(path))


def load_samples(samples_dir: str | Path = "samples") -> Samples:
    root = Path(samples_dir).resolve()
    manifest = load_manifest(root / "models.json", root)
    replicas = load_replicas(root / "replicas.json", manifest)
    plan = load_plan(root / "plan.json", manifest, replicas)
    patch_path = resolve_path(root, plan.patch_path)
    patch = load_patch(patch_path, manifest, samples_root=root)
    if patch.patch_id != plan.patch_id:
        fail(
            ErrorCode.PATCH_INVALID,
            f"计划补丁 ID {plan.patch_id} 与补丁文件 {patch.patch_id} 不一致",
            expected=plan.patch_id,
            actual=patch.patch_id,
        )
    if patch.baseline_version != plan.baseline_version or patch.target_version != plan.target_version:
        fail(
            ErrorCode.PATCH_INVALID,
            "计划版本与补丁版本不一致",
            baseline_version=plan.baseline_version,
            target_version=plan.target_version,
        )
    clock_scenarios = load_clock_steps(root / "clock_steps.json")
    return Samples(
        root=root,
        manifest=manifest,
        replicas=replicas,
        plan=plan,
        patch=patch,
        clock_scenarios=clock_scenarios,
    )
