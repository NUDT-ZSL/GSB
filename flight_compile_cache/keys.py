"""Canonical compile-key construction and shape summaries."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .util import canonical_json, sha256_hex

COMPILE_KEY_PREFIX = "ck_"


def normalized_input_shapes(input_shapes: Mapping[str, Sequence[int]]) -> list[list[Any]]:
    return [[name, [int(dim) for dim in input_shapes[name]]] for name in sorted(input_shapes)]


def compile_key_payload(
    model_id: str, model_version: str, input_shapes: Mapping[str, Sequence[int]]
) -> dict[str, Any]:
    return {
        "schema": "flight-compile-key/v1",
        "model_id": model_id,
        "model_version": model_version,
        "input_shapes": normalized_input_shapes(input_shapes),
    }


def make_compile_key(
    model_id: str, model_version: str, input_shapes: Mapping[str, Sequence[int]]
) -> str:
    """Stable 24-hex-character key for (model version, input tensor shapes)."""

    payload = compile_key_payload(model_id, model_version, input_shapes)
    return COMPILE_KEY_PREFIX + sha256_hex(payload, length=24)


def compile_key_readable(
    model_id: str, model_version: str, input_shapes: Mapping[str, Sequence[int]]
) -> str:
    pieces = [model_id, model_version]
    for name, shape in normalized_input_shapes(input_shapes):
        pieces.append(f"{name}=" + "x".join(str(dim) for dim in shape))
    return "|".join(pieces)


def canonical_shapes_string(input_shapes: Mapping[str, Sequence[int]]) -> str:
    return canonical_json(normalized_input_shapes(input_shapes))
