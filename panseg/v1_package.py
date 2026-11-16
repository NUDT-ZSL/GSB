"""V1 binary run-length mask package reader and writer.

Binary layout (little-endian, all integers LEB128 varints unless noted)::

    magic    4 bytes  b"PAN1"
    version  uint8    always 1
    flags    uint8    bit 0 selects compression (0=pairs, 1=packed)
    width    varint
    height   varint
    count    varint
    record[count]:
        instance_id_len varint, utf-8 instance id
        class_len       varint, utf-8 semantic class name
        payload_len     varint
        payload         RLE varint bytes (see panseg.rle)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .errors import E_INPUT_INVALID, E_RLE_OUT_OF_RANGE, PipelineError
from .rle import decode_runs, encode_runs, encode_varint

MAGIC = b"PAN1"


@dataclass(frozen=True)
class V1RawInstance:
    instance_id: str
    semantic_class: str
    runs: tuple[tuple[int, int], ...]
    pixel_count: int


def _read_varint(buffer: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if offset >= len(buffer):
            raise PipelineError(E_RLE_OUT_OF_RANGE, "truncated v1 varint")
        byte = buffer[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, offset
        shift += 7
        if shift > 63:
            raise PipelineError(E_RLE_OUT_OF_RANGE, "v1 varint exceeds 64 bits")


def write_v1_package(
    path: str | Path,
    width: int,
    height: int,
    instances: list[V1RawInstance],
    compression: int,
) -> None:
    if compression not in (0, 1):
        raise ValueError("compression must be 0 or 1")
    payload = bytearray()
    payload.extend(MAGIC)
    payload.append(1)
    payload.append(compression)
    payload.extend(encode_varint(width))
    payload.extend(encode_varint(height))
    payload.extend(encode_varint(len(instances)))
    for instance in instances:
        identifier = instance.instance_id.encode("utf-8")
        semantic = instance.semantic_class.encode("utf-8")
        encoded = encode_runs(instance.runs, compression)
        payload.extend(encode_varint(len(identifier)))
        payload.extend(identifier)
        payload.extend(encode_varint(len(semantic)))
        payload.extend(semantic)
        payload.extend(encode_varint(len(encoded)))
        payload.extend(encoded)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(payload))


def parse_v1_package(path: str | Path) -> tuple[int, int, list[V1RawInstance]]:
    buffer = Path(path).read_bytes()
    if len(buffer) < 6 or buffer[:4] != MAGIC:
        raise PipelineError(E_INPUT_INVALID, f"not a v1 mask package: {path}")
    version = buffer[4]
    if version != 1:
        raise PipelineError(E_INPUT_INVALID, f"unsupported v1 package version {version}")
    flags = buffer[5]
    compression = flags & 1
    offset = 6
    width, offset = _read_varint(buffer, offset)
    height, offset = _read_varint(buffer, offset)
    count, offset = _read_varint(buffer, offset)
    total_pixels = width * height
    instances: list[V1RawInstance] = []
    for _ in range(count):
        id_len, offset = _read_varint(buffer, offset)
        instance_id = buffer[offset : offset + id_len].decode("utf-8")
        offset += id_len
        class_len, offset = _read_varint(buffer, offset)
        semantic_class = buffer[offset : offset + class_len].decode("utf-8")
        offset += class_len
        payload_len, offset = _read_varint(buffer, offset)
        payload = buffer[offset : offset + payload_len]
        if len(payload) != payload_len:
            raise PipelineError(
                E_RLE_OUT_OF_RANGE,
                "truncated v1 record payload",
                instance_id=instance_id,
            )
        offset += payload_len
        runs, pixel_count = decode_runs(payload, compression, total_pixels)
        instances.append(
            V1RawInstance(
                instance_id=instance_id,
                semantic_class=semantic_class,
                runs=runs,
                pixel_count=pixel_count,
            )
        )
    if offset != len(buffer):
        raise PipelineError(E_INPUT_INVALID, f"trailing bytes in v1 package {path}")
    return width, height, instances
