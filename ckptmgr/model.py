"""Training plan, shard strategy and save-manifest parsing."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .errors import (
    DtypeInvalidError,
    LayerNameDuplicateError,
    PlanFieldError,
    ShardCountInvalidError,
    ShapeInvalidError,
    StrategyInvalidError,
    VersionOrderInvalidError,
)
from .jsonio import digest_json, load_optional_json, read_json

DTYPE_ITEMSIZE: Dict[str, int] = {
    "bool": 1,
    "uint8": 1,
    "uint16": 2,
    "uint32": 4,
    "uint64": 8,
    "int8": 1,
    "int16": 2,
    "float16": 2,
    "bfloat16": 2,
    "int32": 4,
    "float32": 4,
    "int64": 8,
    "float64": 8,
}

DTYPE_ALIASES: Dict[str, str] = {
    "bf16": "bfloat16",
    "double": "float64",
    "float": "float32",
    "fp16": "float16",
    "fp32": "float32",
    "fp64": "float64",
    "half": "float16",
}

HEX_DIGEST_LENGTH = 64


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: Any, field: str) -> int:
    if not _is_int(value) or value <= 0:
        raise PlanFieldError(field, "must be a positive integer", value=value)
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if not _is_int(value) or value < 0:
        raise PlanFieldError(field, "must be a non-negative integer", value=value)
    return value


def _pick(mapping: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


@dataclass(frozen=True)
class LayerSpec:
    name: str
    shape: Tuple[int, ...]
    dtype: str
    base_revision: int = 0

    @property
    def nbytes(self) -> int:
        total = 1
        for dimension in self.shape:
            total *= dimension
        return total * DTYPE_ITEMSIZE[self.dtype]

    def to_canonical(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "shape": list(self.shape),
            "dtype": self.dtype,
            "revision": self.base_revision,
        }


@dataclass(frozen=True)
class VersionSpec:
    version: int
    changed_layers: Tuple[str, ...]

    def changes(self, layer_name: str) -> bool:
        return "*" in self.changed_layers or layer_name in self.changed_layers

    def to_canonical(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "changed_layers": list(self.changed_layers),
        }


@dataclass(frozen=True)
class Strategy:
    shard_count: int
    max_shard_bytes: int
    max_concurrency: int
    round_budget_ms: int
    clock_tolerance_ms: int = 0
    shard_base_cost_ms: int = 5
    bytes_per_ms: int = 4096

    def logical_cost_ms(self, size_bytes: int) -> int:
        transfer = (size_bytes + self.bytes_per_ms - 1) // self.bytes_per_ms
        return max(1, self.shard_base_cost_ms + transfer)

    def to_canonical(self) -> Dict[str, Any]:
        return {
            "shard_count": self.shard_count,
            "max_shard_bytes": self.max_shard_bytes,
            "max_concurrency": self.max_concurrency,
            "round_budget_ms": self.round_budget_ms,
            "clock_tolerance_ms": self.clock_tolerance_ms,
            "shard_base_cost_ms": self.shard_base_cost_ms,
            "bytes_per_ms": self.bytes_per_ms,
        }


@dataclass(frozen=True)
class Plan:
    checkpoint: str
    global_seed: int
    layers: Tuple[LayerSpec, ...]
    strategy: Strategy
    versions: Tuple[VersionSpec, ...]

    def layer_by_name(self) -> Dict[str, LayerSpec]:
        return {layer.name: layer for layer in self.layers}

    def default_version(self) -> int:
        if not self.versions:
            return 1
        return max(item.version for item in self.versions)

    def _version_changes(self, version: int) -> List[VersionSpec]:
        known = [item for item in self.versions if item.version <= version]
        if not any(item.version == version for item in self.versions):
            if not self.versions or version > max(item.version for item in self.versions):
                known.append(VersionSpec(version, ("*",)))
        return known

    def revisions_for(self, version: int) -> Dict[str, int]:
        changes = self._version_changes(version)
        revisions: Dict[str, int] = {}
        for layer in self.layers:
            revision = layer.base_revision
            for item in changes:
                if item.changes(layer.name):
                    revision += 1
            revisions[layer.name] = revision
        return revisions

    def to_canonical(self) -> Dict[str, Any]:
        return {
            "checkpoint": self.checkpoint,
            "global_seed": self.global_seed,
            "layers": [layer.to_canonical() for layer in self.layers],
            "strategy": self.strategy.to_canonical(),
            "versions": [item.to_canonical() for item in self.versions],
        }

    def digest(self) -> str:
        return digest_json(self.to_canonical())

    def summary(self) -> Dict[str, Any]:
        return {
            "checkpoint": self.checkpoint,
            "global_seed": self.global_seed,
            "layer_count": len(self.layers),
            "layers": [
                {
                    "name": layer.name,
                    "shape": list(layer.shape),
                    "dtype": layer.dtype,
                    "nbytes": layer.nbytes,
                }
                for layer in self.layers
            ],
            "strategy": self.strategy.to_canonical(),
            "versions": [item.version for item in self.versions],
            "plan_digest": self.digest(),
        }


def _parse_layers(raw: Any, default_dtype: Any) -> Tuple[LayerSpec, ...]:
    entries: List[Tuple[Any, Any]] = []
    if isinstance(raw, list):
        entries = [(None, item) for item in raw]
    elif isinstance(raw, dict):
        entries = list(raw.items())
    else:
        raise PlanFieldError("layers", "must be a list or an object keyed by layer name")
    if not entries:
        raise PlanFieldError("layers", "must contain at least one layer")

    layers: List[LayerSpec] = []
    seen = set()
    for key, entry in entries:
        if not isinstance(entry, dict):
            raise PlanFieldError("layers", "each layer must be an object")
        name = _pick(entry, ("name", "param_name", "layer_name"), key)
        if not isinstance(name, str) or not name.strip():
            raise PlanFieldError("layer.name", "layer name must be a non-empty string")
        name = name.strip()
        if name in seen:
            raise LayerNameDuplicateError(
                "layer names must be unique", layer=name
            )
        seen.add(name)

        shape = _pick(entry, ("shape", "dims", "dimensions"))
        if not isinstance(shape, (list, tuple)) or not shape:
            raise ShapeInvalidError(
                "layer shape must be a non-empty list of positive integers",
                layer=name,
            )
        parsed_shape: List[int] = []
        for dimension in shape:
            if not _is_int(dimension) or dimension <= 0:
                raise ShapeInvalidError(
                    "shape dimensions must be positive integers",
                    layer=name,
                    dimension=dimension,
                )
            parsed_shape.append(dimension)

        dtype = _pick(entry, ("dtype", "type"), default_dtype)
        if isinstance(dtype, str):
            dtype = DTYPE_ALIASES.get(dtype.lower(), dtype.lower())
        if not isinstance(dtype, str) or dtype not in DTYPE_ITEMSIZE:
            raise DtypeInvalidError(
                "dtype is not one of the supported enumerations",
                layer=name,
                dtype=dtype,
                allowed=sorted(DTYPE_ITEMSIZE),
            )
        revision = _nonnegative_int(entry.get("revision", 0), "layer.revision")
        layers.append(
            LayerSpec(
                name=name,
                shape=tuple(parsed_shape),
                dtype=dtype,
                base_revision=revision,
            )
        )
    return tuple(layers)


def _parse_strategy(raw: Any, layer_count: int) -> Strategy:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise StrategyInvalidError("strategy must be an object")
    if "strategy" in raw and isinstance(raw["strategy"], dict):
        raw = raw["strategy"]
    elif "shard_strategy" in raw and isinstance(raw["shard_strategy"], dict):
        raw = raw["shard_strategy"]

    try:
        shard_count = _positive_int(
            _pick(raw, ("shard_count", "num_shards", "shards"), 1),
            "strategy.shard_count",
        )
    except PlanFieldError as exc:
        raise ShardCountInvalidError(
            "shard_count must be a positive integer",
            shard_count=_pick(raw, ("shard_count", "num_shards", "shards"), 1),
        ) from exc
    if shard_count > layer_count:
        raise ShardCountInvalidError(
            "shard_count cannot exceed the number of layers",
            shard_count=shard_count,
            layer_count=layer_count,
        )
    try:
        max_shard_bytes = _positive_int(
            _pick(
                raw,
                ("max_shard_bytes", "shard_bytes_limit", "shard_byte_limit"),
                67108864,
            ),
            "strategy.max_shard_bytes",
        )
        max_concurrency = _positive_int(
            _pick(raw, ("max_concurrency", "concurrency", "per_round_concurrency"), 1),
            "strategy.max_concurrency",
        )
        round_budget_ms = _positive_int(
            _pick(
                raw,
                ("round_budget_ms", "time_budget_ms", "per_round_budget_ms"),
                60000,
            ),
            "strategy.round_budget_ms",
        )
        clock_tolerance_ms = _nonnegative_int(
            _pick(raw, ("clock_tolerance_ms", "logical_clock_tolerance_ms"), 0),
            "strategy.clock_tolerance_ms",
        )
        shard_base_cost_ms = _positive_int(
            _pick(raw, ("shard_base_cost_ms", "base_write_ms"), 5),
            "strategy.shard_base_cost_ms",
        )
        bytes_per_ms = _positive_int(
            _pick(raw, ("bytes_per_ms", "write_bytes_per_ms"), 4096),
            "strategy.bytes_per_ms",
        )
    except PlanFieldError as exc:
        raise StrategyInvalidError(
            "strategy numeric limits must be positive integers",
            field=exc.details.get("field"),
            value=exc.details.get("value"),
        ) from exc
    return Strategy(
        shard_count=shard_count,
        max_shard_bytes=max_shard_bytes,
        max_concurrency=max_concurrency,
        round_budget_ms=round_budget_ms,
        clock_tolerance_ms=clock_tolerance_ms,
        shard_base_cost_ms=shard_base_cost_ms,
        bytes_per_ms=bytes_per_ms,
    )


def _parse_changed_layers(value: Any, layer_names: Sequence[str], version: int) -> Tuple[str, ...]:
    if value is None:
        return ("*",)
    if not isinstance(value, (list, tuple)):
        raise VersionOrderInvalidError(
            "changed_layers must be a list", version=version
        )
    changed: List[str] = []
    known = set(layer_names)
    for item in value:
        if item == "*":
            if "*" not in changed:
                changed.append("*")
            continue
        if not isinstance(item, str) or item not in known:
            raise VersionOrderInvalidError(
                "changed_layers contains an unknown layer",
                version=version,
                layer=item,
            )
        if item not in changed:
            changed.append(item)
    return tuple(changed)


def _parse_versions(
    raw_plan: Mapping[str, Any],
    save_plan_override: Optional[Mapping[str, Any]],
    layer_names: Sequence[str],
) -> Tuple[VersionSpec, ...]:
    raw_save: Any = save_plan_override
    if raw_save is None or not raw_save:
        raw_save = _pick(
            raw_plan, ("save_plan", "checkpoint_manifest", "checkpoints", "versions")
        )
    if raw_save is None:
        raw_save = {"versions": [1]}
    if isinstance(raw_save, list):
        raw_save = {"versions": raw_save}
    if not isinstance(raw_save, dict):
        raise VersionOrderInvalidError("save plan must be an object or a list")
    if "save_plan" in raw_save and isinstance(raw_save["save_plan"], dict):
        raw_save = raw_save["save_plan"]
    elif "checkpoint_manifest" in raw_save and isinstance(
        raw_save["checkpoint_manifest"], dict
    ):
        raw_save = raw_save["checkpoint_manifest"]

    raw_versions = raw_save.get("versions")
    changes_map = raw_save.get("changes", raw_save.get("changed_layers"))
    if raw_versions is None and isinstance(changes_map, dict):
        try:
            raw_versions = sorted(int(key) for key in changes_map.keys())
        except (TypeError, ValueError) as exc:
            raise VersionOrderInvalidError(
                "change map keys must be checkpoint versions"
            ) from exc
    if raw_versions is None:
        raw_versions = [1]
    if not isinstance(raw_versions, list) or not raw_versions:
        raise VersionOrderInvalidError("versions must be a non-empty list")

    versions: List[VersionSpec] = []
    previous_input = 0
    normalized_changes: Dict[int, Any] = {}
    if isinstance(changes_map, dict):
        for key, value in changes_map.items():
            try:
                normalized_changes[int(key)] = value
            except (TypeError, ValueError) as exc:
                raise VersionOrderInvalidError(
                    "change map keys must be checkpoint versions", value=key
                ) from exc

    for entry in raw_versions:
        if _is_int(entry):
            version = entry
            changed_value = normalized_changes.get(version)
        elif isinstance(entry, dict):
            version = entry.get("version")
            changed_value = entry.get(
                "changed_layers",
                normalized_changes.get(version) if _is_int(version) else None,
            )
        else:
            raise VersionOrderInvalidError(
                "version entries must be integers or objects", value=entry
            )
        if not _is_int(version) or version <= 0:
            raise VersionOrderInvalidError(
                "checkpoint versions must be positive integers", version=version
            )
        if version <= previous_input:
            raise VersionOrderInvalidError(
                "checkpoint versions must be strictly increasing in declaration order",
                previous=previous_input,
                current=version,
            )
        previous_input = version
        changed = _parse_changed_layers(changed_value, layer_names, version)
        versions.append(VersionSpec(version=version, changed_layers=changed))

    return tuple(versions)


def parse_plan(
    raw_plan: Mapping[str, Any],
    strategy_override: Optional[Mapping[str, Any]] = None,
    save_plan_override: Optional[Mapping[str, Any]] = None,
) -> Plan:
    if not isinstance(raw_plan, dict):
        raise PlanFieldError("plan", "training plan must be a JSON object")

    checkpoint = _pick(raw_plan, ("checkpoint", "name", "checkpoint_name"), "default")
    if not isinstance(checkpoint, str) or not checkpoint.strip():
        raise PlanFieldError(
            "checkpoint", "checkpoint name must be a non-empty string"
        )
    checkpoint = checkpoint.strip()

    global_seed = _pick(raw_plan, ("global_seed", "seed"))
    if not _is_int(global_seed):
        raise PlanFieldError(
            "global_seed", "global seed must be an integer", value=global_seed
        )

    layers = _parse_layers(
        _pick(raw_plan, ("layers", "model_layers", "parameters")),
        _pick(raw_plan, ("dtype", "default_dtype")),
    )
    layer_names = [layer.name for layer in layers]

    raw_strategy = strategy_override
    if raw_strategy is None or not raw_strategy:
        raw_strategy = _pick(raw_plan, ("strategy", "shard_strategy"))
        if raw_strategy is None:
            raw_strategy = {
                key: raw_plan[key]
                for key in (
                    "shard_count",
                    "num_shards",
                    "max_shard_bytes",
                    "max_concurrency",
                    "round_budget_ms",
                    "clock_tolerance_ms",
                    "shard_base_cost_ms",
                    "bytes_per_ms",
                )
                if key in raw_plan
            }
    strategy = _parse_strategy(raw_strategy, len(layers))
    versions = _parse_versions(raw_plan, save_plan_override, layer_names)
    return Plan(
        checkpoint=checkpoint,
        global_seed=global_seed,
        layers=layers,
        strategy=strategy,
        versions=versions,
    )


def load_plan(
    plan_path: Union[str, Path],
    strategy_path: Optional[Union[str, Path]] = None,
    save_plan_path: Optional[Union[str, Path]] = None,
) -> Plan:
    raw_plan = read_json(plan_path)
    if not isinstance(raw_plan, dict):
        raise PlanFieldError("plan", "training plan must be a JSON object")
    if (
        isinstance(raw_plan.get("plan"), dict)
        and not any(
            key in raw_plan
            for key in ("layers", "model_layers", "parameters")
        )
    ):
        raw_plan = raw_plan["plan"]
    strategy = load_optional_json(strategy_path, "strategy") if strategy_path else None
    save_plan = (
        load_optional_json(save_plan_path, "save_plan") if save_plan_path else None
    )
    return parse_plan(raw_plan, strategy, save_plan)


def digest_is_hex(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != HEX_DIGEST_LENGTH:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def stable_layer_rank(name: str) -> bytes:
    return hashlib.sha256(("ckpt-shard/v1\0" + name).encode("utf-8")).digest()


def shard_assignment(layer_names: Iterable[str], shard_count: int) -> Dict[str, int]:
    ranked = sorted(layer_names, key=lambda name: (stable_layer_rank(name), name))
    return {name: index % shard_count for index, name in enumerate(ranked)}
