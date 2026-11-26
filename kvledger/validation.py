from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .errors import Code, KvError
from .util import LAYER_ID_RE, is_int, is_number, read_json


@dataclass(frozen=True)
class LayerSpec:
    layer_id: str
    entry_bytes: tuple[int, ...]
    cold_order: int


@dataclass(frozen=True)
class RequestSpec:
    seq_id: str
    priority: int
    max_len: int
    generated_tokens: int
    layers: tuple[LayerSpec, ...]


@dataclass(frozen=True)
class FixedPointSpec:
    scale: int
    rounding: str


@dataclass(frozen=True)
class MemoryConfig:
    total_memory_bytes: int
    ratios: Mapping[str, Decimal]
    layer_capacity_bytes: Mapping[str, int]
    eviction_budget_bytes: int
    max_concurrent_evictions: int
    fixed_point: FixedPointSpec
    nvme_file: str
    lock_file: str


def _fail(code: str, message: str, **details: Any) -> None:
    raise KvError(code, message, details=details or None)


def validate_requests_payload(payload: Any) -> List[RequestSpec]:
    if not isinstance(payload, dict):
        _fail(Code.JSON_INVALID, "requests document must be a JSON object")
    raw_requests = payload.get("requests")
    if not isinstance(raw_requests, list) or not raw_requests:
        _fail(Code.JSON_INVALID, "requests must be a nonempty list")

    seen_seq: set[str] = set()
    requests: List[RequestSpec] = []
    for index, raw in enumerate(raw_requests):
        where = f"requests[{index}]"
        if not isinstance(raw, dict):
            _fail(Code.JSON_INVALID, f"{where} must be an object", index=index)

        seq_id = raw.get("seq_id")
        if not isinstance(seq_id, str) or not seq_id or len(seq_id) > 128:
            _fail(Code.SEQ_ID_INVALID, f"{where}.seq_id must be a nonempty string", index=index)
        if seq_id in seen_seq:
            _fail(Code.SEQ_ID_DUPLICATE, f"duplicate seq_id: {seq_id}", seq_id=seq_id)
        seen_seq.add(seq_id)

        max_len = raw.get("max_len")
        if not is_int(max_len) or max_len <= 0:
            _fail(Code.MAX_LEN_INVALID, f"{seq_id}: max_len must be a positive integer", seq_id=seq_id)

        generated = raw.get("generated_tokens")
        if not is_int(generated) or generated <= 0:
            _fail(
                Code.GENERATED_TOKENS_INVALID,
                f"{seq_id}: generated_tokens must be a positive integer",
                seq_id=seq_id,
            )
        if generated > max_len:
            _fail(
                Code.TOKEN_LIMIT_EXCEEDED,
                f"{seq_id}: generated_tokens exceeds max_len",
                seq_id=seq_id,
                generated_tokens=generated,
                max_len=max_len,
            )

        priority = raw.get("priority", 100)
        if not is_int(priority) or priority < 0:
            _fail(Code.PRIORITY_INVALID, f"{seq_id}: priority must be a nonnegative integer", seq_id=seq_id)

        raw_layers = raw.get("layers")
        if not isinstance(raw_layers, list) or len(raw_layers) == 0:
            _fail(Code.LAYER_LIST_EMPTY, f"{seq_id}: layers must be a nonempty list", seq_id=seq_id)

        seen_layers: set[str] = set()
        layers: List[LayerSpec] = []
        for layer_index, raw_layer in enumerate(raw_layers):
            layer_where = f"{seq_id}.layers[{layer_index}]"
            if not isinstance(raw_layer, dict):
                _fail(Code.JSON_INVALID, f"{layer_where} must be an object", seq_id=seq_id)
            layer_id = raw_layer.get("layer_id")
            if not isinstance(layer_id, str) or not LAYER_ID_RE.fullmatch(layer_id):
                _fail(
                    Code.LAYER_ID_INVALID,
                    f"{layer_where}.layer_id is not a legal layer identifier",
                    seq_id=seq_id,
                    layer_id=layer_id,
                )
            if layer_id in seen_layers:
                _fail(Code.LAYER_ID_DUPLICATE, f"{seq_id}: duplicate layer {layer_id}", seq_id=seq_id, layer_id=layer_id)
            seen_layers.add(layer_id)

            if "entry_bytes" in raw_layer and raw_layer.get("entry_bytes") is not None:
                raw_sizes = raw_layer["entry_bytes"]
                if not isinstance(raw_sizes, list) or not raw_sizes:
                    _fail(Code.KV_BYTES_INVALID, f"{layer_where}.entry_bytes must be a nonempty list", layer_id=layer_id)
                if len(raw_sizes) != generated:
                    _fail(
                        Code.ENTRY_BYTES_LENGTH_MISMATCH,
                        f"{layer_where}.entry_bytes length must equal generated_tokens",
                        layer_id=layer_id,
                        expected=generated,
                        actual=len(raw_sizes),
                    )
                sizes: List[int] = []
                for size in raw_sizes:
                    if not is_int(size) or size <= 0:
                        _fail(Code.KV_BYTES_INVALID, f"{layer_where}: KV entry byte size must be a positive integer", layer_id=layer_id)
                    sizes.append(size)
                entry_bytes = tuple(sizes)
            else:
                size = raw_layer.get("kv_entry_bytes", raw_layer.get("byte_size"))
                if not is_int(size) or size <= 0:
                    _fail(Code.KV_BYTES_INVALID, f"{layer_where}.kv_entry_bytes must be a positive integer", layer_id=layer_id)
                entry_bytes = tuple([int(size)] * generated)

            cold_order = raw_layer.get("cold_order", layer_index)
            if not is_int(cold_order):
                _fail(Code.JSON_INVALID, f"{layer_where}.cold_order must be an integer", layer_id=layer_id)

            layers.append(LayerSpec(layer_id, entry_bytes, int(cold_order)))

        requests.append(
            RequestSpec(
                seq_id=seq_id,
                priority=int(priority),
                max_len=int(max_len),
                generated_tokens=int(generated),
                layers=tuple(layers),
            )
        )
    return requests


def validate_requests_file(path: str | Path) -> List[RequestSpec]:
    return validate_requests_payload(read_json(path))


def _decimal_ratio(value: Any, code: str, message: str, **details: Any) -> Decimal:
    try:
        if isinstance(value, Decimal):
            ratio = value
        elif is_number(value) or isinstance(value, str):
            ratio = Decimal(str(value))
        else:
            raise ValueError("unsupported ratio type")
    except Exception as exc:
        raise KvError(code, message, details=details or None) from exc
    if not ratio.is_finite() or ratio <= 0 or ratio > 1:
        raise KvError(code, message, details=details or None)
    return ratio


def validate_memory_config_payload(payload: Any, layer_ids: set[str] | None = None) -> MemoryConfig:
    if not isinstance(payload, dict):
        _fail(Code.MEMORY_CONFIG_INVALID, "memory config must be an object")

    total = payload.get("total_memory_bytes")
    if not is_int(total) or total <= 0:
        _fail(Code.MEMORY_CONFIG_INVALID, "total_memory_bytes must be a positive integer")
    total = int(total)

    raw_ratios = payload.get("layer_capacity_ratios")
    if not isinstance(raw_ratios, dict) or not raw_ratios:
        _fail(Code.LAYER_RATIO_INVALID, "layer_capacity_ratios must be a nonempty object")

    ratios: Dict[str, Decimal] = {}
    capacities: Dict[str, int] = {}
    for layer_id, raw_ratio in raw_ratios.items():
        if not isinstance(layer_id, str) or not LAYER_ID_RE.fullmatch(layer_id):
            _fail(Code.LAYER_ID_INVALID, f"invalid layer id in ratios: {layer_id}", layer_id=layer_id)
        ratio = _decimal_ratio(
            raw_ratio,
            Code.LAYER_RATIO_INVALID,
            f"ratio for layer {layer_id} must be a finite number in (0,1]",
            layer_id=layer_id,
        )
        ratios[layer_id] = ratio
        capacities[layer_id] = int((Decimal(total) * ratio).to_integral_value(rounding="ROUND_FLOOR"))
        if capacities[layer_id] <= 0:
            _fail(Code.LAYER_RATIO_INVALID, f"ratio for layer {layer_id} rounds to zero capacity", layer_id=layer_id)

    ratio_sum = sum(ratios.values(), Decimal(0))
    if ratio_sum > 1:
        _fail(Code.LAYER_RATIO_SUM_INVALID, "layer capacity ratios must sum to at most 1", ratio_sum=str(ratio_sum))

    capacity_sum = sum(capacities.values())
    if total <= capacity_sum:
        _fail(
            Code.MEMORY_CAPACITY_INVALID,
            "total_memory_bytes must be greater than the sum of layer capacities",
            total_memory_bytes=total,
            layer_capacity_sum=capacity_sum,
        )

    if layer_ids is not None:
        missing = sorted(layer_ids - set(ratios))
        if missing:
            _fail(Code.LAYER_CONFIG_MISSING, "request layers missing from capacity ratios", missing_layers=missing)

    budget = payload.get("eviction_budget_bytes_per_round")
    if not is_int(budget) or budget <= 0:
        _fail(Code.BUDGET_INVALID, "eviction_budget_bytes_per_round must be a positive integer")

    concurrency = payload.get("max_concurrent_evictions")
    if not is_int(concurrency) or concurrency <= 0:
        _fail(Code.CONCURRENCY_INVALID, "max_concurrent_evictions must be a positive integer")

    raw_fp = payload.get("fixed_point", {})
    if not isinstance(raw_fp, dict):
        _fail(Code.FIXED_POINT_INVALID, "fixed_point must be an object")
    scale = raw_fp.get("scale", payload.get("fixed_point_scale", 64))
    if not is_int(scale) or scale <= 0:
        _fail(Code.FIXED_POINT_INVALID, "fixed_point.scale must be a positive integer bytes-per-unit scale")
    rounding = raw_fp.get("rounding", "floor")
    if rounding not in {"floor", "down"}:
        _fail(Code.FIXED_POINT_INVALID, "only floor fixed-point rounding is implemented")

    nvme_file = str(payload.get("nvme_file", "nvme/kv_swap.bin"))
    lock_file = str(payload.get("lock_file", "nvme/kv_swap.lock"))
    return MemoryConfig(
        total_memory_bytes=total,
        ratios=ratios,
        layer_capacity_bytes=capacities,
        eviction_budget_bytes=int(budget),
        max_concurrent_evictions=int(concurrency),
        fixed_point=FixedPointSpec(scale=int(scale), rounding="floor"),
        nvme_file=nvme_file,
        lock_file=lock_file,
    )


def validate_memory_config_file(path: str | Path, layer_ids: set[str] | None = None) -> MemoryConfig:
    return validate_memory_config_payload(read_json(path, parse_decimal=True), layer_ids)
