from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError, ErrorCode
from .models import InputBundle, InstanceSpec, LayerSpec, LedgerConfig, NodeSpec


_INSTANCE_FILES = (
    "instances.json",
    "container_instances.json",
    "container_instance_manifest.json",
)
_LAYER_FILES = ("layers.json", "image_layers.json", "image_layer_manifest.json")
_NODE_FILES = (
    "nodes.json",
    "node_storage.json",
    "node_storage_snapshot.json",
    "node_storage_snapshots.json",
)
_CONFIG_FILES = ("config.json", "ledger_config.json", "ledger.json")


def _fail(code: str, message: str, **details: Any) -> None:
    raise BusinessError(code, message, details)


def _load_json(path: Path) -> Any:
    if not path.is_file():
        _fail(ErrorCode.INVALID_JSON, "input file does not exist", path=str(path))
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(ErrorCode.INVALID_JSON, "unable to parse JSON input", path=str(path), reason=str(exc))


def _find_file(directory: Path, names: Iterable[str], label: str) -> Path:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    _fail(ErrorCode.INVALID_JSON, f"missing {label} JSON file", candidates=list(names))


def _rows(payload: Any, key: str, label: str) -> list[Any]:
    aliases = {
        "instances": (
            "instances",
            "container_instances",
            "container_instance_manifest",
            "items",
        ),
        "layers": ("layers", "image_layers", "managed_layers", "items"),
        "nodes": (
            "nodes",
            "node_snapshots",
            "node_storage",
            "node_storage_snapshots",
            "items",
        ),
    }.get(key, (key,))
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and any(
        isinstance(payload.get(name), list) for name in aliases
    ):
        rows = next(payload[name] for name in aliases if isinstance(payload.get(name), list))
    elif isinstance(payload, dict) and len(payload) == 1 and isinstance(next(iter(payload.values())), list):
        rows = next(iter(payload.values()))
    else:
        _fail(ErrorCode.SCHEMA_INVALID, f"{label} must be a JSON array", expected_key=key)
    return rows


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(ErrorCode.SCHEMA_INVALID, f"{label} must be a JSON object")
    return value


def _text(value: Any, field: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(ErrorCode.SCHEMA_INVALID, f"{label}.{field} must be a non-empty string", field=field)
    return value.strip()


def _integer(value: Any, field: str, label: str) -> int:
    if type(value) is not int:
        _fail(ErrorCode.INVALID_INTEGER, f"{label}.{field} must be an integer", field=field, value=value)
    return value


def _positive(value: Any, field: str, label: str) -> int:
    result = _integer(value, field, label)
    if result <= 0:
        _fail(ErrorCode.INVALID_QUANTITY, f"{label}.{field} must be positive", field=field, value=result)
    return result


def _nonnegative(value: Any, field: str, label: str) -> int:
    result = _integer(value, field, label)
    if result < 0:
        _fail(ErrorCode.INVALID_QUANTITY, f"{label}.{field} must be non-negative", field=field, value=result)
    return result


def _pick(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _unique(items: Iterable[str], code: str, label: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item in seen:
            _fail(code, f"duplicate {label}", value=item)
        seen.add(item)


def _parse_config(path: Path) -> LedgerConfig:
    raw = _load_json(path)
    if isinstance(raw, dict):
        for wrapper in ("ledger_config", "config", "ledger"):
            if isinstance(raw.get(wrapper), dict):
                raw = raw[wrapper]
                break
    row = _object(raw, "config")
    label = "config"

    def get(*names: str) -> Any:
        value = _pick(row, *names)
        if value is None:
            _fail(
                ErrorCode.INVALID_CONFIG,
                f"{label} is missing required field {names[0]}",
                field=names[0],
            )
        return value

    scale = _positive(
        get("fixed_scale", "fixed_point_scale", "scale"),
        "fixed_scale",
        label,
    )
    error_bound = _positive(
        get(
            "error_upper_bound_budget",
            "error_bound_budget",
            "error_budget",
            "fixed_error_budget",
        ),
        "error_upper_bound_budget",
        label,
    )
    tolerance_raw = _pick(
        row,
        "comparison_tolerance_ticks",
        "comparison_tolerance",
        "precision_comparison_tolerance",
    )
    tolerance = error_bound if tolerance_raw is None else _nonnegative(
        tolerance_raw, "comparison_tolerance_ticks", label
    )
    rollback = _pick(
        row,
        "clock_rollback_threshold_ms",
        "rollback_threshold_ms",
        "max_clock_rollback_ms",
    )
    rollback_threshold = 0 if rollback is None else _nonnegative(
        rollback, "clock_rollback_threshold_ms", label
    )
    return LedgerConfig(
        temp_storage_quota_limit_bytes=_positive(
            get(
                "temp_storage_quota_limit_bytes",
                "temporary_storage_quota_limit_bytes",
                "temp_storage_quota_bytes",
                "temporary_storage_quota_bytes",
            ),
            "temp_storage_quota_limit_bytes",
            label,
        ),
        layer_read_bandwidth_bytes_per_second=_positive(
            get(
                "layer_read_bandwidth_bytes_per_second",
                "image_layer_read_bandwidth_bytes_per_second",
                "layer_read_bandwidth_bps",
                "image_layer_read_bandwidth_bps",
            ),
            "layer_read_bandwidth_bytes_per_second",
            label,
        ),
        round_logical_time_budget_ms=_positive(
            get(
                "round_logical_time_budget_ms",
                "round_time_budget_ms",
                "single_round_time_budget_ms",
            ),
            "round_logical_time_budget_ms",
            label,
        ),
        fixed_scale=scale,
        error_upper_bound_budget=error_bound,
        logical_clock_step_ms=_positive(
            get("logical_clock_step_ms", "clock_step_ms", "clock_step"),
            "logical_clock_step_ms",
            label,
        ),
        comparison_tolerance_ticks=tolerance,
        clock_rollback_threshold_ms=rollback_threshold,
    )


def _parse_layers(path: Path, node_names: set[str]) -> tuple[LayerSpec, ...]:
    rows = _rows(_load_json(path), "layers", "layers")
    layers: list[LayerSpec] = []
    for index, value in enumerate(rows):
        label = f"layers[{index}]"
        row = _object(value, label)
        digest = _text(_pick(row, "digest", "layer_digest", "digest_sha256"), "digest", label)
        layer = LayerSpec(
            digest=digest,
            bytes=_positive(
                _pick(row, "bytes", "layer_bytes", "size_bytes"),
                "bytes",
                label,
            ),
            node=_text(_pick(row, "node", "node_name", "host_node"), "node", label),
            ref_count=_nonnegative(_pick(row, "ref_count", "reference_count"), "ref_count", label),
        )
        if layer.node not in node_names:
            _fail(
                ErrorCode.NODE_MISSING,
                "layer references an unknown node",
                digest=digest,
                node=layer.node,
            )
        layers.append(layer)
    _unique((item.digest for item in layers), ErrorCode.DUPLICATE_LAYER, "layer digest")
    return tuple(layers)


def _parse_nodes(path: Path) -> tuple[NodeSpec, ...]:
    rows = _rows(_load_json(path), "nodes", "nodes")
    nodes: list[NodeSpec] = []
    for index, value in enumerate(rows):
        label = f"nodes[{index}]"
        row = _object(value, label)
        nodes.append(
            NodeSpec(
                name=_text(_pick(row, "node", "node_name", "name", "host"), "node", label),
                available_ephemeral_bytes=_positive(
                    _pick(
                        row,
                        "available_ephemeral_bytes",
                        "available_temp_storage_bytes",
                        "available_ephemeral_storage_bytes",
                        "available_temp_bytes",
                    ),
                    "available_ephemeral_bytes",
                    label,
                ),
                image_layer_bytes=_nonnegative(
                    _pick(
                        row,
                        "image_layer_bytes",
                        "image_layers_bytes",
                        "image_layer_occupied_bytes",
                    ),
                    "image_layer_bytes",
                    label,
                ),
                max_concurrent_cold_starts=_positive(
                    _pick(
                        row,
                        "max_concurrent_cold_starts",
                        "max_concurrent_starts",
                        "concurrency_limit",
                    ),
                    "max_concurrent_cold_starts",
                    label,
                ),
            )
        )
    _unique((item.name for item in nodes), ErrorCode.DUPLICATE_NODE, "node name")
    return tuple(nodes)


def _parse_instances(
    path: Path,
    layers_by_digest: dict[str, LayerSpec],
) -> tuple[InstanceSpec, ...]:
    rows = _rows(_load_json(path), "instances", "instances")
    instances: list[InstanceSpec] = []
    for index, value in enumerate(rows):
        label = f"instances[{index}]"
        row = _object(value, label)
        instance_id = _text(
            _pick(row, "instance_id", "id", "container_instance_id"),
            "instance_id",
            label,
        )
        service = _text(_pick(row, "service", "service_name"), "service", label)
        priority = _nonnegative(_pick(row, "priority"), "priority", label)
        requested = _positive(
            _pick(
                row,
                "requested_ephemeral_bytes",
                "requested_temp_storage_bytes",
                "requested_write_bytes",
                "requested_temp_bytes",
                "ephemeral_storage_bytes",
            ),
            "requested_ephemeral_bytes",
            label,
        )
        deadline_ms = _nonnegative(
            _pick(
                row,
                "deadline_ms",
                "startup_deadline_ms",
                "deadline_logical_ms",
                "startup_deadline_logical_ms",
            ),
            "deadline_ms",
            label,
        )
        raw_layers = _pick(
            row,
            "required_layers",
            "required_layer_digests",
            "image_layers",
            "layers",
        )
        if not isinstance(raw_layers, list) or not raw_layers:
            _fail(ErrorCode.SCHEMA_INVALID, f"{label}.required_layers must be a non-empty array")
        required_layers: list[str] = []
        for layer_index, raw_digest in enumerate(raw_layers):
            digest = _text(raw_digest, f"required_layers[{layer_index}]", label)
            if digest not in layers_by_digest:
                _fail(
                    ErrorCode.LAYER_MISSING,
                    "required image layer is absent from the managed layer manifest",
                    instance_id=instance_id,
                    digest=digest,
                )
            required_layers.append(digest)
        _unique(required_layers, ErrorCode.DUPLICATE_REQUIRED_LAYER, "required layer")
        required_layers.sort()
        hosted_nodes = {layers_by_digest[digest].node for digest in required_layers}
        if len(hosted_nodes) != 1:
            _fail(
                ErrorCode.INSTANCE_NODE_AMBIGUOUS,
                "all required layers of a local cold start must be hosted by one node",
                instance_id=instance_id,
                nodes=sorted(hosted_nodes),
            )
        node = next(iter(hosted_nodes))
        explicit_node = _pick(row, "node", "node_name")
        if explicit_node is not None and _text(explicit_node, "node", label) != node:
            _fail(
                ErrorCode.INSTANCE_NODE_AMBIGUOUS,
                "declared instance node does not host all required layers",
                instance_id=instance_id,
                declared_node=explicit_node,
                layer_node=node,
            )
        instances.append(
            InstanceSpec(
                instance_id=instance_id,
                service=service,
                priority=priority,
                requested_ephemeral_bytes=requested,
                required_layers=tuple(required_layers),
                deadline_ms=deadline_ms,
                node=node,
            )
        )
    _unique((item.instance_id for item in instances), ErrorCode.DUPLICATE_INSTANCE, "instance id")
    return tuple(instances)


def load_inputs(directory: str | Path) -> InputBundle:
    root = Path(directory)
    if not root.is_dir():
        _fail(ErrorCode.INVALID_JSON, "input directory does not exist", path=str(root))

    config = _parse_config(_find_file(root, _CONFIG_FILES, "config"))
    nodes = _parse_nodes(_find_file(root, _NODE_FILES, "nodes"))
    node_names = {item.name for item in nodes}
    layers = _parse_layers(_find_file(root, _LAYER_FILES, "layers"), node_names)
    layers_by_digest = {item.digest: item for item in layers}
    instances = _parse_instances(_find_file(root, _INSTANCE_FILES, "instances"), layers_by_digest)

    managed_by_node: dict[str, int] = {name: 0 for name in node_names}
    for layer in layers:
        managed_by_node[layer.node] += layer.bytes
    for node in nodes:
        if managed_by_node[node.name] > node.image_layer_bytes:
            _fail(
                ErrorCode.LAYER_BYTES_EXCEED_NODE,
                "managed image-layer bytes exceed node snapshot occupancy",
                node=node.name,
                managed_layer_bytes=managed_by_node[node.name],
                image_layer_bytes=node.image_layer_bytes,
            )

    return InputBundle(
        instances=tuple(sorted(instances, key=lambda item: item.instance_id)),
        layers=tuple(sorted(layers, key=lambda item: item.digest)),
        nodes=tuple(sorted(nodes, key=lambda item: item.name)),
        config=config,
    )

