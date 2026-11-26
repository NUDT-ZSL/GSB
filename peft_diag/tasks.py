"""PEFT task-list ingestion, structure validation and deterministic normalization."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_bytes, content_hash, to_plain
from .errors import E_FORMAT, format_error, unsupported


SUPPORTED_TASK_VERSION = "v1"
SUPPORTED_SNAPSHOT_VERSIONS = {"v1", "v2"}
MAX_SEED = 2**32 - 1
CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
LAYER_KEY_PATTERN = re.compile(r"^(layer\.(?P<index>[0-9]+)\.(?P<module>[A-Za-z0-9_]+)\.(?P<factor>A|B))$")


MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "r1": {
        "hidden": 16,
        "num_layers": 4,
        "modules": ("q_proj", "v_proj"),
    },
    "r2": {
        "hidden": 24,
        "num_layers": 6,
        "modules": ("q_proj", "k_proj", "v_proj"),
    },
}

ADAPTER_TYPES: dict[str, dict[str, Any]] = {
    "lora": {"factor_count": 2, "allowed_base_revs": {"r1", "r2"}},
    "qlora": {"factor_count": 2, "allowed_base_revs": {"r1", "r2"}},
}


REQUIRED_FIELDS = {
    "case_id",
    "base_model_rev",
    "adapter_type",
    "adapter_dim",
    "num_layers",
    "lr",
    "steps",
    "micro_batch",
    "seed",
    "revision",
    "logical_clock_ms",
}
OPTIONAL_FIELDS = {"task_version", "expected_trainable_params"}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_int(
    data: dict[str, Any],
    field: str,
    *,
    case_id: str | None = None,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if field not in data:
        raise format_error(f"missing required field {field}", task_id=case_id, field=field)
    value = data[field]
    if not _is_int(value):
        raise format_error(
            f"{field} must be an integer, got {type(value).__name__}",
            task_id=case_id,
            field=field,
            details={"value": value},
        )
    if value < minimum or (maximum is not None and value > maximum):
        raise format_error(
            f"{field} is outside [{minimum}, {maximum if maximum is not None else 'inf'}]",
            task_id=case_id,
            field=field,
            details={"value": value},
        )
    return value


def _validate_case_id(value: Any) -> str:
    if not isinstance(value, str) or not CASE_ID_PATTERN.match(value):
        raise format_error(
            "case_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
            task_id=str(value) if value is not None else None,
            field="case_id",
            details={"value": value},
        )
    return value


@dataclass(frozen=True)
class AdapterLayerSpec:
    layer_idx: int
    module: str
    factor: str
    key: str
    shape: tuple[int, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer_idx": self.layer_idx,
            "module": self.module,
            "factor": self.factor,
            "key": self.key,
            "shape": list(self.shape),
        }


@dataclass
class NormalizedTask:
    case_id: str
    base_model_rev: str
    adapter_type: str
    adapter_dim: int
    num_layers: int
    lr: float
    steps: int
    micro_batch: int
    seed: int
    revision: int
    logical_clock_ms: int
    task_version: str
    trainable_params: int
    model_hidden: int
    adapter_modules: tuple[str, ...]
    layer_specs: tuple[AdapterLayerSpec, ...] = field(repr=False)

    @property
    def optimizer_steps(self) -> int:
        return self.steps // self.micro_batch

    @property
    def layer_keys(self) -> tuple[str, ...]:
        return tuple(spec.key for spec in self.layer_specs)

    def canonical_fields(self) -> dict[str, Any]:
        return {
            "task_version": self.task_version,
            "case_id": self.case_id,
            "base_model_rev": self.base_model_rev,
            "adapter_type": self.adapter_type,
            "adapter_dim": self.adapter_dim,
            "num_layers": self.num_layers,
            "lr": float(self.lr),
            "steps": self.steps,
            "micro_batch": self.micro_batch,
            "seed": self.seed,
            "revision": self.revision,
            "logical_clock_ms": self.logical_clock_ms,
        }

    def structure_dict(self) -> dict[str, Any]:
        return {
            "base_model_rev": self.base_model_rev,
            "adapter_type": self.adapter_type,
            "adapter_dim": self.adapter_dim,
            "num_layers": self.num_layers,
            "model_hidden": self.model_hidden,
            "adapter_modules": list(self.adapter_modules),
            "trainable_params": self.trainable_params,
            "layers": [spec.as_dict() for spec in self.layer_specs],
        }

    def canonical_dict(self) -> dict[str, Any]:
        data = self.canonical_fields()
        data["trainable_params"] = self.trainable_params
        data["optimizer_steps"] = self.optimizer_steps
        data["structure_summary"] = self.parameter_summary
        return data

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.canonical_dict())

    @property
    def parameter_summary(self) -> str:
        return content_hash(self.structure_dict())

    @property
    def fingerprint(self) -> str:
        return content_hash(self.canonical_dict())

    def as_dict(self) -> dict[str, Any]:
        data = {key: getattr(self, key) for key in self.canonical_fields()}
        data["trainable_params"] = self.trainable_params
        data["optimizer_steps"] = self.optimizer_steps
        data["structure_summary"] = self.parameter_summary
        data["layer_specs"] = [spec.as_dict() for spec in self.layer_specs]
        return to_plain(data)


def adapter_structure(
    *,
    base_model_rev: str,
    adapter_type: str,
    adapter_dim: int,
    num_layers: int,
    case_id: str | None = None,
) -> tuple[int, tuple[str, ...], tuple[AdapterLayerSpec, ...], int]:
    if base_model_rev not in MODEL_REGISTRY:
        raise unsupported(
            f"unsupported base_model_rev {base_model_rev!r}",
            task_id=case_id,
            field="base_model_rev",
            details={"supported": sorted(MODEL_REGISTRY)},
        )
    if adapter_type not in ADAPTER_TYPES:
        raise unsupported(
            f"unsupported adapter_type {adapter_type!r}",
            task_id=case_id,
            field="adapter_type",
            details={"supported": sorted(ADAPTER_TYPES)},
        )
    model = MODEL_REGISTRY[base_model_rev]
    adapter = ADAPTER_TYPES[adapter_type]
    if base_model_rev not in adapter["allowed_base_revs"]:
        raise unsupported(
            f"adapter_type {adapter_type} is not supported for {base_model_rev}",
            task_id=case_id,
            field="adapter_type",
        )
    expected_layers = int(model["num_layers"])
    if num_layers != expected_layers:
        raise format_error(
            "adapter layer count does not match base model revision",
            task_id=case_id,
            field="num_layers",
            details={"expected": expected_layers, "actual": num_layers},
        )
    hidden = int(model["hidden"])
    modules = tuple(str(module) for module in model["modules"])
    if adapter_type == "qlora":
        modules = tuple(dict.fromkeys((*modules, "k_proj")))
    if adapter_dim < 2 or adapter_dim > hidden:
        raise format_error(
            "adapter_dim must be in [2, hidden]",
            task_id=case_id,
            field="adapter_dim",
            details={"minimum": 2, "maximum": hidden, "actual": adapter_dim},
        )
    if adapter_dim % 2 != 0:
        raise format_error(
            "adapter_dim must be even",
            task_id=case_id,
            field="adapter_dim",
            details={"actual": adapter_dim},
        )
    specs: list[AdapterLayerSpec] = []
    for layer_idx in range(num_layers):
        for module in modules:
            for factor in ("A", "B"):
                key = f"layer.{layer_idx}.{module}.{factor}"
                specs.append(
                    AdapterLayerSpec(
                        layer_idx=layer_idx,
                        module=module,
                        factor=factor,
                        key=key,
                        shape=(hidden, adapter_dim),
                    )
                )
    trainable_params = num_layers * len(modules) * int(adapter["factor_count"]) * hidden * adapter_dim
    return hidden, modules, tuple(specs), trainable_params


def normalize_task(data: dict[str, Any], *, line_number: int | None = None) -> NormalizedTask:
    if not isinstance(data, dict):
        raise format_error("task line must be a JSON object", field="line", details={"line": line_number})
    unknown = sorted(set(data) - REQUIRED_FIELDS - OPTIONAL_FIELDS)
    if unknown:
        raise format_error(
            "unknown task field",
            field=unknown[0],
            details={"unknown": unknown, "line": line_number},
        )
    missing = sorted(REQUIRED_FIELDS - set(data))
    if missing:
        raise format_error(
            f"missing required field {missing[0]}",
            field=missing[0],
            details={"missing": missing, "line": line_number},
        )
    task_version = data.get("task_version", SUPPORTED_TASK_VERSION)
    if not isinstance(task_version, str) or task_version not in {SUPPORTED_TASK_VERSION}:
        raise unsupported(
            f"unsupported task version {task_version!r}",
            task_id=data.get("case_id"),
            field="task_version",
            details={"supported": [SUPPORTED_TASK_VERSION]},
        )
    case_id = _validate_case_id(data.get("case_id"))
    base_model_rev = data["base_model_rev"]
    if not isinstance(base_model_rev, str):
        raise format_error("base_model_rev must be a string", task_id=case_id, field="base_model_rev")
    adapter_type = data["adapter_type"]
    if not isinstance(adapter_type, str):
        raise format_error("adapter_type must be a string", task_id=case_id, field="adapter_type")
    adapter_dim = _require_int(data, "adapter_dim", case_id=case_id, minimum=1)
    num_layers = _require_int(data, "num_layers", case_id=case_id, minimum=1)
    steps = _require_int(data, "steps", case_id=case_id, minimum=1)
    micro_batch = _require_int(data, "micro_batch", case_id=case_id, minimum=1, maximum=steps)
    if steps % micro_batch != 0:
        raise format_error(
            "steps must be an exact multiple of micro_batch",
            task_id=case_id,
            field="micro_batch",
            details={"steps": steps, "micro_batch": micro_batch},
        )
    seed = _require_int(data, "seed", case_id=case_id, minimum=0, maximum=MAX_SEED)
    revision = _require_int(data, "revision", case_id=case_id, minimum=0)
    logical_clock_ms = _require_int(data, "logical_clock_ms", case_id=case_id, minimum=0)
    lr_raw = data["lr"]
    if not _is_number(lr_raw):
        raise format_error(
            "lr must be a number",
            task_id=case_id,
            field="lr",
            details={"type": type(lr_raw).__name__},
        )
    lr = float(lr_raw)
    if not (0.0 < lr <= 1.0):
        raise format_error(
            "lr must be in (0, 1]",
            task_id=case_id,
            field="lr",
            details={"value": lr},
        )
    hidden, modules, specs, trainable_params = adapter_structure(
        base_model_rev=base_model_rev,
        adapter_type=adapter_type,
        adapter_dim=adapter_dim,
        num_layers=num_layers,
        case_id=case_id,
    )
    if "expected_trainable_params" in data:
        expected = _require_int(data, "expected_trainable_params", case_id=case_id, minimum=1)
        if expected != trainable_params:
            raise format_error(
                "trainable parameter count does not match adapter structure",
                task_id=case_id,
                field="expected_trainable_params",
                details={"expected": trainable_params, "actual": expected},
            )
    return NormalizedTask(
        case_id=case_id,
        base_model_rev=base_model_rev,
        adapter_type=adapter_type,
        adapter_dim=adapter_dim,
        num_layers=num_layers,
        lr=lr,
        steps=steps,
        micro_batch=micro_batch,
        seed=seed,
        revision=revision,
        logical_clock_ms=logical_clock_ms,
        task_version=task_version,
        trainable_params=trainable_params,
        model_hidden=hidden,
        adapter_modules=modules,
        layer_specs=specs,
    )


def parse_tasks(path: str | Path) -> list[NormalizedTask]:
    task_path = Path(path)
    try:
        raw_lines = task_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise format_error(f"cannot read task list: {exc}", field="tasks_path", details={"path": str(task_path)})
    tasks: list[NormalizedTask] = []
    seen: dict[str, int] = {}
    for line_number, line in enumerate(raw_lines, start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise format_error(
                "invalid JSON task line",
                field="line",
                details={"line": line_number, "error": exc.msg},
            )
        task = normalize_task(data, line_number=line_number)
        if task.case_id in seen:
            raise format_error(
                "duplicate case_id",
                task_id=task.case_id,
                field="case_id",
                details={"first_line": seen[task.case_id], "duplicate_line": line_number},
            )
        seen[task.case_id] = line_number
        tasks.append(task)
    if not tasks:
        raise format_error("task list contains no tasks", field="tasks_path")
    tasks.sort(key=lambda item: (item.case_id, item.logical_clock_ms))
    return tasks


def write_tasks(path: str | Path, tasks: Iterable[NormalizedTask]) -> str:
    ordered = sorted(tasks, key=lambda item: (item.case_id, item.logical_clock_ms))
    lines = [canonical_bytes(task.canonical_fields()).decode("utf-8") for task in ordered]
    payload = "\n".join(lines) + "\n"
    from .canonical import atomic_write_bytes, sha256_bytes

    atomic_write_bytes(path, payload.encode("utf-8"))
    return sha256_bytes(payload.encode("utf-8"))


def validate_revision_transition(current: NormalizedTask, updated: NormalizedTask) -> None:
    if updated.case_id != current.case_id:
        raise format_error("revision update changed case_id", task_id=current.case_id, field="case_id")
    if updated.revision < current.revision:
        raise format_error(
            "revision rollback is not allowed",
            task_id=current.case_id,
            field="revision",
            details={"current": current.revision, "requested": updated.revision},
        )


def validate_layer_key(
    task: NormalizedTask,
    *,
    key: Any,
    layer_idx: Any,
    shape: Any,
    seed: Any,
) -> AdapterLayerSpec:
    if not isinstance(key, str):
        raise format_error("layer key must be a string", task_id=task.case_id, field="key")
    match = LAYER_KEY_PATTERN.match(key)
    if not match:
        raise format_error(
            "layer key does not follow layer.<idx>.<module>.<A|B>",
            task_id=task.case_id,
            field="key",
            details={"key": key},
        )
    parsed_index = int(match.group("index"))
    if not _is_int(layer_idx) or layer_idx != parsed_index:
        raise format_error(
            "layer_idx does not match layer key",
            task_id=task.case_id,
            field="layer_idx",
            details={"key": key, "layer_idx": layer_idx, "key_index": parsed_index},
        )
    if layer_idx < 0 or layer_idx >= task.num_layers:
        raise format_error(
            "layer_idx is out of range",
            task_id=task.case_id,
            field="layer_idx",
            details={"value": layer_idx, "num_layers": task.num_layers},
        )
    spec_by_key = {spec.key: spec for spec in task.layer_specs}
    if key not in spec_by_key:
        raise format_error(
            "unknown adapter layer key",
            task_id=task.case_id,
            field="key",
            details={"key": key, "expected": sorted(spec_by_key)},
        )
    expected_shape = spec_by_key[key].shape
    if (
        not isinstance(shape, (list, tuple))
        or len(shape) != 2
        or not all(_is_int(item) and item > 0 for item in shape)
        or tuple(shape) != expected_shape
    ):
        raise format_error(
            "adapter layer shape does not match the validated structure",
            task_id=task.case_id,
            field="shape",
            details={"key": key, "expected": list(expected_shape), "actual": shape},
        )
    if not _is_int(seed) or seed < 0 or seed > MAX_SEED:
        raise format_error(
            "seed must be an integer in [0, 2^32-1]",
            task_id=task.case_id,
            field="seed",
            details={"value": seed},
        )
    if seed != task.seed:
        raise format_error(
            "snapshot layer seed differs from task seed",
            task_id=task.case_id,
            field="seed",
            details={"expected": task.seed, "actual": seed},
        )
    return spec_by_key[key]


def assert_complete_layers(task: NormalizedTask, layer_keys: Iterable[str]) -> None:
    actual = set(layer_keys)
    expected = set(task.layer_keys)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise format_error(
            "snapshot layer set does not match adapter structure",
            task_id=task.case_id,
            field="layers",
            details={"missing": missing, "extra": extra},
        )


__all__ = [
    "ADAPTER_TYPES",
    "MODEL_REGISTRY",
    "AdapterLayerSpec",
    "NormalizedTask",
    "E_FORMAT",
    "adapter_structure",
    "assert_complete_layers",
    "normalize_task",
    "parse_tasks",
    "validate_layer_key",
    "validate_revision_transition",
    "write_tasks",
]

