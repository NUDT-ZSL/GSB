"""Deterministic parameter material, shard layout and content addressing."""

import hashlib
import json
import struct
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .errors import ShardFormatError, ShardSizeExceededError
from .jsonio import canonical_bytes, digest_bytes, digest_json
from .model import LayerSpec, Plan, shard_assignment

SHARD_MAGIC = b"CKPTSH01"
SHARD_FORMAT = "ckpt-shard/v1"
PARAM_DOMAIN = "ckpt-parameter/v1"


@dataclass(frozen=True)
class LayerContent:
    name: str
    shape: Tuple[int, ...]
    dtype: str
    revision: int
    nbytes: int
    param_digest: str
    payload: bytes

    def metadata(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "shape": list(self.shape),
            "dtype": self.dtype,
            "revision": self.revision,
            "nbytes": self.nbytes,
            "param_digest": self.param_digest,
        }


@dataclass(frozen=True)
class ShardContent:
    shard_id: str
    shard_index: int
    layer_names: Tuple[str, ...]
    file_bytes: bytes
    digest: str
    size_bytes: int
    logical_cost_ms: int

    def to_record(self) -> Dict[str, Any]:
        return {
            "shard_id": self.shard_id,
            "shard_index": self.shard_index,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "layer_names": list(self.layer_names),
            "logical_cost_ms": self.logical_cost_ms,
        }


@dataclass(frozen=True)
class CheckpointContent:
    plan: Plan
    version: int
    layers: Tuple[LayerContent, ...]
    shards: Tuple[ShardContent, ...]
    parameter_digest: str
    layer_order: Tuple[str, ...]

    def layer_map(self) -> Dict[str, LayerContent]:
        return {layer.name: layer for layer in self.layers}

    def shard_map(self) -> Dict[str, ShardContent]:
        return {shard.shard_id: shard for shard in self.shards}

    def summary(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "parameter_digest": self.parameter_digest,
            "layer_order": list(self.layer_order),
            "layers": [layer.metadata() for layer in self.layers],
            "shards": [shard.to_record() for shard in self.shards],
        }


def derive_parameter(
    global_seed: int,
    layer: LayerSpec,
    revision: int,
) -> LayerContent:
    seed_material = canonical_bytes(
        {
            "domain": PARAM_DOMAIN,
            "global_seed": global_seed,
            "name": layer.name,
            "shape": list(layer.shape),
            "dtype": layer.dtype,
            "revision": revision,
        }
    )
    nbytes = layer.nbytes
    payload = hashlib.shake_256(seed_material).digest(nbytes)
    digest_input = hashlib.sha256()
    digest_input.update(b"ckpt-parameter-digest/v1\0")
    digest_input.update(seed_material)
    digest_input.update(b"\0")
    digest_input.update(payload)
    return LayerContent(
        name=layer.name,
        shape=layer.shape,
        dtype=layer.dtype,
        revision=revision,
        nbytes=nbytes,
        param_digest=digest_input.hexdigest(),
        payload=payload,
    )


def parameter_digest_for(
    global_seed: int,
    version: int,
    layers: Sequence[LayerContent],
) -> str:
    return digest_json(
        {
            "domain": "ckpt-checkpoint-parameters/v1",
            "global_seed": global_seed,
            "version": version,
            "layers": [layer.metadata() for layer in layers],
        }
    )


def _shard_id(index: int, shard_count: int) -> str:
    return "shard-{0:04d}-of-{1:04d}".format(index, shard_count)


def build_shard(
    shard_index: int,
    shard_count: int,
    layers: Sequence[LayerContent],
    logical_cost_ms: int,
) -> ShardContent:
    offset = 0
    layer_records: List[Dict[str, Any]] = []
    payload_parts: List[bytes] = []
    for layer in layers:
        layer_records.append(
            {
                "name": layer.name,
                "shape": list(layer.shape),
                "dtype": layer.dtype,
                "revision": layer.revision,
                "nbytes": layer.nbytes,
                "param_digest": layer.param_digest,
                "offset": offset,
            }
        )
        payload_parts.append(layer.payload)
        offset += layer.nbytes
    payload = b"".join(payload_parts)
    shard_id = _shard_id(shard_index, shard_count)
    header = {
        "format": SHARD_FORMAT,
        "shard_id": shard_id,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "layers": layer_records,
        "payload_bytes": len(payload),
    }
    header_bytes = canonical_bytes(header)
    file_bytes = (
        SHARD_MAGIC
        + struct.pack(">Q", len(header_bytes))
        + header_bytes
        + payload
    )
    return ShardContent(
        shard_id=shard_id,
        shard_index=shard_index,
        layer_names=tuple(layer.name for layer in layers),
        file_bytes=file_bytes,
        digest=digest_bytes(file_bytes),
        size_bytes=len(file_bytes),
        logical_cost_ms=logical_cost_ms,
    )


def derive_checkpoint(plan: Plan, version: int) -> CheckpointContent:
    revisions = plan.revisions_for(version)
    assignments = shard_assignment(
        (layer.name for layer in plan.layers), plan.strategy.shard_count
    )
    planned_sizes: List[int] = [0 for _ in range(plan.strategy.shard_count)]
    planned_layers: List[List[str]] = [
        [] for _ in range(plan.strategy.shard_count)
    ]
    for layer in plan.layers:
        shard_index = assignments[layer.name]
        planned_sizes[shard_index] += layer.nbytes
        planned_layers[shard_index].append(layer.name)
    for shard_index, payload_bytes in enumerate(planned_sizes):
        if payload_bytes > plan.strategy.max_shard_bytes:
            raise ShardSizeExceededError(
                "derived shard payload exceeds the configured maximum before materialization",
                shard_id=_shard_id(shard_index, plan.strategy.shard_count),
                payload_bytes_lower_bound=payload_bytes,
                max_shard_bytes=plan.strategy.max_shard_bytes,
                layers=planned_layers[shard_index],
            )
    layers = tuple(
        derive_parameter(plan.global_seed, layer, revisions[layer.name])
        for layer in plan.layers
    )
    shard_layers: List[List[LayerContent]] = [
        [] for _ in range(plan.strategy.shard_count)
    ]
    for layer in layers:
        shard_layers[assignments[layer.name]].append(layer)

    shards: List[ShardContent] = []
    for index, items in enumerate(shard_layers):
        provisional = build_shard(index, plan.strategy.shard_count, items, 1)
        cost = plan.strategy.logical_cost_ms(provisional.size_bytes)
        shard = replace(provisional, logical_cost_ms=cost)
        if shard.size_bytes > plan.strategy.max_shard_bytes:
            raise ShardSizeExceededError(
                "derived shard exceeds the configured maximum",
                shard_id=shard.shard_id,
                size_bytes=shard.size_bytes,
                max_shard_bytes=plan.strategy.max_shard_bytes,
                layers=list(shard.layer_names),
            )
        shards.append(shard)

    parameter_digest = parameter_digest_for(plan.global_seed, version, layers)
    return CheckpointContent(
        plan=plan,
        version=version,
        layers=layers,
        shards=tuple(shards),
        parameter_digest=parameter_digest,
        layer_order=tuple(layer.name for layer in layers),
    )


def decode_shard(data: bytes) -> Tuple[Dict[str, Any], bytes]:
    if len(data) < len(SHARD_MAGIC) + 8 or data[: len(SHARD_MAGIC)] != SHARD_MAGIC:
        raise ShardFormatError("shard magic header is invalid")
    header_length = struct.unpack(">Q", data[len(SHARD_MAGIC) : len(SHARD_MAGIC) + 8])[0]
    header_start = len(SHARD_MAGIC) + 8
    header_end = header_start + header_length
    if header_length <= 0 or header_end > len(data):
        raise ShardFormatError("shard header length is invalid")
    try:
        header = json.loads(data[header_start:header_end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShardFormatError("shard header is not valid JSON") from exc
    if not isinstance(header, dict) or header.get("format") != SHARD_FORMAT:
        raise ShardFormatError("shard format marker is invalid")
    payload = data[header_end:]
    if header.get("payload_bytes") != len(payload):
        raise ShardFormatError(
            "shard payload length does not match its header",
            expected=header.get("payload_bytes"),
            actual=len(payload),
        )
    return header, payload


def extract_layer_payloads(
    header: Mapping[str, Any],
    payload: bytes,
    shard_id: str,
) -> Dict[str, bytes]:
    records = header.get("layers")
    if not isinstance(records, list):
        raise ShardFormatError("shard layer table is missing", shard_id=shard_id)
    extracted: Dict[str, bytes] = {}
    expected_offset = 0
    for record in records:
        if not isinstance(record, dict):
            raise ShardFormatError("shard layer record must be an object", shard_id=shard_id)
        name = record.get("name")
        offset = record.get("offset")
        nbytes = record.get("nbytes")
        if not isinstance(name, str) or not isinstance(offset, int) or not isinstance(nbytes, int):
            raise ShardFormatError(
                "shard layer record has invalid offset or size", shard_id=shard_id
            )
        if offset != expected_offset or nbytes < 0 or offset + nbytes > len(payload):
            raise ShardFormatError(
                "shard layer payload ranges are not contiguous",
                shard_id=shard_id,
                layer=name,
                offset=offset,
                expected_offset=expected_offset,
                nbytes=nbytes,
            )
        if name in extracted:
            raise ShardFormatError(
                "shard contains a duplicate layer", shard_id=shard_id, layer=name
            )
        extracted[name] = payload[offset : offset + nbytes]
        expected_offset = offset + nbytes
    if expected_offset != len(payload):
        raise ShardFormatError(
            "shard has unreferenced payload bytes",
            shard_id=shard_id,
            consumed=expected_offset,
            payload_bytes=len(payload),
        )
    return extracted
